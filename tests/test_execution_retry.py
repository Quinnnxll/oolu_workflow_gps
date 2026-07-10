"""Execution retry, exact-failing-node labelling, and the LLM rebuild.

The user-visible contract under test:

1. When a node fails during execution, the EXACT node is labelled — in the
   execution record, the monitor's summary, the incident, and the pause
   payload the UI reads.
2. The operator can retry an incident; each retry is counted separately
   from automatic recovery.
3. After the user hit Retry twice and the run still fails, the engine calls
   the model out to PLAN the steps and WRITE the code for execution —
   gated on the "Auto-build nodes on my paths" consent, refusing with the
   exact hint when consent is off.
4. A rebuilt route is honest (origin, plan notes) and re-earns human
   confirmation before model-written code runs.
5. The script runner treats a planner-provided script as a proposal:
   verified by execution before it is trusted or cached.
"""

from __future__ import annotations

from oolu.gateway.app import _failure_view, _no_route_view, _plan_view
from oolu.orchestrator import (
    AUTOBUILD_SETTING_LABEL,
    ActionExecutorRouteRunner,
    Blueprint,
    BoundedRetryRecovery,
    CapabilityGrounder,
    CollectingFeedbackSink,
    DagRouteRunner,
    LeastCostRouteOptimizer,
    LLMRouteRebuilder,
    PauseKind,
    Phase,
    RebuildDecision,
    ReservedAction,
    ResumeInput,
    RiskBasedHumanControl,
    StaticIntaker,
    StatusOutcomeMonitor,
    TaskContract,
    WorkflowOrchestrator,
    parse_plan_steps,
)
from oolu.runtime.backend import StubBackend, make_failure, make_success
from oolu.runtime.script_node import NodeScriptRunner
from oolu.skills.models import ActionEvent, ExecutionOutcome, ExecutionStatus
from oolu.skills.requirements import (
    AuthorizationGrant,
    AuthorizationMode,
    ParameterDomain,
    ParameterSource,
    RequirementBrief,
    RequirementParameter,
)


# --------------------------------------------------------------------------- #
# Test doubles.                                                               #
# --------------------------------------------------------------------------- #
class OperationScriptedExecutor:
    """An ``ActionExecutor`` whose named operations always fail."""

    name = "test"

    def __init__(self, capabilities: set[str], *, failing: set[str] = frozenset()):
        self._caps = frozenset(capabilities)
        self._failing = set(failing)
        self.calls: list[str] = []

    def capabilities(self) -> frozenset[str]:
        return self._caps

    def execute(self, action: ActionEvent, *, idempotency_key: str) -> ExecutionOutcome:
        self.calls.append(action.operation)
        failed = action.operation in self._failing
        return ExecutionOutcome(
            idempotency_key=idempotency_key,
            skill_id=action.correlation_id,
            status=ExecutionStatus.FAILED if failed else ExecutionStatus.SUCCEEDED,
            error=f"{action.operation} broke" if failed else None,
        )

    def cancel(self, idempotency_key: str) -> None:
        return None


class FakeChatModel:
    """``reply(messages) -> str`` returning a scripted answer, counting calls."""

    def __init__(self, answer: str):
        self._answer = answer
        self.calls: list[list[dict]] = []

    def reply(self, messages: list[dict]) -> str:
        self.calls.append(messages)
        return self._answer


REBUILD_ANSWER = (
    "1. Compute the answer directly.\n"
    "2. Emit it through the runtime contract.\n"
    "```python\nfrom _oolu_runtime import emit_result\nemit_result(42)\n```"
)


def _param(name: str, value: str) -> RequirementParameter:
    return RequirementParameter(
        name=name,
        description=f"the {name}",
        domain=ParameterDomain(value_type="str"),
        required=True,
        value=value,
        source=ParameterSource.USER,
    )


def _brief(*, delegated: bool = True) -> RequirementBrief:
    return RequirementBrief(
        intent="fetch the report",
        parameters=[_param("target", "report")],
        authorization=AuthorizationGrant(
            mode=(
                AuthorizationMode.FULLY_DELEGATED
                if delegated
                else AuthorizationMode.GUIDED
            ),
            allow_all_unspecified=True,
        ),
    )


def _two_step_blueprint() -> Blueprint:
    return Blueprint(
        name="two-step",
        actions=[
            ReservedAction(
                action=ActionEvent(
                    correlation_id="c1", adapter="test", operation="one"
                ),
                required_capabilities=frozenset({"one"}),
            ),
            ReservedAction(
                action=ActionEvent(
                    correlation_id="c2", adapter="test", operation="two"
                ),
                required_capabilities=frozenset({"two"}),
            ),
        ],
        estimated_cost=1.0,
    )


