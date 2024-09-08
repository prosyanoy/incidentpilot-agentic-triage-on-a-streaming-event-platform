"""Event-driven ingestion pipeline: records -> DuckDB + detector -> incidents -> agent."""
from __future__ import annotations

import logging
import threading
from typing import Callable, Iterable

from .detector import Detector
from .models import Anomaly, Incident, IncidentReport
from .otel import parse_record
from .store import Store

log = logging.getLogger("incidentpilot.pipeline")
Triage = Callable[[Incident], IncidentReport]


class Pipeline:
    """Consumes OTLP-style records in event-time order.

    The first confirmed anomaly opens an incident; further anomalies are grouped
    into it for ``settle`` seconds of event time so the agent sees the whole blast
    radius, then the agent is dispatched. A ``cooldown`` suppresses duplicates.
    """

    def __init__(self, store: Store, triage: Triage | None = None, detector: Detector | None = None,
                 settle: float = 30.0, cooldown: float = 300.0, flush_rows: int = 1000):
        self.store = store
        self.triage = triage
        self.detector = detector or Detector()
        self.settle = settle
        self.cooldown = cooldown
        self.flush_rows = flush_rows
        self.incidents: list[Incident] = []
        self.stats = {"logs": 0, "metrics": 0, "rejected": 0, "anomalies": 0}
        self._logs: list[dict] = []
        self._metrics: list[dict] = []
        self._pending: Incident | None = None
        self._quiet_until = 0.0
        self._lock = threading.RLock()

    # -- public ----------------------------------------------------------
    def process(self, records: Iterable[dict]) -> list[Incident]:
        """Process records; returns incidents dispatched during this call."""
        done: list[Incident] = []
        with self._lock:
            for rec in records:
                try:
                    kind, rows = parse_record(rec)
                except ValueError as exc:
                    self.stats["rejected"] += 1
                    log.warning("rejected record: %s", exc)
                    continue
                for row in rows:
                    done += self.process_row(kind, row)
            self.flush()
        return done

    def process_row(self, kind: str, row: dict) -> list[Incident]:
        done = []
        with self._lock:
            p = self._pending
            if p is not None and row["ts"] >= p.detected_ts + self.settle:
                done.append(self._dispatch(p))
            if kind == "log":
                self._logs.append(row)
                self.stats["logs"] += 1
                found = self.detector.observe_log(row)
            else:
                self._metrics.append(row)
                self.stats["metrics"] += 1
                found = self.detector.observe_metric(row)
            if len(self._logs) + len(self._metrics) >= self.flush_rows:
                self.flush()
            self._on_anomalies(found)
        return done

    def finish(self) -> list[Incident]:
        """End of stream: close the last window and dispatch any pending incident."""
        with self._lock:
            self._on_anomalies(self.detector.flush())
            done = [self._dispatch(self._pending)] if self._pending else []
            self.flush()
            return done

    def flush(self) -> None:
        with self._lock:
            if self._logs:
                self.store.insert_logs(self._logs)
                self._logs = []
            if self._metrics:
                self.store.insert_metrics(self._metrics)
                self._metrics = []

    # -- internals ---------------------------------------------------------
    def _on_anomalies(self, found: list[Anomaly]) -> None:
        for a in found:
            self.stats["anomalies"] += 1
            if self._pending is not None:
                self._pending.anomalies.append(a)
            elif a.window_end >= self._quiet_until:
                inc = Incident(id=f"INC-{len(self.incidents) + 1:04d}", onset_ts=a.window_start,
                               detected_ts=a.window_end, anomalies=[a])
                self.incidents.append(inc)
                self._pending = inc
            elif self.incidents:
                self.incidents[-1].anomalies.append(a)

    def _dispatch(self, inc: Incident) -> Incident:
        self.flush()  # the agent must see everything up to the dispatch time
        inc.dispatch_ts = inc.detected_ts + self.settle
        self._pending = None
        self._quiet_until = inc.dispatch_ts + self.cooldown
        if self.triage is None:
            return inc
        try:
            inc.report = self.triage(inc)
            inc.status = "triaged"
        except Exception as exc:  # keep the stream alive; surface the failure on the incident
            log.exception("triage failed for %s", inc.id)
            inc.status, inc.error = "failed", f"{type(exc).__name__}: {exc}"
        return inc
