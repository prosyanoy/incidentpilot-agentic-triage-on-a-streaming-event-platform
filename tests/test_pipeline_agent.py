import json

import httpx
import pytest

from incidentpilot.agent import AgentError, TriageAgent
from incidentpilot.generator import generate, to_records
from incidentpilot.llm import FallbackLLM, LLMError, MockLLM, OllamaLLM, make_llm
from incidentpilot.models import Incident
from incidentpilot.pipeline import Pipeline
from incidentpilot.report import to_markdown
from incidentpilot.store import Store
from tests.conftest import run_scenario


def test_pipeline_opens_one_incident_and_triages(deploy_run):
    gen, pipe, store = deploy_run
    assert len(pipe.incidents) == 1
    inc = pipe.incidents[0]
    assert inc.status == "triaged" and inc.report
    assert gen.truth.start_ts <= inc.onset_ts <= gen.truth.start_ts + 30
    assert inc.dispatch_ts == inc.detected_ts + pipe.settle
    assert len(inc.anomalies) >= 2


def test_report_is_correct_and_cites_real_evidence(deploy_run):
    _, pipe, store = deploy_run
    rep = pipe.incidents[0].report
    assert rep.root_cause.category == "bad_deploy"
    assert rep.root_cause.service == "payments"
    assert rep.recommended_fix and "roll back" in rep.recommended_fix[0].lower()
    assert len(rep.evidence) >= 3
    ids = [e.id for e in rep.evidence]
    assert len(set(ids)) == len(ids)
    from incidentpilot.query import run_query
    for e in rep.evidence:  # every cited query is re-runnable and returned rows
        assert e.rows > 0 and e.finding
        assert run_query(e.query, store).rows
    assert "2.7.0" in " ".join(rep.timeline)
    md = to_markdown(rep)
    assert "Recommended fix" in md and "[E1]" in md


def test_agent_never_sees_data_after_dispatch_time(deploy_run):
    _, pipe, _ = deploy_run
    inc = pipe.incidents[0]
    for e in inc.report.evidence:
        if "latest=" in e.query:
            latest = float(e.query.split("latest=")[1].split()[0])
            assert latest <= inc.dispatch_ts + 1


def test_batching_does_not_change_results():
    gen = generate("cert_expiry", seed=9)
    results = []
    for batch in (3, 97, 5000):
        store = Store()
        pipe = Pipeline(store, TriageAgent(store, MockLLM()).investigate, flush_rows=batch)
        recs = list(to_records(gen.events))
        for i in range(0, len(recs), batch):
            pipe.process(recs[i:i + batch])
        pipe.finish()
        results.append(pipe.incidents[0].report.root_cause.model_dump())
        store.close()
    assert results[0] == results[1] == results[2]
    assert results[0]["category"] == "cert_expiry" and results[0]["service"] == "payments"


def test_cooldown_prevents_duplicate_incidents():
    _, pipe, _ = run_scenario("traffic_spike", seed=5)
    assert len(pipe.incidents) == 1


def test_malformed_records_are_counted_not_fatal():
    store = Store()
    pipe = Pipeline(store, None)
    good = next(to_records(generate(None, duration=5).events))
    pipe.process([{"bogus": 1}, good, {"resource": {}}])
    assert pipe.stats["rejected"] == 2 and store.count("logs") + store.count("metrics") == 1


def test_triage_failure_marks_incident_failed():
    class Boom:
        name = "boom"

        def next_step(self, messages):
            raise RuntimeError("model exploded")

    _, pipe, _ = run_scenario("dns_failure", llm=Boom(), seed=3)
    inc = pipe.incidents[0]
    assert inc.status == "failed" and "model exploded" in inc.error and inc.report is None


class Scripted:
    """LLM that replays fixed actions."""
    name = "scripted"

    def __init__(self, actions):
        self.actions = list(actions)

    def next_step(self, messages):
        return self.actions.pop(0)


def _agent(actions, store):
    return TriageAgent(store, Scripted(actions), max_steps=4)


def _incident():
    return Incident(id="INC-X", onset_ts=1760000180, detected_ts=1760000200, dispatch_ts=1760000230)


def test_agent_drops_invented_citations_and_unknown_categories(small_store):
    spl = {"action": "tool", "tool": "spl", "args": {"query": "search level=ERROR | stats count"}}
    final = {"action": "final", "report": {
        "category": "aliens", "service": "payments", "summary": "?", "confidence": "high",
        "evidence": [{"id": "E1", "finding": "real"}, {"id": "E99", "finding": "invented"}]}}
    rep = _agent([spl, final], small_store).investigate(_incident())
    assert rep.root_cause.category == "unknown"
    assert [e.id for e in rep.evidence] == ["E1"]
    assert 0 <= rep.root_cause.confidence <= 1


def test_agent_recovers_from_bad_query_and_unknown_tool(small_store):
    actions = [
        {"action": "tool", "tool": "spl", "args": {"query": "search nosuch=1"}},
        {"action": "tool", "tool": "rm", "args": {}},
        {"action": "tool", "tool": "spl", "args": {"query": "search level=ERROR | stats count"}},
        {"action": "final", "report": {"category": "bad_deploy", "service": "payments",
                                        "evidence": [{"id": "E1", "finding": "x"}]}},
    ]
    rep = _agent(actions, small_store).investigate(_incident())
    assert rep.steps == 4 and rep.evidence[0].query == "search level=ERROR | stats count"


def test_agent_uncited_report_gets_ledger_and_step_limit(small_store):
    spl = {"action": "tool", "tool": "spl", "args": {"query": "search | stats count"}}
    rep = _agent([spl, {"action": "final", "report": {"category": "dns_failure"}}],
                 small_store).investigate(_incident())
    assert rep.evidence and rep.evidence[0].id == "E1"
    with pytest.raises(AgentError):
        _agent([spl] * 5, small_store).investigate(_incident())


def test_fallback_llm_degrades_to_mock(monkeypatch):
    def down(*a, **k):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(httpx, "post", down)
    llm = make_llm("ollama", "http://127.0.0.1:1", "x")
    assert isinstance(llm, FallbackLLM)
    _, pipe, _ = run_scenario("disk_full", llm=llm, seed=8)
    rep = pipe.incidents[0].report
    assert rep.root_cause.category == "disk_full" and "fallback" in rep.llm


def test_ollama_parses_json_and_retries_invalid_output(monkeypatch):
    replies = iter(["not json", json.dumps({"nope": 1}),
                    json.dumps({"action": "final", "report": {"category": "x"}})])
    seen = []

    def fake_post(url, json=None, timeout=None):
        seen.append(json)
        return httpx.Response(200, json={"message": {"content": next(replies)}},
                              request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx, "post", fake_post)
    action = OllamaLLM("http://o", "m", retries=2).next_step([{"role": "user", "content": "hi"}])
    assert action["action"] == "final"
    assert seen[0]["format"] == "json" and seen[0]["options"]["temperature"] == 0
    assert len(seen[2]["messages"]) == 3  # corrective turns were appended

    replies2 = iter(["bad"] * 3)
    monkeypatch.setattr(httpx, "post", lambda url, json=None, timeout=None: httpx.Response(
        200, json={"message": {"content": next(replies2)}}, request=httpx.Request("POST", url)))
    with pytest.raises(LLMError):
        OllamaLLM("http://o", "m", retries=2).next_step([])


def test_make_llm_rejects_unknown_backend():
    with pytest.raises(ValueError):
        make_llm("gpt-banana")
