"""Trace statistics: posteriors, Thompson sampling, precedence -> partial order."""

from __future__ import annotations

import random

from oolu.knowledge import NodeObservation, TraceStore, route_node_key


def _run(store: TraceStore, goal: str, node_keys: list[str], *, ok: bool = True):
    store.record_run(
        goal=goal,
        steps=[NodeObservation(node_key=key, ok=ok) for key in node_keys],
        success=ok,
    )


def test_posteriors_accumulate_and_fall_back_to_global():
    store = TraceStore(":memory:")
    store.record_run(
        goal="g",
        steps=[NodeObservation("n", ok=True), NodeObservation("m", ok=False)],
        success=True,
    )
    assert store.posterior("n").successes == 1
    assert store.posterior("m").failures == 1
    assert store.posterior(route_node_key("g")).successes == 1
    # An unseen context bucket answers from the global bucket, not ignorance.
    assert store.posterior("n", context="office-laptop").successes == 1
    assert store.posterior("never-seen").observations == 0


def test_thompson_sampling_prefers_the_proven_node():
    store = TraceStore(":memory:")
    for _ in range(30):
        store.record_run(
            goal="g", steps=[NodeObservation("good", ok=True)], success=True
        )
        store.record_run(
            goal="g", steps=[NodeObservation("bad", ok=False)], success=False
        )
    rng = random.Random(11)
    wins = sum(
        store.sample_success("good", rng=rng) > store.sample_success("bad", rng=rng)
        for _ in range(100)
    )
    assert wins > 90


def test_cost_ewma_tracks_measured_cost():
    store = TraceStore(":memory:")
    store.record_run(
        goal="g", steps=[NodeObservation("n", ok=True, cost=10.0)], success=True
    )
    store.record_run(
        goal="g", steps=[NodeObservation("n", ok=True, cost=20.0)], success=True
    )
    cost = store.expected_cost("n")
    assert cost is not None and 10.0 < cost < 20.0
    assert store.expected_cost("unknown") is None


def test_derive_edges_recovers_partial_order_from_linear_traces():
    store = TraceStore(":memory:")
    # a always precedes b and c; b and c alternate order (parallel in truth);
    # both always precede z.
    for flip in range(6):
        middle = ["b", "c"] if flip % 2 == 0 else ["c", "b"]
        _run(store, "g", ["a", *middle, "z"])

    edges = set(store.derive_edges(["a", "b", "c", "z"]))
    assert ("a", "b") in edges and ("a", "c") in edges
    assert ("b", "z") in edges and ("c", "z") in edges
    # The inconsistent pair stays parallel — no edge either way.
    assert ("b", "c") not in edges and ("c", "b") not in edges
    # Transitive reduction: the implied a->z edge is dropped.
    assert ("a", "z") not in edges


def test_derive_edges_needs_enough_evidence():
    store = TraceStore(":memory:")
    _run(store, "g", ["a", "b"])  # a single observation is not structure
    assert store.derive_edges(["a", "b"], min_observations=3) == []


def test_failed_steps_do_not_teach_ordering():
    store = TraceStore(":memory:")
    for _ in range(5):
        store.record_run(
            goal="g",
            steps=[NodeObservation("a", ok=False), NodeObservation("b", ok=True)],
            success=False,
        )
    assert store.precedence("a", "b") == (0, 0)


def test_statistics_persist_across_reopen(tmp_path):
    path = tmp_path / "traces.db"
    store = TraceStore(path)
    for _ in range(4):
        _run(store, "g", ["a", "b"])
    store.close()

    reopened = TraceStore(path)
    assert reopened.posterior("a").successes == 4
    assert reopened.precedence("a", "b") == (4, 0)
    assert reopened.derive_edges(["a", "b"]) == [("a", "b")]
    reopened.close()