class CollectingEvents:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def append(self, event_type: str, payload: dict) -> None:
        self.events.append((event_type, payload))

    def types(self) -> list[str]:
        return [t for t, _ in self.events]


def _orchestrator(
    *,
    executor: OperationScriptedExecutor,
    blueprint: Blueprint | None = None,
    delegated: bool = True,
    rebuilder=None,
    extra_executors: dict | None = None,
    events: CollectingEvents | None = None,
) -> WorkflowOrchestrator:
    executors = {"test": executor, **(extra_executors or {})}
    return WorkflowOrchestrator(
        intaker=StaticIntaker(_brief(delegated=delegated)),
        grounder=CapabilityGrounder({"target": "one"}, always_resolved=frozenset({"two"})),
        optimizer=LeastCostRouteOptimizer([blueprint or _two_step_blueprint()]),
        human_control=RiskBasedHumanControl(),
        executor=ActionExecutorRouteRunner(executors),
        monitor=StatusOutcomeMonitor(),
        recovery=BoundedRetryRecovery(),
        feedback=CollectingFeedbackSink(),
        events=events,
        rebuilder=rebuilder,
    )


def _contract() -> TaskContract:
    return TaskContract(
        intent="fetch the report", metadata={"tenant_id": "acme"}
    )


def _fail_to_incident(orch: WorkflowOrchestrator):
    """Start a run that fails, auto-retries once, and escalates."""
    state = orch.start(_contract())
    assert state.is_paused and state.pause.kind is PauseKind.INCIDENT
    return state


def _retry(orch, state):
    return orch.resume(
        state, ResumeInput(kind=PauseKind.INCIDENT, incident_decision="retry")
    )


# --------------------------------------------------------------------------- #
# 1. The exact failing node is labelled everywhere the user looks.             #
# --------------------------------------------------------------------------- #
def test_sequential_runner_labels_the_exact_failing_node():
    executor = OperationScriptedExecutor({"one", "two"}, failing={"two"})
    state = _fail_to_incident(_orchestrator(executor=executor))

    record = state.execution
    assert record.status is ExecutionStatus.FAILED
    assert record.failed_action_label == "test/two"
    assert record.failed_action_id == state.route.chosen.actions[1].action.id
    # The monitor's summary — and therefore the incident — names the node.
    assert "node 'test/two' failed" in state.monitoring.summary
    assert "node 'test/two' failed" in state.latest_incident.reason
    # The pause payload the UI reads carries the label and the retry count.
    assert state.pause.payload["failed_action_label"] == "test/two"
    assert state.pause.payload["user_retries"] == 0


def test_dag_runner_labels_the_exact_failing_node():
    executor = OperationScriptedExecutor({"one", "two"}, failing={"two"})
    runner = DagRouteRunner({"test": executor})
    from oolu.orchestrator.state import RoutePlan

    record = runner.execute(
        RoutePlan(chosen=_two_step_blueprint()), idempotency_key="r:exec:1", attempt=1
    )
    assert record.status is ExecutionStatus.FAILED
    assert record.failed_action_label == "test/two"
    assert record.error == "two broke"


def test_blocked_capability_labels_the_gated_node():
    executor = OperationScriptedExecutor({"one"})  # "two" missing
    runner = ActionExecutorRouteRunner({"test": executor})
    from oolu.orchestrator.state import RoutePlan

    record = runner.execute(
        RoutePlan(chosen=_two_step_blueprint()), idempotency_key="r:exec:1", attempt=1
    )
    assert record.status is ExecutionStatus.BLOCKED
    assert record.failed_action_label == "test/two"


def test_abort_keeps_the_failing_node_in_the_failure_reason():
    executor = OperationScriptedExecutor({"one", "two"}, failing={"two"})
    orch = _orchestrator(executor=executor)
    state = _fail_to_incident(orch)
    state = orch.resume(
        state, ResumeInput(kind=PauseKind.INCIDENT, incident_decision="abort")
    )
    assert state.phase is Phase.FAILED
    assert "node 'test/two' failed" in state.failure_reason


