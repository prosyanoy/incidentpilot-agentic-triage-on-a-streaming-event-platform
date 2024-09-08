"""Streaming statistical anomaly detector.

Events are bucketed into fixed event-time windows. Per (service, signal) a robust
baseline (median / MAD over recent *normal* windows) is kept; a window is flagged
when its robust z-score exceeds a threshold, and an anomaly is emitted after
``confirm`` consecutive flagged windows (debounce). Flagged windows never feed
the baseline, so slow ramps (memory leaks) cannot "teach" the detector.
"""
from __future__ import annotations

import statistics
from collections import defaultdict, deque
from dataclasses import dataclass

from .models import Anomaly


@dataclass(frozen=True)
class SignalSpec:
    two_sided: bool
    abs_floor: float   # minimum scale in signal units
    rel_floor: float   # minimum scale as a fraction of |median|


COUNT = SignalSpec(two_sided=False, abs_floor=1.0, rel_floor=0.0)
RATE = SignalSpec(two_sided=True, abs_floor=3.0, rel_floor=0.1)
LATENCY = SignalSpec(two_sided=False, abs_floor=5.0, rel_floor=0.15)
METRIC = SignalSpec(two_sided=True, abs_floor=0.5, rel_floor=0.02)


class Detector:
    def __init__(self, window: float = 10.0, threshold: float = 4.0, confirm: int = 2,
                 warmup: int = 12, history: int = 60):
        self.window = window
        self.threshold = threshold
        self.confirm = confirm
        self.warmup = warmup
        self.history = history
        self._cur: int | None = None
        # window index -> key -> [sum, n]; key = (service, signal)
        self._acc: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0])
        self._spec: dict[tuple[str, str], SignalSpec] = {}
        self._baseline: dict[tuple[str, str], deque] = defaultdict(lambda: deque(maxlen=history))
        self._streak: dict[tuple[str, str], int] = defaultdict(int)
        self._first_flag: dict[tuple[str, str], int] = {}
        self.late_dropped = 0
        self.closed_windows = 0

    # ------------------------------------------------------------------
    def _add(self, key, spec: SignalSpec, value: float) -> None:
        self._spec[key] = spec
        a = self._acc[key]
        a[0] += value
        a[1] += 1

    def observe_log(self, row: dict) -> list[Anomaly]:
        out = self._advance(row["ts"])
        if out is None:
            return []
        svc = row["service"]
        self._add((svc, "events"), RATE, 1.0)
        self._add((svc, "errors"), COUNT, 1.0 if row["level"] in ("WARN", "ERROR") else 0.0)
        if row.get("latency_ms") is not None:
            self._add((svc, "latency_ms"), LATENCY, row["latency_ms"])
        return out

    def observe_metric(self, row: dict) -> list[Anomaly]:
        out = self._advance(row["ts"])
        if out is None:
            return []
        self._add((row["service"], f"metric:{row['name']}"), METRIC, row["value"])
        return out

    def flush(self) -> list[Anomaly]:
        """Close the current window (end of stream)."""
        if self._cur is None:
            return []
        out = self._close(self._cur)
        self._cur = None
        return out

    # ------------------------------------------------------------------
    def _advance(self, ts: float) -> list[Anomaly] | None:
        """Roll windows forward; None means the event is too late to count."""
        w = int(ts // self.window)
        if self._cur is None:
            self._cur = w
        if w < self._cur:
            self.late_dropped += 1
            return None
        out: list[Anomaly] = []
        while self._cur < w:
            out += self._close(self._cur)
            self._cur += 1
        return out

    def _close(self, w: int) -> list[Anomaly]:
        start = w * self.window
        out = []
        # count-like signals are zero when silent; level signals are skipped
        for key in list(self._spec):
            acc = self._acc.get(key)
            spec = self._spec[key]
            if acc is None or acc[1] == 0:
                if key[1] in ("events", "errors"):
                    value = 0.0
                else:
                    continue
            elif key[1] in ("events", "errors"):
                value = acc[0]
            else:
                value = acc[0] / acc[1]
            a = self._evaluate(key, spec, value, start)
            if a:
                out.append(a)
        self._acc.clear()
        self.closed_windows += 1
        return out

    def _evaluate(self, key, spec: SignalSpec, value: float, start: float) -> Anomaly | None:
        base = self._baseline[key]
        if len(base) < self.warmup:
            base.append(value)
            return None
        med = statistics.median(base)
        mad = statistics.median(abs(x - med) for x in base)
        # MAD is robust but noisy on short histories; the stdev of the (already
        # outlier-free) baseline keeps the scale from collapsing on lucky draws.
        scale = max(1.4826 * mad, statistics.pstdev(base), spec.rel_floor * abs(med), spec.abs_floor)
        z = (value - med) / scale
        flagged = abs(z) > self.threshold if spec.two_sided else z > self.threshold
        if not flagged:
            base.append(value)
            self._streak[key] = 0
            self._first_flag.pop(key, None)
            return None
        self._streak[key] += 1
        self._first_flag.setdefault(key, int(start // self.window))
        if self._streak[key] == self.confirm:
            first = self._first_flag[key] * self.window
            return Anomaly(window_start=first, window_end=start + self.window,
                           service=key[0], signal=key[1], value=round(value, 3),
                           baseline=round(med, 3), zscore=round(z, 2))
        return None
