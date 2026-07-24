"""M5: reinforcement route learning — the RL rungs, each gated."""

from __future__ import annotations

import json
import random

from oolu import routelearning as rl
from oolu import skillinduction as si
from oolu.durable.connection import DurableConnection
from oolu.knowledge.traces import NodeObservation, TraceStore
from oolu.memoryspine import MemorySpine
from oolu.orchestrator.assembler import ContractAssembler, GoalSpec
from oolu.orchestrator.replay import earns_its_cost, replay
from oolu.planner.baseline import PlannerProposalModel
from oolu.skills.contract import ActionsBody, NodeContract, Slot
from oolu.skills.models import ActionEvent


def _contract(name: str, produces: str) -> NodeContract:
    return NodeContract(
        id=f"node.{name}",
        name=name,
        produces=[Slot(name=produces, value_type="str", role="result")],
        body=ActionsBody(
            actions=[ActionEvent(correlation_id="c", adapter="cli", operation="run")]
        ),
    )


# ------------------------------------------------------------------ #
# Rung 1 — the dataset and its reward.                                #
# ------------------------------------------------------------------ #
def test_observations_record_verbatim_and_reward_holds_the_verified_bar():
    store = TraceStore(":memory:")
    try:
        rl.observe_route(
            store,
            goal="ship the report",
            route="pdf-route",
            features={"goal_class": "reports", "model": "m-8b"},
            success=True,
            outcome_score=1.0,
            cost=2.0,
            latency=30.0,
            node_versions=("pdf@3",),
            interventions=1,
            reuse_created=2,
        )
        rl.observe_route(
            store,
            goal="ship the report",
            route="pdf-route",
            features={"goal_class": "reports", "model": "m-8b"},
            success=False,
            outcome_score=0.0,
            cost=5.0,
        )
        rows = store.observations(route="pdf-route")
        assert len(rows) == 2
        newest, oldest = rows
        assert oldest.features == {"goal_class": "reports", "model": "m-8b"}
        assert oldest.context_bucket == "goal_class=reports|model=m-8b"
        assert oldest.node_versions == ("pdf@3",)
        # The §20 expression on the verified row; None — never a
        # magnitude — on the unverified one: only verified outcomes teach.
        got = rl.reward(oldest)
        assert got is not None and abs(got - (1.0 - 0.02 - 0.3 - 0.1 + 0.1)) < 1e-9
        assert rl.reward(newest) is None
        # cost_per_verified_state divides ALL spend by VERIFIED outcomes.
        assert rl.cost_per_verified_state(rows) == 7.0
        # Free goal text pre-buckets through the planner's bounded band,
        # and the bucket string stays readable after slugging.
        bucket = rl.context_bucket({"goal_class": rl.goal_class("ship it")})
        assert bucket.startswith("goal_class=goal-")
        assert bucket == rl.context_bucket(
            {"goal_class": rl.goal_class("SHIP it ")}
        )
    finally:
        store.close()


def test_rung_switches_are_config_changes():
    store = TraceStore(":memory:")
    try:
        off = rl.RouteLearningConfig(
            observations_on=False, contextual_bandit_on=False, reranker_on=False
        )
        assert (
            rl.observe_route(
                store, goal="g", route="r", success=True, outcome_score=1.0,
                config=off,
            )
            is None
        )
        assert store.observations() == []
        assert rl.reranker_for(off, store) is None
        # Exploration is off BY DEFAULT — randomness in a chooser is opt-in.
        assert rl.exploration_rng(risk_levels=("read",)) is None
    finally:
        store.close()


