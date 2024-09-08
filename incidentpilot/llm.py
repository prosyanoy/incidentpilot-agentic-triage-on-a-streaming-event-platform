"""LLM backends. All speak one tiny protocol: ``next_step(messages) -> action dict``.

An action is either
  {"action": "tool", "tool": "spl"|"anomalies", "args": {...}, "thought": "..."} or
  {"action": "final", "report": {...}}.

``MockLLM`` is a deterministic, offline investigator: it runs a fixed
investigation playbook through the same tool interface a real model uses and
reasons over the tool results (never over ground truth). ``OllamaLLM`` talks to a
local Ollama server. ``FallbackLLM`` degrades to the mock if the server is down.
"""
from __future__ import annotations

import json
import logging
import math
import time
from datetime import datetime, timezone
from typing import Any, Protocol

import httpx

from .playbook import PLAYBOOK

log = logging.getLogger("incidentpilot.llm")
CONTEXT_MARK = "INCIDENT_CONTEXT\n"
RESULT_MARK = "TOOL_RESULT\n"


class LLMError(RuntimeError):
    pass


class LLM(Protocol):
    name: str

    def next_step(self, messages: list[dict]) -> dict: ...


def hhmmss(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")


# ---------------------------------------------------------------------------
class MockLLM:
    name = "mock"

    # -- history helpers -------------------------------------------------
    @staticmethod
    def _parse(messages: list[dict]) -> tuple[dict, dict[str, dict]]:
        ctx, results = {}, {}
        for m in messages:
            c = m["content"]
            if m["role"] == "user" and c.startswith(CONTEXT_MARK):
                ctx = json.loads(c[len(CONTEXT_MARK):])
            elif m["role"] == "user" and c.startswith(RESULT_MARK):
                r = json.loads(c[len(RESULT_MARK):])
                results[r["query"]] = r
        return ctx, results

    @staticmethod
    def _rows(res: dict | None) -> list[dict]:
        if not res or res.get("error"):
            return []
        return [dict(zip(res["columns"], r)) for r in res["rows"]]

    def next_step(self, messages: list[dict]) -> dict:
        ctx, results = self._parse(messages)
        onset, dispatch = ctx["onset_ts"], ctx["dispatch_ts"]
        e0, b = math.floor(onset - 20), math.ceil(dispatch)
        a = math.floor(onset - 600)
        win = f"earliest={e0} latest={b}"
        wide = f"earliest={a} latest={b}"

        def ask(q: str, why: str):
            return {"action": "tool", "tool": "spl", "args": {"query": q}, "thought": why}

        q_svc = f"search level=ERROR,WARN {win} | stats count by service | sort -count"
        if q_svc not in results:
            return ask(q_svc, "Which services are producing the most errors/warnings?")
        counts = self._rows(results[q_svc])
        if counts:
            top = counts[0]["service"]
        else:
            anomalies = sorted(ctx.get("anomalies", []), key=lambda x: -abs(x["zscore"]))
            top = anomalies[0]["service"] if anomalies else "gateway"

        q_msg = f"search service={top} level=ERROR,WARN {win} | top 6 message"
        if q_msg not in results:
            return ask(q_msg, f"What are {top} actually complaining about?")
        q_peer = f"search service={top} level=ERROR,WARN peer=* {win} | top 3 peer"
        if q_peer not in results:
            return ask(q_peer, f"Do the errors in {top} blame a downstream peer?")
        peers = [r for r in self._rows(results[q_peer]) if r["peer"] and r["count"] >= 5]
        root = peers[0]["peer"] if peers else top

        q_peer_msg = None
        if root != top:
            q_peer_msg = f"search service={root} level=ERROR,WARN {win} | top 6 message"
            if q_peer_msg not in results:
                return ask(q_peer_msg, f"{top} blames {root}; check {root}'s own errors.")
        q_time = f"search service={top} level=ERROR,WARN {win} | timechart span=30s count"
        if q_time not in results:
            return ask(q_time, "When did the errors start and how do they trend?")
        q_chg = (f"search event=deploy,config_change {wide} | fields ts,service,event,message "
                 "| sort ts")
        if q_chg not in results:
            return ask(q_chg, "Did a deploy or config change precede the incident?")
        q_met = f"search source=metrics service={root} {wide} | stats min(value) max(value) by name"
        if q_met not in results:
            return ask(q_met, f"Do {root}'s resource metrics show saturation or growth?")
        q_src = (f"search service={top} level=ERROR,WARN {win} | "
                 "stats count dc(client_ip) as ips dc(user) as users")
        if q_src not in results:
            return ask(q_src, "Are the errors concentrated on a few clients or spread out?")
        return {"action": "final", "report": self._conclude(
            ctx, results, top, root, q_svc, q_msg, q_peer_msg, q_time, q_chg, q_met, q_src)}

    # -- reasoning ---------------------------------------------------------
    def _conclude(self, ctx, results, top, root, q_svc, q_msg, q_peer_msg, q_time, q_chg,
                  q_met, q_src) -> dict:
        eid = lambda q: results[q]["evidence_id"]  # noqa: E731
        onset = ctx["onset_ts"]
        counts = self._rows(results[q_svc])
        msgs = self._rows(results[q_msg]) + (self._rows(results[q_peer_msg]) if q_peer_msg else [])
        corpus = " ".join(str(m["message"]).lower() for m in msgs)
        metrics = {r["name"]: r for r in self._rows(results[q_met])}
        changes = [r for r in self._rows(results[q_chg])
                   if r["service"] in (root, top) and onset - 600 <= r["ts"] <= onset + 30]
        src = (self._rows(results[q_src]) or [{"count": 0, "ips": 0, "users": 0}])[0]

        scored = []
        for play in PLAYBOOK.values():
            score, why = 0, []
            for kw in play.keywords:
                if kw in corpus:
                    score += 2
                    why.append(f"log text matches '{kw}'")
            for name, kind, thr in play.metric_hints:
                m = metrics.get(name)
                if not m:
                    continue
                lo, hi = m["min(value)"], m["max(value)"]
                if (kind == "max>=" and hi >= thr) or (kind == "ratio>=" and lo > 0 and hi / lo >= thr):
                    score += 3
                    why.append(f"metric {name} on {root} went {lo:.0f} -> {hi:.0f}")
            if "deploy" in play.signals and any(c["event"] == "deploy" for c in changes):
                score += 4
                why.append("a deploy of this service preceded the errors")
            if "config" in play.signals and any(c["event"] == "config_change" for c in changes):
                score += 4
                why.append("a config change preceded the errors")
            if "few_ips_many_users" in play.signals and 0 < src["ips"] <= 10 and src["users"] >= 100:
                score += 3
                why.append(f"{src['count']} failures from only {src['ips']} IPs across {src['users']} users")
            if "many_ips" in play.signals and src["ips"] >= 100:
                score += 3
                why.append(f"requests spread over {src['ips']} client IPs")
            scored.append((score, play, why))
        scored.sort(key=lambda s: -s[0])
        best, play, why = scored[0]

        if best < 3:
            category, service, summary, conf = "unknown", top, \
                "Anomalies detected but no known failure signature matched.", 0.2
        else:
            category = play.category
            service = root if play.root_is_peer else top
            if play.category == "bad_deploy" and changes:
                service = changes[-1]["service"]
            summary = f"{play.summary} Evidence: " + "; ".join(why) + "."
            second = scored[1][0]
            conf = round(min(0.98, 0.5 + 0.06 * best + 0.03 * (best - second)), 2)

        findings = [{"id": eid(q_svc), "finding": "Error/warn volume by service: " + ", ".join(
            f"{c['service']}={c['count']}" for c in counts[:4])}]
        if msgs:
            m0 = msgs[0]
            findings.append({"id": eid(q_msg), "finding":
                             f"Top message in {top}: \"{m0['message']}\" ({m0['count']}x)"})
        if q_peer_msg:
            findings.append({"id": eid(q_peer_msg), "finding": f"{top} blames {root}; "
                             f"top message at {root}: " + (str(self._rows(results[q_peer_msg])[0]["message"])
                                                           if self._rows(results[q_peer_msg]) else "none")})
        trend = self._rows(results[q_time])
        timeline = [f"{hhmmss(a['window_end'])} detector flagged {a['service']} {a['signal']} "
                    f"(z={a['zscore']})" for a in sorted(ctx.get("anomalies", []),
                                                        key=lambda x: x["window_end"])[:3]]
        if trend:
            findings.append({"id": eid(q_time), "finding": f"Errors in {top} first seen in the "
                             f"{hhmmss(trend[0]['_time'])} bucket and peak at "
                             f"{max(t['count'] for t in trend)} per 30s"})
            timeline.insert(0, f"{hhmmss(trend[0]['_time'])} first error bucket in {top}")
        for c in changes:
            findings.append({"id": eid(q_chg), "finding": f"{c['event']} on {c['service']} at "
                             f"{hhmmss(c['ts'])}: {c['message']}"})
            timeline.insert(0, f"{hhmmss(c['ts'])} {c['event']} on {c['service']}: {c['message']}")
            break
        if metrics:
            interesting = [f"{n} {r['min(value)']:.0f}->{r['max(value)']:.0f}"
                           for n, r in metrics.items()
                           if r["min(value)"] > 0 and r["max(value)"] / r["min(value)"] >= 1.3]
            findings.append({"id": eid(q_met), "finding": f"{root} metrics: " +
                             (", ".join(interesting) if interesting else "no resource saturation")})
        findings.append({"id": eid(q_src), "finding": f"{src['count']} errors from {src['ips']} "
                         f"distinct IPs and {src['users']} distinct users"})
        floor = max(5, counts[0]["count"] * 0.1) if counts else 0
        affected = [c["service"] for c in counts if c["count"] >= floor]
        if service not in affected:
            affected.append(service)
        return {"category": category, "service": service, "summary": summary,
                "confidence": conf, "affected_services": affected, "timeline": timeline,
                "evidence": findings}


# ---------------------------------------------------------------------------
class OllamaLLM:
    def __init__(self, url: str = "http://localhost:11434", model: str = "llama3.1:8b",
                 timeout: float = 120.0, retries: int = 2):
        self.url, self.model, self.timeout, self.retries = url.rstrip("/"), model, timeout, retries
        self.name = f"ollama:{model}"

    def next_step(self, messages: list[dict]) -> dict:
        msgs = list(messages)
        last: Exception | None = None
        for _ in range(self.retries + 1):
            try:
                r = httpx.post(f"{self.url}/api/chat", timeout=self.timeout, json={
                    "model": self.model, "messages": msgs, "stream": False, "format": "json",
                    "options": {"temperature": 0}})
                r.raise_for_status()
                content = r.json()["message"]["content"]
                action = json.loads(content)
                if action.get("action") in ("tool", "final"):
                    return action
                raise ValueError("JSON lacks a valid 'action'")
            except (httpx.HTTPError, KeyError) as exc:
                raise LLMError(f"ollama unavailable: {exc}") from exc
            except (ValueError, json.JSONDecodeError) as exc:
                last = exc
                msgs = msgs + [{"role": "user", "content":
                                f"Your reply was invalid ({exc}). Reply with exactly one JSON action."}]
        raise LLMError(f"ollama returned invalid output: {last}")


class FallbackLLM:
    """Use ``primary``; on LLMError switch to ``fallback`` for the rest of the run."""

    def __init__(self, primary: Any, fallback: Any):
        self.primary, self.fallback, self.degraded = primary, fallback, False

    @property
    def name(self) -> str:
        return self.fallback.name + " (fallback)" if self.degraded else self.primary.name

    def next_step(self, messages: list[dict]) -> dict:
        if not self.degraded:
            try:
                return self.primary.next_step(messages)
            except LLMError as exc:
                log.warning("LLM backend failed (%s); falling back to mock", exc)
                self.degraded = True
        return self.fallback.next_step(messages)


def make_llm(kind: str, ollama_url: str = "", model: str = "") -> Any:
    if kind == "mock":
        return MockLLM()
    if kind == "ollama":
        return FallbackLLM(OllamaLLM(ollama_url or "http://localhost:11434", model or "llama3.1:8b"),
                           MockLLM())
    raise ValueError(f"unknown LLM backend {kind!r}; use 'mock' or 'ollama'")
