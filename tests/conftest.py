import pytest

from incidentpilot.agent import TriageAgent
from incidentpilot.generator import generate
from incidentpilot.llm import MockLLM
from incidentpilot.pipeline import Pipeline
from incidentpilot.store import Store


def run_scenario(scenario, llm=None, **kw):
    """Generate + ingest + triage a scenario; returns (generated, pipeline, store)."""
    gen = generate(scenario, **kw)
    store = Store()
    pipe = Pipeline(store, TriageAgent(store, llm or MockLLM()).investigate)
    for e in gen.events:
        pipe.process_row(e["kind"], e["row"])
    pipe.finish()
    return gen, pipe, store


@pytest.fixture(scope="session")
def deploy_run():
    gen, pipe, store = run_scenario("bad_deploy", seed=3)
    yield gen, pipe, store
    store.close()


@pytest.fixture(scope="session")
def small_store():
    store = Store()
    gen = generate("bad_deploy", seed=3)
    store.insert_logs(e["row"] for e in gen.events if e["kind"] == "log")
    store.insert_metrics(e["row"] for e in gen.events if e["kind"] == "metric")
    yield store
    store.close()