# --------------------------------------------------------------------------- #
# 2 + 3. Retry twice, then the model plans and writes the code.                #
# --------------------------------------------------------------------------- #
def test_llm_rebuild_fires_after_two_user_retries_and_completes():
    executor = OperationScriptedExecutor({"one", "two"}, failing={"two"})
    script_hand = OperationScriptedExecutor({"run"})
    script_hand.name = "script"
    model = FakeChatModel(REBUILD_ANSWER)
    events = CollectingEvents()
    orch = _orchestrator(
        executor=executor,
        rebuilder=LLMRouteRebuilder(model, consent=lambda tenant: True),
        extra_executors={"script": script_hand},
        events=events,
    )

    state = _fail_to_incident(orch)
    state = _retry(orch, state)  # user retry #1 -> fails -> incident again
    assert state.user_retries == 1
    assert state.pause is not None and state.pause.kind is PauseKind.INCIDENT
    state = _retry(orch, state)  # user retry #2 -> fails -> REBUILD

    # The model was consulted once, with the failing node in the context.
    assert len(model.calls) == 1
    assert "test/two" in model.calls[0][1]["content"]
    # The rebuilt route is honest about its provenance and shows the plan.
    assert state.route.chosen.origin == "llm_rebuild"
    assert state.route.chosen.plan_notes == [
        "Compute the answer directly.",
        "Emit it through the runtime contract.",
    ]
    # Fully delegated -> no confirmation; the script hand ran the new code.
    assert state.phase is Phase.COMPLETED
    assert script_hand.calls == ["run"]
    assert state.result["rebuilt"] is True
    assert "workflow.rebuilt" in events.types()


def test_rebuilt_route_requires_confirmation_when_not_delegated():
    executor = OperationScriptedExecutor({"one", "two"}, failing={"two"})
    script_hand = OperationScriptedExecutor({"run"})
    script_hand.name = "script"
    orch = _orchestrator(
        executor=executor,
        delegated=False,
        rebuilder=LLMRouteRebuilder(
            FakeChatModel(REBUILD_ANSWER), consent=lambda tenant: True
        ),
        extra_executors={"script": script_hand},
    )
    state = _fail_to_incident(orch)
    state = _retry(orch, state)
    state = _retry(orch, state)

    # Model-written code re-earns the human's confirmation before running.
    assert state.pause is not None
    assert state.pause.kind is PauseKind.CONFIRMATION
    assert script_hand.calls == []
    state = orch.resume(
        state, ResumeInput(kind=PauseKind.CONFIRMATION, confirmed=True)
    )
    assert state.phase is Phase.COMPLETED
    assert script_hand.calls == ["run"]


def test_rebuild_refuses_without_consent_and_names_the_switch():
    executor = OperationScriptedExecutor({"one", "two"}, failing={"two"})
    model = FakeChatModel(REBUILD_ANSWER)
    orch = _orchestrator(
        executor=executor,
        rebuilder=LLMRouteRebuilder(model, consent=lambda tenant: False),
    )
    state = _fail_to_incident(orch)
    state = _retry(orch, state)
    state = _retry(orch, state)

    # No consent: no model call, an incident whose payload says exactly
    # which Settings switch would unblock the run.
    assert model.calls == []
    assert state.pause is not None and state.pause.kind is PauseKind.INCIDENT
    assert AUTOBUILD_SETTING_LABEL in state.pause.payload["rebuild_refusal"]
    assert state.user_retries == 2


def test_rebuild_refuses_when_the_model_writes_no_code():
    executor = OperationScriptedExecutor({"one", "two"}, failing={"two"})
    orch = _orchestrator(
        executor=executor,
        rebuilder=LLMRouteRebuilder(
            FakeChatModel("I would suggest checking the network."),
            consent=lambda tenant: True,
        ),
    )
    state = _fail_to_incident(orch)
    state = _retry(orch, state)
    state = _retry(orch, state)

    assert state.pause is not None and state.pause.kind is PauseKind.INCIDENT
    assert "no runnable code" in state.pause.payload["rebuild_refusal"]


def test_rebuild_refuses_when_no_script_runtime_exists():
    executor = OperationScriptedExecutor({"one", "two"}, failing={"two"})
    orch = _orchestrator(
        executor=executor,  # no "script" executor registered
        rebuilder=LLMRouteRebuilder(
            FakeChatModel(REBUILD_ANSWER), consent=lambda tenant: True
        ),
    )
    state = _fail_to_incident(orch)
    state = _retry(orch, state)
    state = _retry(orch, state)

    assert state.pause is not None and state.pause.kind is PauseKind.INCIDENT
    assert "missing capabilities: run" in state.pause.payload["rebuild_refusal"]


