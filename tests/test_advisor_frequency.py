from concurrent.futures import ThreadPoolExecutor
import random

import pytest

from subroute.advisor_frequency import AdvisorFrequencyGate


@pytest.mark.parametrize("target", [0.3, 0.5, 0.7])
@pytest.mark.parametrize("distribution", ["uniform", "mixed", "tied"])
def test_stationary_traffic_approaches_target(target, distribution):
    rng = random.Random(8091)
    gate = AdvisorFrequencyGate()
    selected_scores = []
    rejected_scores = []
    for i in range(5000):
        score = rng.random() if distribution == "uniform" else rng.choice([0.1, 0.1, 0.4, 0.9]) if distribution == "mixed" else 0.5
        decision = gate.choose(f"stationary-{i}", score, target, "policy")
        (selected_scores if decision["consult"] else rejected_scores).append(score)
    stats = gate.snapshot("policy")
    assert abs(stats["observed_rate"] - target) < 0.02
    assert stats["eligible_count"] == 5000
    assert stats["window_count"] == 256
    if distribution != "tied":
        assert sum(selected_scores) / len(selected_scores) > sum(rejected_scores) / len(rejected_scores)


def test_higher_score_has_priority_with_identical_history_and_id():
    low, high = AdvisorFrequencyGate(), AdvisorFrequencyGate()
    history = random.Random(7).sample(range(256), 256)
    for i, value in enumerate(history):
        for gate in (low, high):
            gate.choose(f"history-{i}", value / 255, 0.5, "p")
    lower = low.choose("candidate", 0.1, 0.5, "p")
    higher = high.choose("candidate", 0.9, 0.5, "p")
    assert higher["percentile"] > lower["percentile"]
    assert higher["consult"] and not lower["consult"]


def test_first_request_has_no_synthetic_score_history_and_is_reproducible():
    low, high = AdvisorFrequencyGate(), AdvisorFrequencyGate()
    low_result = low.choose("warmup", 0.0, 0.5, "p")
    high_result = high.choose("warmup", 1.0, 0.5, "p")
    assert low_result["percentile"] == high_result["percentile"]
    assert low_result["consult"] == high_result["consult"]
    assert low_result["eligible_count"] == 1
    replay_a, replay_b = AdvisorFrequencyGate(), AdvisorFrequencyGate()
    for i in range(100):
        a = replay_a.choose(f"replay-{i}", (i % 10) / 10, 0.5, "p")
        b = replay_b.choose(f"replay-{i}", (i % 10) / 10, 0.5, "p")
        assert a == b


def test_duplicate_is_detached_and_does_not_count_again():
    gate = AdvisorFrequencyGate()
    decision = gate.choose("same", 0.8, 0.5, "p")
    expected = dict(decision)
    decision["consult"] = not decision["consult"]
    for i in range(10):
        gate.choose(f"other-{i}", 0.3, 0.5, "p")
    assert gate.choose("same", 0.2, 0.5, "p") == expected
    assert gate.snapshot("p")["eligible_count"] == 11


def test_inflight_policies_and_instances_remain_independent():
    gate = AdvisorFrequencyGate()
    second = AdvisorFrequencyGate()
    for i in range(20):
        gate.choose(f"old-{i}", 0.9, 0.3, "old")
        gate.choose(f"new-{i}", 0.1, 0.7, "new")
    assert gate.snapshot("old")["eligible_count"] == 20
    assert gate.snapshot("new")["eligible_count"] == 20
    assert second.snapshot("old")["eligible_count"] == 0
    gate.choose("old-late", 0.6, 0.3, "old")
    assert gate.snapshot("new")["eligible_count"] == 20


