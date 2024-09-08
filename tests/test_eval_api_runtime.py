import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from incidentpilot.api import create_app
from incidentpilot.bus import MemoryBus, connect_bus
from incidentpilot.config import Settings
from incidentpilot.evaluation import CASES, HEALTHY_SEEDS, Case, render, run_case, run_eval
from incidentpilot.generator import generate, to_records
from incidentpilot.runtime import Runtime


@pytest.fixture(scope="module")
def eval_report():
    return run_eval()


def test_eval_has_enough_diverse_incidents():
    assert len(CASES) >= 10
    assert len({c.scenario for c in CASES}) >= 10
    assert len({c.seed for c in CASES}) == len(CASES)


def test_eval_scores_mock_agent_perfectly_and_quickly(eval_report):
    s = eval_report["summary"]
    assert s["incidents"] == len(CASES) and s["detected"] == len(CASES)
    assert s["root_cause_accuracy"] == 1.0
    assert s["root_service_accuracy"] == 1.0
    assert s["all_cited"] and s["false_alarms"] == 0 and s["healthy_runs"] == len(HEALTHY_SEEDS)
    assert 0 < s["mean_time_to_diagnosis_s"] < 90
    text = render(eval_report)
    assert "root_cause_accuracy" in text and "XX" not in text


def test_eval_detects_a_wrong_agent():
    class AlwaysDns:
        name = "dns"
        def next_step(self, m):
            if len(m) < 3:
                return {"action": "tool", "tool": "spl", "args": {"query": "search | stats count"}}
            return {"action": "final", "report": {"category": "dns_failure", "service": "gateway",
                                                   "evidence": [{"id": "E1", "finding": "x"}]}}
    res = run_case(Case("x", "disk_full", "inventory", 3), AlwaysDns())
    assert res.detected and not res.category_correct and not res.service_correct


@pytest.fixture
def client():
    app = create_app(Settings(llm="mock"))
    with TestClient(app) as c:
        yield c


def test_api_ingest_triage_query_flow(client):
    assert client.get("/health").json()["status"] == "ok"
    recs = list(to_records(generate("config_change", service="search", seed=2).events))
    for i in range(0, len(recs), 4000):
        r = client.post("/ingest", json={"records": recs[i:i + 4000]})
        assert r.status_code == 200
    client.post("/finish")
    incs = client.get("/incidents").json()
    assert len(incs) == 1 and incs[0]["status"] == "triaged"
    assert incs[0]["root_cause"]["category"] == "config_change"
    full = client.get(f"/incidents/{incs[0]['id']}").json()
    assert full["report"]["root_cause"]["service"] == "search"
    assert "Recommended fix" in client.get(f"/incidents/{incs[0]['id']}/report").text
    q = client.post("/query", json={"query": "search level=ERROR | stats count by service"})
    assert q.status_code == 200 and q.json()["columns"] == ["service", "count"]


def test_api_errors(client):
    assert client.post("/query", json={"query": "nonsense"}).status_code == 400
    assert client.get("/incidents/INC-9999").status_code == 404
    assert client.post("/ingest", json={"records": [{"x": 1}]}).json()["accepted"] == 1
    assert client.get("/health").json()["stats"]["rejected"] == 1


def test_runtime_consumes_from_bus_end_to_end():
    async def main():
        rt = Runtime(Settings(bus_url="memory://", llm="mock"))
        await rt.start_consumer()
        gen = generate("queue_backlog", seed=6)
        for rec in to_records(gen.events):
            await rt._bus.publish(json.dumps(rec).encode())
        await rt._bus.publish(b"not json")
        for _ in range(100):  # let the drain loop run
            await asyncio.sleep(0.1)
            if rt.pipeline.incidents and rt.pipeline.incidents[0].status != "open":
                break
        incs = rt.pipeline.incidents
        stats = dict(rt.pipeline.stats)
        await rt.stop()
        return incs, stats

    incs, stats = asyncio.run(main())
    assert incs and incs[0].report.root_cause.category == "queue_backlog"
    assert stats["rejected"] == 1


def test_connect_bus_variants():
    assert isinstance(asyncio.run(connect_bus("memory://", "s")), MemoryBus)
    with pytest.raises(ValueError):
        asyncio.run(connect_bus("kafka://x", "s"))
    with pytest.raises(ConnectionError):
        asyncio.run(connect_bus("nats://127.0.0.1:1", "s", retries=1, delay=0))


def test_nats_bus_adapter_publishes_and_dispatches_to_handler():
    from incidentpilot.bus import NatsBus

    class FakeMsg:
        data = b"hello"

    class FakeNC:
        def __init__(self):
            self.published, self.cb = [], None

        async def publish(self, subject, data):
            self.published.append((subject, data))

        async def subscribe(self, subject, cb):
            self.cb = cb

        async def drain(self):
            self.drained = True

    async def main():
        nc, got = FakeNC(), []
        bus = NatsBus(nc, "subj")

        async def handler(data):
            got.append(data)
        await bus.subscribe(handler)
        await bus.publish(b"x")
        await nc.cb(FakeMsg())
        await bus.close()
        return nc, got

    nc, got = asyncio.run(main())
    assert nc.published == [("subj", b"x")] and got == [b"hello"] and nc.drained
