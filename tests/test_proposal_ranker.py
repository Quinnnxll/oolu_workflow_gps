"""route-finding-proof.md §5, filled: a small transformer in the
``ProposalModel`` seat.

Exit gate — the three wins the proof reserved the socket for, plus the
honesty the port demands:

- COLD START / CROSS-GOAL: trained on one goal's outcomes, the ranker
  prefers a BRAND-NEW node whose name shares semantics with what worked
  ("tax-form-pdf" kinship) over an equally-new unrelated node, for a
  goal it never saw.
- CONTEXT-CONDITIONED: two tenants with opposite histories get opposite
  advice from their own rankers.
- ADVISORY HONESTY: an untrained model has NO opinion (never noise),
  weights stay in [0, 1], and propose never raises.
- THE STACK: Beta counts outrank the transformer wherever both have an
  opinion; the transformer fills only what counts never saw.
"""

from __future__ import annotations

from oolu.knowledge.traces import NodeObservation, TraceStore
from oolu.orchestrator import (
    GoalSpec,
    LearnedProposalStack,
    TinyTransformerProposalModel,
    TraceProposalModel,
)
from oolu.skills.contract import ActionsBody, NodeContract, Slot
from oolu.skills.models import ActionEvent

PDF = Slot(name="tax_form_pdf", value_type="path", role="result")


def _contract(id: str, name: str) -> NodeContract:
    return NodeContract(
        id=id,
        name=name,
        body=ActionsBody(
            actions=[
                ActionEvent(correlation_id="c", adapter="cli", operation="run")
            ]
        ),
        produces=[PDF],
    )


def _teach(store: TraceStore, *, context: str = "", rounds: int = 30) -> None:
    """One tenant's history: the tax exporter succeeds for tax goals,
    the image resizer fails them."""
    for _ in range(rounds):
        store.record_run(
            goal="export tax form pdf",
            steps=[NodeObservation(node_key="tax-form-exporter", ok=True)],
            success=True,
            context=context,
        )
        store.record_run(
            goal="export tax form pdf",
            steps=[NodeObservation(node_key="image-resizer", ok=False)],
            success=False,
            context=context,
        )


def _propose(model, goal_name: str, candidates: list[NodeContract]):
    return model.propose(
        goal=GoalSpec(name=goal_name, want=[PDF]),
        slot=PDF,
        selected=[],
        candidates=candidates,
    )


def test_cold_start_semantics_generalize_across_goals(tmp_path):
    store = TraceStore(tmp_path / "traces.db")
    try:
        _teach(store)
        ranker = TinyTransformerProposalModel(store)
        # A goal it never saw, and two BRAND-NEW nodes with zero history:
        # the one sharing the working goal's semantics wins the tie.
        kin = _contract("c-kin", "tax-filing-pdf-writer")
        stranger = _contract("c-str", "photo-collage-maker")
        proposal = _propose(
            ranker, "prepare the tax filing document pdf", [kin, stranger]
        )
        assert set(proposal.weights) == {"c-kin", "c-str"}
        assert proposal.weights["c-kin"] > proposal.weights["c-str"]
        assert all(0.0 <= w <= 1.0 for w in proposal.weights.values())
        assert proposal.cost == 0.0
    finally:
        store.close()


def test_context_conditions_the_choice(tmp_path):
    store = TraceStore(tmp_path / "traces.db")
    try:
        # Two tenants, opposite experiences with the same two providers.
        for _ in range(30):
            store.record_run(
                goal="ship the export",
                steps=[NodeObservation(node_key="fast-courier", ok=True)],
                success=True,
                context="tenant-eu",
            )
            store.record_run(
                goal="ship the export",
                steps=[NodeObservation(node_key="slow-barge", ok=False)],
                success=False,
                context="tenant-eu",
            )
            store.record_run(
                goal="ship the export",
                steps=[NodeObservation(node_key="slow-barge", ok=True)],
                success=True,
                context="tenant-us",
            )
            store.record_run(
                goal="ship the export",
                steps=[NodeObservation(node_key="fast-courier", ok=False)],
                success=False,
                context="tenant-us",
            )
        courier = _contract("c-courier", "fast-courier")
        barge = _contract("c-barge", "slow-barge")
        eu = TinyTransformerProposalModel(store, context="tenant-eu")
        us = TinyTransformerProposalModel(store, context="tenant-us")
        eu_view = _propose(eu, "ship the export", [courier, barge]).weights
        us_view = _propose(us, "ship the export", [courier, barge]).weights
        assert eu_view["c-courier"] > eu_view["c-barge"]
        assert us_view["c-barge"] > us_view["c-courier"]
    finally:
        store.close()


def test_an_untrained_ranker_has_no_opinion_and_never_raises(tmp_path):
    store = TraceStore(tmp_path / "traces.db")
    try:
        ranker = TinyTransformerProposalModel(store)
        silent = _propose(ranker, "anything", [_contract("c1", "whatever")])
        assert silent.weights == {}  # noise is worse than no opinion

        # A broken store never breaks assembly: advice is optional.
        class _Boom:
            def runs(self, **kwargs):
                raise RuntimeError("db on fire")

        wounded = TinyTransformerProposalModel(_Boom())  # type: ignore[arg-type]
        assert _propose(wounded, "anything", [_contract("c1", "x")]).weights == {}
    finally:
        store.close()


def test_the_stack_lets_counts_outrank_and_fills_only_the_unseen(tmp_path):
    store = TraceStore(tmp_path / "traces.db")
    try:
        _teach(store)
        # The counting model matches candidates by their ROUTE key, so a
        # reused whole-route node carries its history under route:<name>.
        for _ in range(5):
            store.record_run(
                goal="export tax form pdf",
                steps=[
                    NodeObservation(node_key="route:tax-form-exporter", ok=True)
                ],
                success=True,
            )
        stack = LearnedProposalStack(
            TraceProposalModel(store),
            TinyTransformerProposalModel(store),
        )
        veteran = _contract("c-vet", "tax-form-exporter")  # counted history
        rookie = _contract("c-new", "tax-filing-pdf-writer")  # never seen
        proposal = _propose(stack, "export tax form pdf", [veteran, rookie])
        # The veteran's weight is the Beta mean of its 30 wins — direct
        # evidence, untouched by the ranker.
        counted = _propose(
            TraceProposalModel(store), "export tax form pdf", [veteran]
        ).weights["c-vet"]
        assert proposal.weights["c-vet"] == counted
        # The rookie exists only through the transformer's opinion.
        assert "c-new" in proposal.weights
        assert 0.0 <= proposal.weights["c-new"] <= 1.0
    finally:
        store.close()
