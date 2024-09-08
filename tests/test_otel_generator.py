import pytest

from incidentpilot.generator import SCENARIOS, SERVICES, generate, to_records
from incidentpilot.otel import parse_record


def test_roundtrip_preserves_rows():
    gen = generate("credential_stuffing", seed=1, duration=200)
    sample = gen.events[::37]
    for e, rec in zip(sample, to_records(sample)):
        kind, rows = parse_record(rec)
        assert kind == e["kind"]
        orig = e["row"]
        got = rows[0]
        assert got["service"] == orig["service"]
        assert got["ts"] == pytest.approx(orig["ts"], abs=1e-6)
        if kind == "log":
            assert got["message"] == orig["message"]
            assert got["http_status"] == orig["http_status"]
            assert got["client_ip"] == orig["client_ip"]
        else:
            assert got["value"] == pytest.approx(orig["value"])


def test_otlp_shape_follows_semantic_conventions():
    gen = generate("bad_deploy", seed=1, duration=100)
    log = next(r for r in to_records(gen.events) if "logRecord" in r)
    keys = {a["key"] for a in log["resource"]["attributes"]}
    assert {"service.name", "host.name"} <= keys
    assert log["logRecord"]["severityNumber"] in (5, 9, 13, 17)
    assert log["logRecord"]["timeUnixNano"].isdigit()


@pytest.mark.parametrize("bad", [{}, {"resource": {}}, {"resource": {}, "logRecord": {"body": 1}}])
def test_parse_rejects_malformed(bad):
    with pytest.raises(ValueError):
        parse_record(bad)


def test_generation_is_deterministic():
    a = generate("memory_leak", seed=5, duration=120)
    b = generate("memory_leak", seed=5, duration=120)
    c = generate("memory_leak", seed=6, duration=120)
    assert a.events == b.events
    assert a.events != c.events


def test_events_time_ordered_and_cover_all_services():
    gen = generate("disk_full", seed=2)
    ts = [e["row"]["ts"] for e in gen.events]
    assert ts == sorted(ts)
    assert {e["row"]["service"] for e in gen.events} == set(SERVICES)


SIGNATURES = {  # a substring each scenario must put in the logs, never in a healthy run
    "memory_leak": "OutOfMemoryError", "bad_deploy": "NullPointerException",
    "credential_stuffing": "login failed", "db_pool_exhaustion": "connection pool",
    "disk_full": "No space left", "dependency_saturation": "timeout calling",
    "cert_expiry": "x509", "config_change": "invalid configuration",
    "queue_backlog": "consumer lag", "cache_failure": "maxmemory",
    "traffic_spike": "rate limit exceeded", "dns_failure": "no such host",
}


def test_signatures_cover_every_scenario():
    assert set(SIGNATURES) == set(SCENARIOS)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_scenario_signature_appears_only_after_injection(name):
    gen = generate(name, seed=4)
    healthy = generate(None, seed=4)
    sig = SIGNATURES[name].lower()
    hits = [e["row"]["ts"] for e in gen.events
            if e["kind"] == "log" and sig in e["row"]["message"].lower()]
    assert hits and min(hits) >= gen.truth.start_ts
    assert not any(sig in e["row"]["message"].lower() for e in healthy.events if e["kind"] == "log")


def test_unknown_scenario_rejected():
    with pytest.raises(ValueError):
        generate("nope")
