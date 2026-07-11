"""The ProposalModel replay harness — the audition §5 promised.

Exit gate for "must beat this baseline in the replay harness to earn
its inference cost": the harness exists, replays a corpus prequentially
(test-then-train — nothing predicts from its own future), splits cold
(never-seen nodes) from warm, and its verdict function both PASSES the
shipped stack and REJECTS a pretender — a gate that can only say yes
gates nothing.
"""

from __future__ import annotations

from oolu.knowledge.traces import NodeObservation, RecordedRun, route_node_key
from oolu.orchestrator import (
    LearnedProposalStack,
    TinyTransformerProposalModel,
    TraceProposalModel,
    earns_its_cost,
    replay,
    synthetic_semantic_runs,
)
from oolu.orchestrator.assembler import Proposal

CONTENDERS = {
    "counts": lambda store: TraceProposalModel(store),
    "transformer": lambda store: TinyTransformerProposalModel(store),
    "stack": lambda store: LearnedProposalStack(
        TraceProposalModel(store), TinyTransformerProposalModel(store)
    ),
}


class _Mute:
    """A model with no opinion, ever — the honest floor."""

    def propose(self, *, goal, slot, selected, candidates):
        return Proposal(weights={}, cost=0.0)


class _Yes:
    """A pretender: full-throated endorsement of everything."""

    def propose(self, *, goal, slot, selected, candidates):
        return Proposal(weights={c.id: 1.0 for c in candidates}, cost=0.0)


def _run(goal: str, node: str, ok: bool, when: int) -> RecordedRun:
    return RecordedRun(
        goal=goal,
        context="",
        success=ok,
        steps=(NodeObservation(node_key=route_node_key(node), ok=ok),),
        recorded_at=f"t{when:04d}",
    )


# --------------------------------------------------------------------------- #
# The harness itself: prequential, split by coldness, deterministic.            #
# --------------------------------------------------------------------------- #
def test_abstaining_scores_the_neutral_coin_exactly():
    runs = [_run("g", "steady", i % 2 == 0, i) for i in range(20)]
    (report,) = replay(runs, {"mute": lambda store: _Mute()}, warmup=4).values()
    # Every step scored, no opinions spoken, every miss a coin's miss.
    assert report.predictions == 16
    assert report.opinions == 0
    assert report.brier == 0.25
    # Nothing after the first sighting is cold.
    assert report.cold_predictions == 0


def test_cold_and_warm_split_on_first_sighting():
    runs = [_run("g", "early", True, 0)] * 6 + [_run("g", "late", True, 6)]
    (report,) = replay(runs, {"mute": lambda store: _Mute()}, warmup=2).values()
    # "late" appears once, never seen before: exactly one cold point.
    assert report.cold_predictions == 1
    assert report.warm_predictions == 4
    assert report.cold_brier == 0.25


def test_replay_is_deterministic():
    runs = synthetic_semantic_runs()
    first = replay(runs, CONTENDERS)
    again = replay(runs, CONTENDERS)
    assert first == again


# --------------------------------------------------------------------------- #
# The audition: the shipped stack earns its seat; a pretender does not.        #
# --------------------------------------------------------------------------- #
def test_the_stack_beats_the_counting_baseline_on_the_replay():
    reports = replay(synthetic_semantic_runs(), CONTENDERS)
    baseline, stack = reports["counts"], reports["stack"]

    # The baseline abstains on every cold node — the neutral coin.
    assert baseline.cold_brier == 0.25
    assert baseline.opinions < baseline.predictions

    # Warm territory: counts outrank the transformer inside the stack,
    # so the stack never degrades what verified evidence already knows.
    assert stack.warm_brier <= baseline.warm_brier + 1e-9

    # Cold territory — the seat §5 reserved: the stack reads the names
    # counting cannot, and measurably beats the coin.
    assert stack.cold_brier < baseline.cold_brier

    # The gate, end to end.
    assert earns_its_cost(stack, baseline)


def test_the_gate_rejects_a_pretender():
    runs = synthetic_semantic_runs()
    reports = replay(
        runs,
        {"counts": CONTENDERS["counts"], "yes": lambda store: _Yes()},
    )
    # Endorsing everything is confidently wrong on every stranger run —
    # far worse than the baseline. The gate says no.
    assert reports["yes"].brier > reports["counts"].brier
    assert not earns_its_cost(reports["yes"], reports["counts"])
    # And abstaining entirely earns nothing either.
    mute = replay(runs, {"mute": lambda store: _Mute()})["mute"]
    assert not earns_its_cost(mute, reports["counts"])
