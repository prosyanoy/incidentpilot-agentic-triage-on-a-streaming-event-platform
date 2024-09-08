"""Tool-using triage agent: investigates an incident with SPL queries and writes a cited report."""
from __future__ import annotations

import json
import time

from .llm import CONTEXT_MARK, RESULT_MARK, LLM
from .models import EvidenceItem, Incident, IncidentReport, RootCause
from .playbook import PLAYBOOK, UNKNOWN_FIX
from .query import QueryError, run_query
from .store import Store

PREVIEW_ROWS = 25

SYSTEM_PROMPT = """You are IncidentPilot, an SRE agent that triages production incidents.
You investigate by calling tools, one per turn, then write a final report.
Reply with EXACTLY one JSON object per turn, either
 {"action":"tool","tool":"spl","args":{"query":"..."},"thought":"..."}
 {"action":"tool","tool":"anomalies","args":{}}
 {"action":"final","report":{"category":"<one of the categories>","service":"<root-cause service>",
   "summary":"...","confidence":0.0-1.0,"affected_services":["..."],"timeline":["HH:MM:SS ..."],
   "evidence":[{"id":"E1","finding":"what that evidence shows"}]}}
Every claim in the report must cite an evidence id returned by a tool.
Query language (Splunk-like), tables: logs (default) and metrics (source=metrics):
 search [source=metrics] field=value level=ERROR,WARN service=x "free text" earliest=<epoch> latest=<epoch>
   | stats count|dc(f)|avg(f)|min(f)|max(f)|sum(f)|p95(f) [as alias] [by f1,f2]
   | timechart span=30s [fn(f)] [by field] | top N field | sort -field | head N | fields a,b | where cond
log fields: ts service host version level message trace_id http_status latency_ms client_ip user peer event path
metric fields: ts service host name value (names: cpu_pct memory_mb disk_used_pct db_pool_in_use consumer_lag)
Categories: """ + ", ".join(PLAYBOOK) + ", unknown."


class AgentError(RuntimeError):
    pass


class TriageAgent:
    def __init__(self, store: Store, llm: LLM, max_steps: int = 14):
        self.store, self.llm, self.max_steps = store, llm, max_steps

    def investigate(self, incident: Incident) -> IncidentReport:
        started = time.perf_counter()
        ctx = {
            "id": incident.id, "onset_ts": incident.onset_ts, "detected_ts": incident.detected_ts,
            "dispatch_ts": incident.dispatch_ts,
            "anomalies": [a.model_dump() for a in incident.anomalies],
        }
        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": CONTEXT_MARK + json.dumps(ctx)}]
        evidence: dict[str, EvidenceItem] = {}
        steps = 0
        while True:
            if steps >= self.max_steps:
                raise AgentError(f"no final report after {self.max_steps} steps")
            action = self.llm.next_step(messages)
            steps += 1
            messages.append({"role": "assistant", "content": json.dumps(action)})
            if action["action"] == "final":
                return self._build(incident, action.get("report", {}), evidence, steps,
                                   time.perf_counter() - started)
            result = self._run_tool(action, evidence)
            messages.append({"role": "user", "content": RESULT_MARK + json.dumps(result, default=str)})

    # ------------------------------------------------------------------
    def _run_tool(self, action: dict, evidence: dict[str, EvidenceItem]) -> dict:
        tool, args = action.get("tool"), action.get("args") or {}
        if tool == "anomalies":
            return {"query": "anomalies()", "error": "use the incident context; anomalies are listed there"}
        if tool != "spl":
            return {"query": str(tool), "error": f"unknown tool {tool!r}; use 'spl'"}
        query = str(args.get("query", ""))
        try:
            res = run_query(query, self.store)
        except QueryError as exc:
            return {"query": query, "error": str(exc)}
        eid = f"E{len(evidence) + 1}"
        evidence[eid] = EvidenceItem(id=eid, query=query, finding="", rows=len(res.rows))
        return {"query": query, "evidence_id": eid, "columns": res.columns,
                "rows": res.rows[:PREVIEW_ROWS], "row_count": len(res.rows),
                "truncated": len(res.rows) > PREVIEW_ROWS}

    def _build(self, incident: Incident, rep: dict, ledger: dict[str, EvidenceItem], steps: int,
               seconds: float) -> IncidentReport:
        category = rep.get("category", "unknown")
        if category not in PLAYBOOK:
            category = "unknown"
        play = PLAYBOOK.get(category)
        cited = []
        for item in rep.get("evidence", []):
            base = ledger.get(item.get("id"))
            if base:  # drop citations the agent invented
                cited.append(base.model_copy(update={"finding": str(item.get("finding", ""))}))
        if not cited and ledger:  # never ship an uncited report
            cited = [e.model_copy(update={"finding": f"query returned {e.rows} rows"})
                     for e in list(ledger.values())[:3]]
        try:
            conf = float(rep.get("confidence", 0.3))
        except (TypeError, ValueError):
            conf = 0.3
        service = str(rep.get("service") or (incident.anomalies[0].service if incident.anomalies else "unknown"))
        affected = [str(s) for s in rep.get("affected_services", [])] or [service]
        return IncidentReport(
            incident_id=incident.id,
            title=f"{play.title if play else 'Unclassified anomaly'} in {service}",
            severity=play.severity if play else "medium",
            root_cause=RootCause(category=category, service=service,
                                 summary=str(rep.get("summary", "")), confidence=max(0.0, min(1.0, conf))),
            affected_services=affected,
            timeline=[str(t) for t in rep.get("timeline", [])],
            evidence=cited,
            recommended_fix=list(play.fix if play else UNKNOWN_FIX),
            steps=steps, llm=self.llm.name, agent_seconds=round(seconds, 4))