def test_snapshot_is_nonmutating_and_policy_retention_is_bounded():
    gate = AdvisorFrequencyGate()
    empty = gate.snapshot("missing")
    assert empty == {"eligible_count": 0, "selected_count": 0, "observed_rate": 0.0, "window_count": 0}
    for i in range(4):
        gate.choose("first", 0.3, 0.5, f"p{i}")
    gate.snapshot("p0")
    gate.snapshot("missing")
    gate.choose("fifth", 0.3, 0.5, "p4")
    assert gate.snapshot("p0") == empty
    assert gate.snapshot("p1")["eligible_count"] == 1
    assert len(gate._windows) == 4


def test_request_cache_is_bounded_and_recent_duplicates_survive():
    gate = AdvisorFrequencyGate()
    for i in range(1100):
        gate.choose(f"r{i}", 0.6, 0.5, "p")
    assert len(gate._windows["p"].decisions) == 1024
    assert gate.snapshot("p")["window_count"] == 256
    gate.choose("r1099", 0.6, 0.5, "p")
    assert gate.snapshot("p")["eligible_count"] == 1100
    gate.choose("r0", 0.6, 0.5, "p")
    assert gate.snapshot("p")["eligible_count"] == 1101


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -0.01, 1.01, True, "0.5", None])
def test_invalid_scores_leave_state_empty(score):
    gate = AdvisorFrequencyGate()
    with pytest.raises(ValueError):
        gate.choose("id", score, 0.5, "p")
    assert gate.snapshot("p")["eligible_count"] == 0


@pytest.mark.parametrize("target", [0, 1, 0.4, float("nan"), True, "0.5", None])
def test_invalid_targets(target):
    with pytest.raises(ValueError):
        AdvisorFrequencyGate().choose("id", 0.5, target, "p")


@pytest.mark.parametrize("request_id,policy_key", [("", "p"), (None, "p"), ("id", ""), ("id", None)])
def test_invalid_identifiers(request_id, policy_key):
    with pytest.raises(ValueError):
        AdvisorFrequencyGate().choose(request_id, 0.5, 0.5, policy_key)


def test_concurrent_duplicate_requests_count_once():
    gate = AdvisorFrequencyGate()
    with ThreadPoolExecutor(max_workers=12) as pool:
        decisions = list(pool.map(lambda _: gate.choose("same", 0.8, 0.5, "p"), range(100)))
    assert all(decision == decisions[0] for decision in decisions)
    assert gate.snapshot("p")["eligible_count"] == 1


@pytest.mark.parametrize("before,after", [(0.1, 0.9), (0.9, 0.1)])
def test_rate_adapts_after_distribution_shift(before, after):
    gate = AdvisorFrequencyGate()
    for i in range(1000):
        gate.choose(f"before-{i}", before, 0.5, "p")
    decisions = [gate.choose(f"after-{i}", after, 0.5, "p") for i in range(2000)]
    # Count feedback limits the transient burst instead of selecting almost
    # everything/nothing while score history turns over.
    initial_count = sum(d["consult"] for d in decisions[:100])
    assert abs(initial_count - 50) <= 17
    settled_rate = sum(d["consult"] for d in decisions[256:]) / len(decisions[256:])
    assert abs(settled_rate - 0.5) < 0.04
@pytest.mark.parametrize("target", [.3, .5, .7])
@pytest.mark.parametrize("pattern", ["ascending", "descending", "constant", "blocks", "adaptive"])
def test_target_count_error_is_bounded_even_under_adversarial_drift(target, pattern):
    gate = AdvisorFrequencyGate()
    last_selected = False
    for i in range(3000):
        score = {
            "ascending": i / 3000,
            "descending": 1 - i / 3000,
            "constant": .5,
            "blocks": .9 if (i // 100) % 2 else .1,
            "adaptive": 0.0 if last_selected else 1.0,
        }[pattern]
        result = gate.choose(f"adversarial-{i}", score, target, pattern)
        last_selected = result["consult"]
        error = result["selected_count"] - target * result["eligible_count"]
        assert -16 * (1 - target) - 1e-9 <= error <= 16 * target + 1 + 1e-9
    assert abs(result["observed_rate"] - target) < .005
