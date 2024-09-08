import pytest

from incidentpilot.detector import Detector
from incidentpilot.generator import SCENARIOS, generate


def run_detector(scenario, **kw):
    gen = generate(scenario, **kw)
    det, found = Detector(), []
    for e in gen.events:
        obs = det.observe_log if e["kind"] == "log" else det.observe_metric
        found += obs(e["row"])
    found += det.flush()
    return gen, found


@pytest.mark.parametrize("seed", range(1, 13))
def test_no_false_alarms_on_healthy_traffic(seed):
    _, found = run_detector(None, seed=seed)
    assert found == []


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_scenario_detected_promptly_and_not_early(name):
    gen, found = run_detector(name, seed=21)
    start = gen.truth.start_ts
    assert found, "scenario produced no anomaly"
    assert all(a.window_end > start for a in found)
    assert min(a.window_end for a in found) - start <= 40
    assert gen.truth.service in {a.service for a in found} or name in {"cert_expiry", "dependency_saturation"}


def test_flagged_windows_do_not_poison_baseline():
    # a sustained 10x level shift must keep being flagged, not be absorbed
    det = Detector(window=10, warmup=6)
    out = []
    for w in range(60):
        value = 10.0 if w < 20 else 100.0
        out += det.observe_metric({"ts": w * 10 + 1, "service": "s", "name": "m", "value": value})
    streaks = det._streak[("s", "metric:m")]
    assert len(out) == 1 and streaks > 30


def test_debounce_ignores_single_window_blip():
    det = Detector(window=10, warmup=6, confirm=2)
    out = []
    for w in range(40):
        value = 500.0 if w == 25 else 10.0 + (w % 3) * 0.1
        out += det.observe_metric({"ts": w * 10 + 1, "service": "s", "name": "m", "value": value})
    out += det.flush()
    assert out == []


def test_late_events_are_dropped_not_crashing():
    det = Detector()
    det.observe_metric({"ts": 1000.0, "service": "s", "name": "m", "value": 1})
    det.observe_metric({"ts": 1100.0, "service": "s", "name": "m", "value": 1})
    det.observe_metric({"ts": 1001.0, "service": "s", "name": "m", "value": 1})
    assert det.late_dropped == 1
