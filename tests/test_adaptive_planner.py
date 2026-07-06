"""The growth loop: routes improve with executions, candidates grow with skills."""

from __future__ import annotations

import random
from datetime import UTC, datetime

from oolu.knowledge import NodeObservation, TraceStore, route_node_key
from oolu.orchestrator import (
    AdaptivePlanner,
    BoundedRetryRecovery,
    CapabilityGrounder,
    CollectingFeedbackSink,
    DagRouteRunner,
    Phase,
    RiskBasedHumanControl,
    StaticIntaker,
    StatusOutcomeMonitor,
    TaskContract,
    ThompsonRouteOptimizer,
    WorkflowOrchestrator,
)
from oolu.orchestrator.state import SemanticGrounding
from oolu.skills import SkillRegistry, parse_sop
from oolu.skills.models import (
    ActionEvent,
    ExecutionOutcome,
    ExecutionStatus,
    ReusableSkill,
    SkillSignature,
)
from oolu.skills.requirements import (
    AuthorizationGrant,
    AuthorizationMode,
    RequirementBrief,
)


class ModeExecutor:
    """Succeeds or fails per the action's demonstrated ``mode`` parameter."""

    name = "cli"

    def __init__(self):
        self.calls: list[str] = []

    def capabilities(self) -> frozenset[str]:
        return frozenset({"send", "export_data", "publish", "x", "y", "z"})

    def execute(self, action: ActionEvent, *, idempotency_key: str) -> ExecutionOutcome:
        self.calls.append(action.operation)
        ok = action.parameters.get("mode") != "flaky"
        now = datetime.now(UTC)
        return ExecutionOutcome(
            idempotency_key=idempotency_key,
            skill_id="s",
            status=ExecutionStatus.SUCCEEDED if ok else ExecutionStatus.FAILED,
            error=None if ok else "flaky mode failed",
            started_at=now,
            completed_at=now,
        )

    def cancel(self, idempotency_key: str) -> None:  # pragma: no cover - stub
        pass


def _skill(name: str, mode: str, *operations: str) -> ReusableSkill:
    return ReusableSkill(
        id=f"skill.{name}",
        name=name,
        description=name,
        signature=SkillSignature(application="cli", adapter="cli"),
        actions=[
            ActionEvent(
                correlation_id="c",
                adapter="cli",
                operation=op,
                parameters={"mode": mode},
            )
            for op in (operations or ("send",))
        ],
    )


def _brief() -> RequirementBrief:
    return RequirementBrief(
        intent="send the report",
        authorization=AuthorizationGrant(mode=AuthorizationMode.FULLY_DELEGATED),
    )


GROUNDING = SemanticGrounding(
    resolved_capabilities=frozenset({"send", "export_data", "publish"})
)


def test_route_choice_converges_to_the_route_that_works_for_this_user():
    registry = SkillRegistry(":memory:")
    registry.register(_skill("flaky-route", "flaky"), semver="0.1.0")
    registry.register(_skill("solid-route", "solid"), semver="0.1.0")
    store = TraceStore(":memory:")
    planner = AdaptivePlanner(registry, trace_store=store)
    optimizer = ThompsonRouteOptimizer(planner, store, rng=random.Random(3))
    runner = DagRouteRunner({"cli": ModeExecutor()}, trace_store=store)

    chosen: list[str] = []
    for i in range(30):
        route = optimizer.optimize(_brief(), GROUNDING)
        runner.execute(route, idempotency_key=f"run-{i}", attempt=1)
        chosen.append(route.chosen.name)

    # Early on both routes get explored; by the end the user's own history
    # dominates and the reliable route wins.
    assert "flaky-route" in chosen  # it was explored, not hard-coded away
    assert chosen[-10:].count("solid-route") >= 8
    solid = store.posterior(route_node_key("solid-route"))
    flaky = store.posterior(route_node_key("flaky-route"))
    assert solid.mean > flaky.mean
    registry.close()


