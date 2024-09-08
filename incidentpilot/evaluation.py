"""Offline evaluation: score the agent on scripted incidents."""
from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass

from .agent import TriageAgent
from .generator import generate
from .llm import LLM, MockLLM
from .pipeline import Pipeline
from .store import Store


@dataclass(frozen=True)
class Case:
    name: str
    scenario: str
    service: str
    seed: int
    start: int = 180


CASES: list[Case] = [
    Case("leak-checkout", "memory_leak", "checkout", 1),
    Case("leak-auth", "memory_leak", "auth", 2, start=150),
    Case("deploy-payments", "bad_deploy", "payments", 3),
    Case("deploy-checkout", "bad_deploy", "checkout", 4, start=200),
    Case("stuffing-auth", "credential_stuffing", "auth", 5),
    Case("dbpool-checkout", "db_pool_exhaustion", "checkout", 6),
    Case("dbpool-payments", "db_pool_exhaustion", "payments", 7, start=160),
    Case("disk-inventory", "disk_full", "inventory", 8),
    Case("disk-search", "disk_full", "search", 9),
    Case("saturation-inventory", "dependency_saturation", "inventory", 10),
    Case("saturation-payments", "dependency_saturation", "payments", 11),
    Case("cert-payments", "cert_expiry", "payments", 12),
    Case("cert-inventory", "cert_expiry", "inventory", 13, start=170),
    Case("config-search", "config_change", "search", 14),
    Case("config-payments", "config_change", "payments", 15),
    Case("backlog-inventory", "queue_backlog", "inventory", 16),
    Case("cache-redis", "cache_failure", "cache", 17),
    Case("spike-gateway", "traffic_spike", "gateway", 18),
    Case("dns-checkout", "dns_failure", "checkout", 19),
    Case("dns-gateway", "dns_failure", "gateway", 20),
]
HEALTHY_SEEDS = [101, 102, 103]


@dataclass
class CaseResult:
    case: str
    expected_category: str
    expected_service: str
    detected: bool
    predicted_category: str | None
    predicted_service: str | None
    category_correct: bool
    service_correct: bool
    evidence_cited: int
    detection_delay_s: float | None
    time_to_diagnosis_s: float | None
    agent_seconds: float | None
    steps: int | None
    confidence: float | None


def run_case(case: Case, llm: LLM | None = None) -> CaseResult:
    gen = generate(case.scenario, service=case.service, seed=case.seed, start=case.start)
    truth = gen.truth
    store = Store()
    try:
        agent = TriageAgent(store, llm or MockLLM())
        pipe = Pipeline(store, agent.investigate)
        for e in gen.events:
            pipe.process_row(e["kind"], e["row"])
        pipe.finish()
        # the incident that opened at (or just after) the injection
        inc = next((i for i in pipe.incidents
                    if truth.start_ts - 10 <= i.onset_ts <= truth.start_ts + 120), None)
        rep = inc.report if inc else None
        if inc is None or rep is None:
            return CaseResult(case.name, truth.category, truth.service, inc is not None, None, None,
                              False, False, 0, None, None, None, None, None)
        return CaseResult(
            case.name, truth.category, truth.service, True, rep.root_cause.category,
            rep.root_cause.service, rep.root_cause.category == truth.category,
            rep.root_cause.service == truth.service, len(rep.evidence),
            round(inc.detected_ts - truth.start_ts, 1),
            round(inc.dispatch_ts - truth.start_ts + rep.agent_seconds, 2),
            rep.agent_seconds, rep.steps, rep.root_cause.confidence)
    finally:
        store.close()


def false_alarms(seed: int) -> int:
    gen = generate(None, seed=seed)
    store = Store()
    try:
        pipe = Pipeline(store, None)
        for e in gen.events:
            pipe.process_row(e["kind"], e["row"])
        pipe.finish()
        return len(pipe.incidents)
    finally:
        store.close()


def summarize(results: list[CaseResult], healthy_alarms: list[int]) -> dict:
    n = len(results)
    ttds = [r.time_to_diagnosis_s for r in results if r.time_to_diagnosis_s is not None]
    ordered = sorted(ttds)
    p95 = ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))] if ordered else None
    return {
        "incidents": n,
        "detected": sum(r.detected for r in results),
        "root_cause_accuracy": round(sum(r.category_correct for r in results) / n, 3),
        "root_service_accuracy": round(sum(r.service_correct for r in results) / n, 3),
        "all_cited": all(r.evidence_cited >= 2 for r in results),
        "mean_time_to_diagnosis_s": round(statistics.mean(ttds), 2) if ttds else None,
        "p95_time_to_diagnosis_s": p95,
        "mean_steps": round(statistics.mean(r.steps for r in results if r.steps), 2) if ttds else None,
        "healthy_runs": len(healthy_alarms),
        "false_alarms": sum(healthy_alarms),
    }


def run_eval(llm: LLM | None = None, cases: list[Case] | None = None) -> dict:
    results = [run_case(c, llm) for c in (cases or CASES)]
    alarms = [false_alarms(s) for s in HEALTHY_SEEDS]
    return {"summary": summarize(results, alarms), "cases": [asdict(r) for r in results]}


def render(report: dict) -> str:
    rows = report["cases"]
    head = f"{'case':<22}{'expected':<24}{'predicted':<24}{'svc':<5}{'ttd(s)':>8}{'steps':>6}"
    out = [head, "-" * len(head)]
    for r in rows:
        ok = "ok" if r["category_correct"] else "XX"
        out.append(f"{r['case']:<22}{r['expected_category']:<24}{str(r['predicted_category']):<24}"
                   f"{'y' if r['service_correct'] else 'n':<5}{str(r['time_to_diagnosis_s']):>8}"
                   f"{str(r['steps']):>6}  {ok}")
    out.append("")
    out += [f"{k}: {v}" for k, v in report["summary"].items()]
    return "\n".join(out)