# ------------------------------------------------------------------ #
# Rung 2 — the contextual bandit beats the frozen heuristic (the      #
# plan's acceptance bar, measured as route_regret on the same replay).#
# ------------------------------------------------------------------ #
def test_contextual_choice_beats_the_context_free_heuristic_on_replay():
    # Ground truth: route A verifies in desk=csv contexts, route B in
    # desk=pdf — so any context-free chooser must be wrong somewhere,
    # and the contextual posterior must not be.
    truth = {
        ("A", "desk=csv"): 1.0,
        ("A", "desk=pdf"): 0.0,
        ("B", "desk=csv"): 0.0,
        ("B", "desk=pdf"): 1.0,
    }
    store = TraceStore(":memory:")
    try:
        for _ in range(6):
            for desk in ("csv", "pdf"):
                bucket = rl.context_bucket({"desk": desk})
                for route in ("A", "B"):
                    ok = truth[(route, f"desk={desk}")] > 0.5
                    store.record_run(
                        goal=route, steps=[], success=ok, context=bucket
                    )
        contextual_rewards, frozen_rewards, best_rewards = [], [], []
        for desk in ("csv", "pdf"):
            features = {"desk": desk}
            chose = rl.contextual_choice(store, ["A", "B"], features=features)
            frozen = rl.contextual_choice(
                store,
                ["A", "B"],
                features=features,
                config=rl.RouteLearningConfig(contextual_bandit_on=False),
            )
            contextual_rewards.append(truth[(chose, f"desk={desk}")])
            frozen_rewards.append(truth[(frozen, f"desk={desk}")])
            best_rewards.append(1.0)
        contextual = rl.route_regret(contextual_rewards, best_rewards)
        frozen = rl.route_regret(frozen_rewards, best_rewards)
        assert contextual == 0.0
        assert contextual < frozen  # the acceptance bar, strictly
    finally:
        store.close()


def test_thompson_exploration_flows_only_through_the_rung5_door():
    store = TraceStore(":memory:")
    try:
        # With an rng the choice samples (exploration); the rng comes
        # only from exploration_rng, which refuses irreversible risk
        # STRUCTURALLY — no config makes it yield one.
        on = rl.RouteLearningConfig(exploration_on=True)
        rng = rl.exploration_rng(
            risk_levels=("read", "write"), seed=7, config=on
        )
        assert isinstance(rng, random.Random)
        assert (
            rl.exploration_rng(
                risk_levels=("read", "irreversible"), seed=7, config=on
            )
            is None
        )
        # Past the risk budget or the spend cap: greedy floor, no rng.
        assert (
            rl.exploration_rng(
                risk_levels=("write", "write", "write"),
                config=rl.RouteLearningConfig(
                    exploration_on=True, exploration_risk_budget=1.0
                ),
            )
            is None
        )
        assert (
            rl.exploration_rng(
                risk_levels=("read",),
                spent=10.0,
                config=rl.RouteLearningConfig(
                    exploration_on=True, exploration_spend_cap=10.0
                ),
            )
            is None
        )
        # The door's yes-path really does explore the chooser.
        choice = rl.contextual_choice(store, ["A", "B"], rng=rng)
        assert choice in ("A", "B")
    finally:
        store.close()


# ------------------------------------------------------------------ #
# Rung 3 — the reranker behind the ProposalModel port.                #
# ------------------------------------------------------------------ #
def test_reranker_endorses_observed_reward_and_the_assembler_obeys_containment():
    store = TraceStore(":memory:")
    try:
        # Verified reward evidence: 'good' rewards high, 'bad' verifies
        # but rewards low; 'unknown' has no evidence at all.
        for _ in range(3):
            rl.observe_route(
                store, goal="g", route="good", features={"desk": "csv"},
                success=True, outcome_score=1.0,
            )
            rl.observe_route(
                store, goal="g", route="bad", features={"desk": "csv"},
                success=True, outcome_score=0.1,
            )
            rl.observe_route(
                store, goal="g", route="bad", features={"desk": "csv"},
                success=False, outcome_score=0.0,
            )
        model = rl.ObservationReranker(store, features={"desk": "csv"})
        want = Slot(name="out", value_type="str", role="result")
        candidates = [
            _contract("good", "out"),
            _contract("bad", "out"),
            _contract("unknown", "out"),
        ]
        proposal = model.propose(
            goal=GoalSpec(name="g", want=[want]), slot=want,
            selected=[], candidates=candidates,
        )
        assert proposal.weights["node.good"] == 1.0
        assert proposal.weights["node.bad"] == 0.0
        assert "node.unknown" not in proposal.weights  # no evidence, no opinion
        # Plugged into the assembler the advice decides the thin-history
        # tie; unplugged (rollback = the port) the pick is name-order.
        advised = ContractAssembler(candidates, proposal_model=model).assemble(
            GoalSpec(name="g", want=[want])
        )
        assert advised.selected == ["good"]
        bare = ContractAssembler(candidates).assemble(
            GoalSpec(name="g", want=[want])
        )
        assert bare.selected == ["bad"]  # alphabetical: the frozen floor
        # A broken store downgrades to no-advice, never an exception.
        broken = rl.ObservationReranker(store, features={"desk": "csv"})
        store.close()
        crashed = broken.propose(
            goal=GoalSpec(name="g", want=[want]), slot=want,
            selected=[], candidates=candidates,
        )
        assert crashed.weights == {}
    finally:
        try:
            store.close()
        except Exception:
            pass