def test_newly_learned_skills_become_candidate_routes_immediately():
    registry = SkillRegistry(":memory:")
    registry.register(_skill("flaky-route", "flaky"), semver="0.1.0")
    store = TraceStore(":memory:")
    planner = AdaptivePlanner(registry, trace_store=store)
    optimizer = ThompsonRouteOptimizer(planner, store, rng=random.Random(9))
    runner = DagRouteRunner({"cli": ModeExecutor()}, trace_store=store)

    for i in range(5):
        route = optimizer.optimize(_brief(), GROUNDING)
        assert route.chosen.name == "flaky-route"  # the only candidate
        runner.execute(route, idempotency_key=f"pre-{i}", attempt=1)

    # The user teaches a better skill mid-stream: no restart, no retraining.
    registry.register(_skill("solid-route", "solid"), semver="0.1.0")

    chosen: list[str] = []
    for i in range(15):
        route = optimizer.optimize(_brief(), GROUNDING)
        runner.execute(route, idempotency_key=f"post-{i}", attempt=1)
        chosen.append(route.chosen.name)
    assert "solid-route" in chosen
    assert chosen[-5:].count("solid-route") >= 4
    registry.close()


def test_consistent_precedence_promotes_the_route_to_a_parallel_graph():
    registry = SkillRegistry(":memory:")
    registry.register(_skill("pipeline", "solid", "x", "y", "z"), semver="0.1.0")
    store = TraceStore(":memory:")

    # Four observed runs: x always first; y and z alternate (parallel in truth).
    for flip in range(4):
        order = ["y", "z"] if flip % 2 == 0 else ["z", "y"]
        store.record_run(
            goal="pipeline",
            steps=[
                NodeObservation(f"pipeline:cli/{op}", ok=True) for op in ["x", *order]
            ],
            success=True,
        )

    planner = AdaptivePlanner(registry, trace_store=store, min_observations=3)
    (blueprint,) = planner.blueprints()
    assert blueprint.ordering == "graph"
    ops = {item.action.id: item.action.operation for item in blueprint.actions}
    learned = {(ops[e.source], ops[e.target]) for e in blueprint.edges}
    assert learned == {("x", "y"), ("x", "z")}  # y and z are free to run in parallel

    # Without enough evidence the conservative sequential baseline stands.
    fresh = AdaptivePlanner(registry, trace_store=TraceStore(":memory:"))
    (unproven,) = fresh.blueprints()
    assert unproven.ordering == "sequential" and unproven.edges == []
    registry.close()


def test_sops_are_compiled_into_every_matching_blueprint():
    registry = SkillRegistry(":memory:")
    registry.register(
        _skill("monthly", "solid", "export_data", "publish"),
        semver="0.1.0",
        tags=["reporting"],
    )
    sop = parse_sop(
        "sop: month-end\n"
        "applies_to:\n  tags: [reporting]\n"
        "require_order: [export_data, publish]\n"
        "approval:\n  operations: [publish]\n"
    )
    planner = AdaptivePlanner(registry, sops=[sop])
    (blueprint,) = planner.blueprints()
    assert any(e.provenance == "sop" for e in blueprint.edges)
    by_op = {item.action.operation: item for item in blueprint.actions}
    assert by_op["publish"].reserved is True
    registry.close()


def test_full_orchestrator_run_with_the_adaptive_stack():
    registry = SkillRegistry(":memory:")
    registry.register(_skill("solid-route", "solid"), semver="0.1.0")
    store = TraceStore(":memory:")
    planner = AdaptivePlanner(registry, trace_store=store)
    feedback = CollectingFeedbackSink()

    orchestrator = WorkflowOrchestrator(
        intaker=StaticIntaker(_brief()),
        grounder=CapabilityGrounder({}, always_resolved=frozenset({"send"})),
        optimizer=ThompsonRouteOptimizer(planner, store, rng=random.Random(1)),
        human_control=RiskBasedHumanControl(),
        executor=DagRouteRunner({"cli": ModeExecutor()}, trace_store=store),
        monitor=StatusOutcomeMonitor(),
        recovery=BoundedRetryRecovery(),
        feedback=feedback,
    )
    state = orchestrator.start(TaskContract(intent="send the report"))
    assert state.phase is Phase.COMPLETED
    assert state.route is not None and state.route.chosen.name == "solid-route"
    assert feedback.records and feedback.records[0].success is True
    # The run itself grew the statistics the next plan will use.
    assert store.posterior(route_node_key("solid-route")).successes == 1
    registry.close()