def test_rebuild_happens_at_most_once_per_run():
    executor = OperationScriptedExecutor({"one", "two"}, failing={"two"})
    # The rebuilt script route ALSO fails -> the engine must not loop.
    script_hand = OperationScriptedExecutor({"run"}, failing={"run"})
    script_hand.name = "script"
    model = FakeChatModel(REBUILD_ANSWER)
    orch = _orchestrator(
        executor=executor,
        rebuilder=LLMRouteRebuilder(model, consent=lambda tenant: True),
        extra_executors={"script": script_hand},
    )
    state = _fail_to_incident(orch)
    state = _retry(orch, state)
    state = _retry(orch, state)

    assert state.rebuild_attempts == 1
    assert len(model.calls) == 1
    assert state.pause is not None and state.pause.kind is PauseKind.INCIDENT
    assert "node 'script/run' failed" in state.latest_incident.reason


def test_parse_plan_steps_reads_numbered_lines_before_the_fence():
    assert parse_plan_steps(REBUILD_ANSWER) == [
        "Compute the answer directly.",
        "Emit it through the runtime contract.",
    ]
    assert parse_plan_steps("no plan\n```python\n1. not a step\n```") == []
    assert parse_plan_steps(None) == []


def test_rebuild_decision_survives_a_raising_rebuilder():
    class ExplodingRebuilder:
        def rebuild(self, **kwargs) -> RebuildDecision:
            raise RuntimeError("boom")

    executor = OperationScriptedExecutor({"one", "two"}, failing={"two"})
    orch = _orchestrator(executor=executor, rebuilder=ExplodingRebuilder())
    state = _fail_to_incident(orch)
    state = _retry(orch, state)
    state = _retry(orch, state)
    assert state.pause is not None and state.pause.kind is PauseKind.INCIDENT
    assert "boom" in state.pause.payload["rebuild_refusal"]


# --------------------------------------------------------------------------- #
# 5. A provided script is a proposal: verified before trusted or cached.       #
# --------------------------------------------------------------------------- #
def _script_action(script: str) -> ActionEvent:
    return ActionEvent(
        correlation_id="rb",
        adapter="script",
        operation="run",
        parameters={"goal": "answer", "script": script, "node_key": "rebuild:answer"},
    )


def test_provided_script_is_verified_then_cached():
    from oolu.cache.store import LocalScriptCache

    backend = StubBackend([make_success({"result": 42}), make_success({"result": 42})])
    runner = NodeScriptRunner(backend, LocalScriptCache(":memory:"))

    outcome = runner.execute(_script_action("emit_result(42)"), idempotency_key="k1")
    assert outcome.status is ExecutionStatus.SUCCEEDED
    assert outcome.evidence["cache"] == "provided"
    # Second run of the same node replays from the cache (a hit, no proposal).
    outcome2 = runner.execute(_script_action("emit_result(42)"), idempotency_key="k2")
    assert outcome2.status is ExecutionStatus.SUCCEEDED
    assert outcome2.evidence["cache"] == "hit"
    assert backend.requests[0].script == "emit_result(42)"


def test_failing_provided_script_is_not_trusted():
    from oolu.cache.store import LocalScriptCache

    backend = StubBackend([make_failure(stderr="Traceback: ValueError: nope")])
    runner = NodeScriptRunner(backend, LocalScriptCache(":memory:"))
    outcome = runner.execute(_script_action("raise ValueError"), idempotency_key="k1")
    assert outcome.status is ExecutionStatus.FAILED
    assert "provided script failed verification" in outcome.error


# --------------------------------------------------------------------------- #
# The gateway views: plan steps, no-route explanation, failing node.           #
# --------------------------------------------------------------------------- #
def test_run_views_show_plan_failure_and_no_route():
    executor = OperationScriptedExecutor({"one", "two"}, failing={"two"})
    orch = _orchestrator(executor=executor)
    state = _fail_to_incident(orch)

    plan = _plan_view(state)
    assert plan["route"] == "two-step"
    assert [s["label"] for s in plan["steps"]] == ["test/one", "test/two"]
    statuses = {s["label"]: s["status"] for s in plan["steps"]}
    assert statuses["test/one"] == "succeeded"
    assert statuses["test/two"] == "failed"
    failing = [s for s in plan["steps"] if s["failed"]]
    assert len(failing) == 1 and failing[0]["label"] == "test/two"

    failure = _failure_view(state)
    assert failure["node_label"] == "test/two"
    assert failure["error"] == "two broke"

    # A run that failed in planning explains why there was no route.
    from oolu.orchestrator.state import RunState

    no_route_state = orch.start(_contract())  # incident, so no view
    assert _no_route_view(no_route_state) is None
    planning_failed = RunState(
        intent="x",
        contract=_contract(),
        phase=Phase.FAILED,
        failure_reason="no viable route: unresolved capabilities: teleport",
    )
    view = _no_route_view(planning_failed)
    assert view["reason"].startswith("no viable route")