def test_a_promoted_skill_endorses_its_next_step(tmp_path):
    store = TraceStore(":memory:")
    conn = DurableConnection(tmp_path / "s.db")
    try:
        spine = MemorySpine(conn)
        for goal in ("g1", "g2", "g3", "g4", "g5"):
            store.record_run(
                goal=goal,
                steps=[
                    NodeObservation(node_key="route:extract", ok=True, cost=1.0),
                    NodeObservation(node_key="route:render", ok=True, cost=1.0),
                ],
                success=True,
            )
        si.induce(store, spine, tenant="t1")
        assert si.promote(spine, tenant="t1", motif_key="route:extract→route:render")
        # M4's reader at a live seat: with 'extract' already selected,
        # the candidate completing the promoted motif is endorsed.
        model = rl.ObservationReranker(store, spine=spine, tenant="t1")
        want = Slot(name="out", value_type="str", role="result")
        candidates = [_contract("render", "out"), _contract("elsewhere", "out")]
        proposal = model.propose(
            goal=GoalSpec(name="g", want=[want]), slot=want,
            selected=["extract"], candidates=candidates,
        )
        assert proposal.weights.get("node.render") == rl.SKILL_ENDORSEMENT
        assert "node.elsewhere" not in proposal.weights
    finally:
        conn.close()
        store.close()


# ------------------------------------------------------------------ #
# Rung 4 — the corpus grows; the audition gate stands.                #
# ------------------------------------------------------------------ #
def test_grow_corpus_exports_runs_skills_and_observations(tmp_path):
    store = TraceStore(":memory:")
    conn = DurableConnection(tmp_path / "s.db")
    try:
        spine = MemorySpine(conn)
        for goal in ("g1", "g2", "g3", "g4", "g5"):
            store.record_run(
                goal=goal,
                steps=[
                    NodeObservation(node_key="route:a", ok=True, cost=1.0),
                    NodeObservation(node_key="route:b", ok=True, cost=1.0),
                ],
                success=True,
            )
        si.induce(store, spine, tenant="t1")
        si.promote(spine, tenant="t1", motif_key="route:a→route:b")
        rl.observe_route(
            store, goal="g1", route="a", features={"desk": "csv"},
            success=True, outcome_score=1.0,
        )
        rl.observe_route(
            store, goal="g1", route="a", features={"desk": "csv"},
            success=False, outcome_score=0.0,
        )
        target = tmp_path / "corpus.jsonl"
        counts = rl.grow_corpus(store, target, spine=spine, tenant="t1")
        lines = [
            json.loads(line)
            for line in target.read_text(encoding="utf-8").splitlines()
        ]
        by_source = {"run": [], "skill": [], "observation": []}
        for line in lines:
            by_source[line["source"]].append(line)
        assert counts == {k: len(v) for k, v in by_source.items()}
        assert counts["run"] == 10  # 5 runs x 2 step boundaries
        (skill,) = by_source["skill"]
        assert skill["steps"] == ["route:a", "route:b"]
        assert skill["support"] == 5
        # Observations carry their reward; the unverified one exports
        # with reward null — filtered by the training job, never here.
        rewards = sorted(
            (o["reward"] is None for o in by_source["observation"])
        )
        assert rewards == [False, True]
    finally:
        conn.close()
        store.close()


def test_the_audition_gate_bills_nothing_until_earned():
    # The planner trained live on a consistent corpus must beat the
    # abstaining floor on the SAME replay; the floor itself never earns.
    class Abstain:
        def propose(self, **_kwargs):
            from oolu.orchestrator.assembler import Proposal

            return Proposal(weights={}, cost=0.0)

    seed_store = TraceStore(":memory:")
    try:
        for i in range(30):
            seed_store.record_run(
                goal="process invoices",
                steps=[
                    NodeObservation(node_key="route:invoice-worker", ok=True)
                ],
                success=True,
            )
        runs = list(reversed(seed_store.runs()))
    finally:
        seed_store.close()
    reports = replay(
        runs,
        {
            "planner": lambda store: PlannerProposalModel(store),
            "abstain": lambda store: Abstain(),
        },
        warmup=5,
    )
    assert earns_its_cost(reports["planner"], reports["abstain"])
    assert not earns_its_cost(reports["abstain"], reports["planner"])
