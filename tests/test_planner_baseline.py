"""The baseline: it generates plans, and it plugs the seam without risk."""

from __future__ import annotations

from oolu.knowledge.traces import NodeObservation, TraceStore, route_node_key
from oolu.orchestrator.assembler import ContractAssembler, GoalSpec
from oolu.planner import (
    MarkovPlanner,
    PlannerProposalModel,
    build_vocabulary,
    encode_run,
    export_token_jsonl,
)
from oolu.planner.baseline import MIN_TRAINING_RUNS
from oolu.skills.contract import ActionsBody, NodeContract, Slot
from oolu.skills.models import ActionEvent

CSV = Slot(name="csv", value_type="path", role="result")


def _stub(name: str, *, produces=None) -> NodeContract:
    return NodeContract(
        id=name,
        name=name,
        produces=list(produces or []),
        body=ActionsBody(
            actions=[ActionEvent(correlation_id="c", adapter="cli", operation="run")]
        ),
    )


def _store_with(runs) -> TraceStore:
    store = TraceStore()
    for goal, names, ok in runs:
        steps = [NodeObservation(route_node_key(n), ok=ok) for n in names]
        store.record_run(goal=goal, steps=steps, success=ok, context="")
    return store


def test_planner_generates_the_dominant_verified_plan():
    # A goal reliably accomplished by a three-node sequence: the planner,
    # trained only on the verified runs, regenerates it token by token.
    store = _store_with(
        [("bill run", ["ingest", "validate", "post"], True)] * 8
        + [("bill run", ["ingest", "explode"], False)]
    )
    planner = MarkovPlanner.from_store(store)
    assert planner.plan("bill run") == [
        route_node_key("ingest"),
        route_node_key("validate"),
        route_node_key("post"),
    ]


def test_training_ignores_failed_runs_by_default():
    store = _store_with([("g", ["only_in_failures"], False)] * 5)
    planner = MarkovPlanner.from_store(store)
    # Every run failed, so nothing was learned and the plan is empty.
    assert planner.runs_seen == 0
    assert planner.plan("g") == []


def test_next_distribution_backs_off_to_global_then_abstains_when_untrained():
    store = _store_with([("g", ["a", "b"], True)] * 4)
    planner = MarkovPlanner.from_store(store)
    # First-step distribution favours the observed opener.
    assert route_node_key("a") in planner.next_distribution("g", [])
    # A goal the model never saw still backs off to the global prior (the
    # same global-pool fallback the counting proposal model uses) rather than
    # inventing a guess — the opener the corpus does know leads.
    fallback = planner.next_distribution("a goal never seen", [])
    assert fallback and route_node_key("a") in fallback
    # A model with no training at all has nothing to say at any level.
    assert MarkovPlanner().next_distribution("g", []) == {}


def test_plan_never_loops_even_on_self_referential_history():
    store = _store_with([("g", ["a", "a", "a"], True)] * 5)
    planner = MarkovPlanner.from_store(store)
    plan = planner.plan("g", max_len=50)
    assert plan.count(route_node_key("a")) <= 1  # no repeats, no infinite loop


def test_proposal_model_abstains_until_trained():
    store = _store_with([("g", ["a"], True)] * (MIN_TRAINING_RUNS - 1))
    model = PlannerProposalModel(store)
    proposal = model.propose(
        goal=GoalSpec(name="g", want=[]),
        slot=CSV,
        selected=[],
        candidates=[_stub("a")],
    )
    assert proposal.weights == {}


def test_proposal_model_endorses_the_predicted_next_node():
    store = _store_with([("g", ["opener", "closer"], True)] * 6)
    model = PlannerProposalModel(store)
    proposal = model.propose(
        goal=GoalSpec(name="g", want=[]),
        slot=CSV,
        selected=[],
        candidates=[_stub("opener"), _stub("closer")],
    )
    # It advises on the node it would emit first; weights stay in [0, 1].
    assert "opener" in proposal.weights
    assert all(0.0 <= w <= 1.0 for w in proposal.weights.values())
    assert proposal.cost == 0.0


def test_proposal_model_never_raises_and_drops_unknown_candidates():
    store = _store_with([("g", ["a", "b"], True)] * 6)
    model = PlannerProposalModel(store)
    proposal = model.propose(
        goal=GoalSpec(name="g", want=[]),
        slot=CSV,
        selected=[],
        candidates=[_stub("a"), _stub("stranger-never-in-any-trace")],
    )
    assert "stranger-never-in-any-trace" not in proposal.weights  # no opinion


def test_planner_plugs_the_assembler_as_advisory_only():
    # The generative planner drops into the real ContractAssembler through
    # the ProposalModel seam and assembly still completes — the type system
    # remains authoritative; the planner is a bounded prior.
    store = _store_with([("make csv", ["maker"], True)] * 6)
    maker = _stub("maker", produces=[CSV])
    assembler = ContractAssembler(
        [maker], proposal_model=PlannerProposalModel(store)
    )
    result = assembler.assemble(GoalSpec(name="make csv", want=[CSV]))
    assert result.complete
    assert "maker" in result.selected


def test_corpus_exports_token_ids_with_a_paired_vocabulary(tmp_path):
    store = _store_with([("g", ["a", "b", "c"], True)] * 3)
    count, vocab = export_token_jsonl(store, tmp_path / "corpus.jsonl")
    assert count == 3
    assert (tmp_path / "corpus.jsonl.vocab.json").exists()
    # The exported ids decode back to the recorded node keys through the
    # paired vocabulary (BOS, goal, a, b, c, EOS).
    seq = encode_run(store.runs(limit=1)[0], vocab)
    assert seq.token_ids[0] == vocab.bos_id
    assert seq.token_ids[-1] == vocab.eos_id
    assert vocab.token_of(seq.token_ids[2]) == route_node_key("a")


def test_build_vocabulary_covers_every_node_in_the_corpus():
    store = _store_with(
        [("g1", ["a", "b"], True), ("g2", ["b", "c"], True)]
    )
    vocab = build_vocabulary(store.runs(limit=10))
    assert vocab.node_count == 3  # a, b, c — b counted once
