"""End-to-end tests for the unified orchestrator (ADR-0002).

Covers the five workflow shapes the branch must support — autonomous, confirmed,
dual-approved, recovered, and escalated — plus the two structural guarantees:
the run state survives serialization across every supported pause, and no
execution path can bypass the preflight controls or capability checks.
"""

from __future__ import annotations

import pytest

from oolu.orchestrator import (
    ActionExecutorRouteRunner,
    Blueprint,
    BoundedRetryRecovery,
    CapabilityGrounder,
    CollectingFeedbackSink,
    LeastCostRouteOptimizer,
    PauseKind,
    Phase,
    PreflightError,
    ReservedAction,
    ResumeInput,
    RiskBasedHumanControl,
    RunState,
    StaticIntaker,
    StatusOutcomeMonitor,
    TaskContract,
    WorkflowOrchestrator,
)
from oolu.skills.models import (
    ActionEvent,
    ApprovalRecord,
    ExecutionOutcome,
    ExecutionStatus,
)
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
class ScriptedActionExecutor:
    """An ``ActionExecutor`` that fails its first ``fail_times`` calls."""

    name = "test"

    def __init__(self, capabilities: set[str], *, fail_times: int = 0):
        self._caps = frozenset(capabilities)
        self._fail_times = fail_times
        self.calls = 0

    def capabilities(self) -> frozenset[str]:
        return self._caps

    def execute(self, action: ActionEvent, *, idempotency_key: str) -> ExecutionOutcome:
        self.calls += 1
        if self.calls <= self._fail_times:
            return ExecutionOutcome(
                idempotency_key=idempotency_key,
                skill_id=action.correlation_id,
                status=ExecutionStatus.FAILED,
                error="transient failure",
            )
        return ExecutionOutcome(
            idempotency_key=idempotency_key,
            skill_id=action.correlation_id,
            status=ExecutionStatus.SUCCEEDED,
        )

    def cancel(self, idempotency_key: str) -> None:
        return None


def _roundtrip(state: RunState) -> RunState:
    """Serialize and reload — proves the run state alone carries the workflow."""
    return RunState.model_validate_json(state.model_dump_json())


def _param(name: str, *, value=None) -> RequirementParameter:
    if value is None:
        return RequirementParameter(
            name=name,
            description=f"the {name}",
            domain=ParameterDomain(value_type="str"),
            required=True,
            suggested_values=["a", "b"],
            question=f"What should {name} be?",
            question_priority=1,
        )
    return RequirementParameter(
        name=name,
        description=f"the {name}",
        domain=ParameterDomain(value_type="str"),
        required=True,
        value=value,
        source=ParameterSource.USER,
    )


def _blueprint(
    *, operation: str, capability: str, reserved: bool, risk: str
) -> Blueprint:
    action = ActionEvent(correlation_id="c1", adapter="test", operation=operation)
    return Blueprint(
        name=f"{operation}-route",
        actions=[
            ReservedAction(
                action=action,
                required_capabilities=frozenset({capability}),
                reserved=reserved,
                risk=risk,
            )
        ],
        estimated_cost=1.0,
    )


def _orchestrator(
    *,
    brief: RequirementBrief,
    blueprint: Blueprint,
    executor: ScriptedActionExecutor,
    grounding_map: dict[str, str],
    dual_risks: frozenset[str] = frozenset(),
) -> WorkflowOrchestrator:
    return WorkflowOrchestrator(
        intaker=StaticIntaker(brief),
        grounder=CapabilityGrounder(grounding_map),
        optimizer=LeastCostRouteOptimizer([blueprint]),
        human_control=RiskBasedHumanControl(dual_approval_risks=dual_risks),
        executor=ActionExecutorRouteRunner({"test": executor}),
        monitor=StatusOutcomeMonitor(),
        recovery=BoundedRetryRecovery(),
        feedback=CollectingFeedbackSink(),
    )


# --------------------------------------------------------------------------- #
# 1. Autonomous: read-only, fully delegated — no pause, straight to done.      #
# --------------------------------------------------------------------------- #
def test_autonomous_workflow_completes_without_pause():
    brief = RequirementBrief(
        intent="summarize",
        parameters=[_param("format")],  # missing but delegated below
        authorization=AuthorizationGrant(
            mode=AuthorizationMode.FULLY_DELEGATED, allow_all_unspecified=True
        ),
    )
    orch = _orchestrator(
        brief=brief,
        blueprint=_blueprint(
            operation="render", capability="render", reserved=False, risk="read"
        ),
        executor=ScriptedActionExecutor({"render"}),
        grounding_map={"format": "render"},
    )

    state = orch.start(TaskContract(intent="summarize"))

    assert state.phase is Phase.COMPLETED
    assert not state.is_paused
    assert state.human_control is not None
    assert not state.human_control.requires_confirmation
    assert not state.human_control.requires_approval
    assert state.execution.status is ExecutionStatus.SUCCEEDED
    assert state.feedback is not None and state.feedback.success


# --------------------------------------------------------------------------- #
# 2. Confirmed: a write route pauses for confirmation.                         #
# --------------------------------------------------------------------------- #
def test_confirmed_workflow_pauses_then_completes():
    brief = RequirementBrief(
        intent="apply change",
        parameters=[_param("target", value="prod")],
        authorization=AuthorizationGrant(mode=AuthorizationMode.GUIDED),
    )
    orch = _orchestrator(
        brief=brief,
        blueprint=_blueprint(
            operation="write", capability="write", reserved=False, risk="write"
        ),
        executor=ScriptedActionExecutor({"write"}),
        grounding_map={"target": "write"},
    )

    state = orch.start(TaskContract(intent="apply change"))
    assert state.is_paused
    assert state.pause.kind is PauseKind.CONFIRMATION
    assert state.phase is Phase.CONFIRMATION

    state = _roundtrip(state)  # survive serialization across the pause
    state = orch.resume(state, ResumeInput(kind=PauseKind.CONFIRMATION, confirmed=True))

    assert state.phase is Phase.COMPLETED
    assert state.confirmation.confirmed
    assert state.execution.status is ExecutionStatus.SUCCEEDED


def test_declined_confirmation_cancels():
    brief = RequirementBrief(
        intent="apply change",
        parameters=[_param("target", value="prod")],
        authorization=AuthorizationGrant(mode=AuthorizationMode.GUIDED),
    )
    orch = _orchestrator(
        brief=brief,
        blueprint=_blueprint(
            operation="write", capability="write", reserved=False, risk="write"
        ),
        executor=ScriptedActionExecutor({"write"}),
        grounding_map={"target": "write"},
    )
    state = orch.start(TaskContract(intent="apply change"))
    state = orch.resume(
        state, ResumeInput(kind=PauseKind.CONFIRMATION, confirmed=False)
    )
    assert state.phase is Phase.CANCELLED
    assert state.execution is None  # never executed


# --------------------------------------------------------------------------- #
# 3. Dual-approved: a reserved irreversible action needs two approvals.        #
# --------------------------------------------------------------------------- #
def test_dual_approved_workflow_requires_two_approvals():
    brief = RequirementBrief(
        intent="delete dataset",
        parameters=[_param("dataset", value="logs")],
        authorization=AuthorizationGrant(mode=AuthorizationMode.FULLY_DELEGATED),
    )
    orch = _orchestrator(
        brief=brief,
        blueprint=_blueprint(
            operation="delete",
            capability="delete",
            reserved=True,
            risk="irreversible",
        ),
        executor=ScriptedActionExecutor({"delete"}),
        grounding_map={"dataset": "delete"},
        dual_risks=frozenset({"irreversible"}),
    )

    state = orch.start(TaskContract(intent="delete dataset"))
    assert state.pause.kind is PauseKind.APPROVAL
    assert state.human_control.approvers_required == 2

    # One approval is not enough.
    state = _roundtrip(state)
    state = orch.resume(
        state,
        ResumeInput(
            kind=PauseKind.APPROVAL,
            approvals=[
                ApprovalRecord(principal="alice", policy="delete", decision="approved")
            ],
        ),
    )
    assert state.pause.kind is PauseKind.APPROVAL  # still waiting on a second

    state = _roundtrip(state)
    state = orch.resume(
        state,
        ResumeInput(
            kind=PauseKind.APPROVAL,
            approvals=[
                ApprovalRecord(principal="bob", policy="delete", decision="approved")
            ],
        ),
    )
    assert state.phase is Phase.COMPLETED
    assert len(state.granted_approvals) == 2


def test_denied_approval_cancels():
    brief = RequirementBrief(
        intent="delete dataset",
        parameters=[_param("dataset", value="logs")],
        authorization=AuthorizationGrant(mode=AuthorizationMode.FULLY_DELEGATED),
    )
    orch = _orchestrator(
        brief=brief,
        blueprint=_blueprint(
            operation="delete", capability="delete", reserved=True, risk="irreversible"
        ),
        executor=ScriptedActionExecutor({"delete"}),
        grounding_map={"dataset": "delete"},
    )
    state = orch.start(TaskContract(intent="delete dataset"))
    state = orch.resume(
        state,
        ResumeInput(
            kind=PauseKind.APPROVAL,
            approvals=[
                ApprovalRecord(principal="alice", policy="delete", decision="denied")
            ],
        ),
    )
    assert state.phase is Phase.CANCELLED
    assert state.execution is None


# --------------------------------------------------------------------------- #
# 4. Recovered: first execution fails, automatic retry succeeds.              #
# --------------------------------------------------------------------------- #
def test_recovered_workflow_retries_and_completes():
    brief = RequirementBrief(
        intent="sync",
        parameters=[_param("source", value="s3")],
        authorization=AuthorizationGrant(mode=AuthorizationMode.FULLY_DELEGATED),
    )
    executor = ScriptedActionExecutor({"sync"}, fail_times=1)
    orch = _orchestrator(
        brief=brief,
        blueprint=_blueprint(
            operation="sync", capability="sync", reserved=False, risk="read"
        ),
        executor=executor,
        grounding_map={"source": "sync"},
    )

    state = orch.start(TaskContract(intent="sync"), max_recovery_attempts=1)

    assert state.phase is Phase.COMPLETED
    assert state.recovery_attempts == 1
    assert executor.calls == 2  # failed once, then succeeded
    assert not state.incidents


# --------------------------------------------------------------------------- #
# 5. Escalated: recovery exhausted -> incident -> operator decision.           #
# --------------------------------------------------------------------------- #
def test_escalated_workflow_pauses_on_incident_and_aborts():
    brief = RequirementBrief(
        intent="sync",
        parameters=[_param("source", value="s3")],
        authorization=AuthorizationGrant(mode=AuthorizationMode.FULLY_DELEGATED),
    )
    executor = ScriptedActionExecutor({"sync"}, fail_times=99)
    orch = _orchestrator(
        brief=brief,
        blueprint=_blueprint(
            operation="sync", capability="sync", reserved=False, risk="read"
        ),
        executor=executor,
        grounding_map={"source": "sync"},
    )

    state = orch.start(TaskContract(intent="sync"), max_recovery_attempts=1)
    assert state.pause.kind is PauseKind.INCIDENT
    assert state.latest_incident is not None and state.latest_incident.escalated

    state = _roundtrip(state)
    state = orch.resume(
        state, ResumeInput(kind=PauseKind.INCIDENT, incident_decision="abort")
    )
    assert state.phase is Phase.FAILED
    assert state.latest_incident.resolution == "aborted"


def test_escalated_workflow_operator_retry_can_succeed():
    brief = RequirementBrief(
        intent="sync",
        parameters=[_param("source", value="s3")],
        authorization=AuthorizationGrant(mode=AuthorizationMode.FULLY_DELEGATED),
    )
    # Fails on the two automatic attempts, succeeds on the operator-driven retry.
    executor = ScriptedActionExecutor({"sync"}, fail_times=2)
    orch = _orchestrator(
        brief=brief,
        blueprint=_blueprint(
            operation="sync", capability="sync", reserved=False, risk="read"
        ),
        executor=executor,
        grounding_map={"source": "sync"},
    )
    state = orch.start(TaskContract(intent="sync"), max_recovery_attempts=1)
    assert state.pause.kind is PauseKind.INCIDENT
    state = orch.resume(
        state, ResumeInput(kind=PauseKind.INCIDENT, incident_decision="retry")
    )
    assert state.phase is Phase.COMPLETED
    assert state.latest_incident.resolution == "retried"
    assert executor.calls == 3


# --------------------------------------------------------------------------- #
# Clarification + full serialization survival across every pause kind.         #
# --------------------------------------------------------------------------- #
def test_workflow_survives_every_pause_via_serialization():
    brief = RequirementBrief(
        intent="provision",
        parameters=[_param("size")],  # unresolved, guided -> clarification pause
        authorization=AuthorizationGrant(mode=AuthorizationMode.GUIDED),
    )
    # Two automatic attempts fail, operator retry succeeds.
    executor = ScriptedActionExecutor({"apply"}, fail_times=2)
    orch = _orchestrator(
        brief=brief,
        blueprint=_blueprint(
            operation="apply", capability="apply", reserved=True, risk="irreversible"
        ),
        executor=executor,
        grounding_map={"size": "apply"},
    )

    # Clarification.
    state = orch.start(TaskContract(intent="provision"), max_recovery_attempts=1)
    assert state.pause.kind is PauseKind.CLARIFICATION
    state = _roundtrip(state)
    state = orch.resume(
        state, ResumeInput(kind=PauseKind.CLARIFICATION, answers={"size": "large"})
    )

    # Confirmation.
    assert state.pause.kind is PauseKind.CONFIRMATION
    state = _roundtrip(state)
    state = orch.resume(state, ResumeInput(kind=PauseKind.CONFIRMATION, confirmed=True))

    # Approval.
    assert state.pause.kind is PauseKind.APPROVAL
    state = _roundtrip(state)
    state = orch.resume(
        state,
        ResumeInput(
            kind=PauseKind.APPROVAL,
            approvals=[
                ApprovalRecord(principal="alice", policy="apply", decision="approved")
            ],
        ),
    )

    # Incident (both automatic attempts failed) -> operator retries to success.
    assert state.pause.kind is PauseKind.INCIDENT
    state = _roundtrip(state)
    state = orch.resume(
        state, ResumeInput(kind=PauseKind.INCIDENT, incident_decision="retry")
    )

    assert state.phase is Phase.COMPLETED
    # The resolved answer, confirmation, approval, and incident all survived.
    resolved = {p.name: p.value for p in state.brief.parameters}
    assert resolved["size"] == "large"
    assert state.confirmation.confirmed
    assert len(state.granted_approvals) == 1
    assert state.latest_incident.resolution == "retried"


# --------------------------------------------------------------------------- #
# Preflight: no execution path bypasses the controls or capability checks.     #
# --------------------------------------------------------------------------- #
def _drive_to_execution_gate(executor: ScriptedActionExecutor) -> tuple:
    brief = RequirementBrief(
        intent="delete dataset",
        parameters=[_param("dataset", value="logs")],
        authorization=AuthorizationGrant(mode=AuthorizationMode.FULLY_DELEGATED),
    )
    orch = _orchestrator(
        brief=brief,
        blueprint=_blueprint(
            operation="delete", capability="delete", reserved=True, risk="irreversible"
        ),
        executor=executor,
        grounding_map={"dataset": "delete"},
    )
    state = orch.start(TaskContract(intent="delete dataset"))
    assert state.pause.kind is PauseKind.APPROVAL
    return orch, state


def test_execution_without_required_approval_is_blocked():
    orch, state = _drive_to_execution_gate(ScriptedActionExecutor({"delete"}))
    # Tamper: force the phase to EXECUTION without recording the approval.
    state.pause = None
    state.phase = Phase.EXECUTION
    with pytest.raises(PreflightError) as excinfo:
        orch.step(state)
    assert any("approval" in reason.lower() for reason in excinfo.value.reasons)


def test_execution_with_missing_capability_is_blocked():
    # Executor lacks the "delete" capability the route requires. Approval is fully
    # granted, so only the capability gate can stop execution — and it must.
    orch, state = _drive_to_execution_gate(ScriptedActionExecutor({"read"}))
    with pytest.raises(PreflightError) as excinfo:
        orch.resume(
            state,
            ResumeInput(
                kind=PauseKind.APPROVAL,
                approvals=[
                    ApprovalRecord(
                        principal="alice", policy="delete", decision="approved"
                    )
                ],
            ),
        )
    assert any("capabilit" in reason.lower() for reason in excinfo.value.reasons)


def test_preflight_guard_rejects_excluded_route_directly():
    orch, state = _drive_to_execution_gate(ScriptedActionExecutor({"delete"}))
    state.pause = None
    state.phase = Phase.EXECUTION
    # Mark the chosen route excluded; the guard must refuse it.
    state.route = state.route.model_copy(
        update={
            "chosen": state.route.chosen.model_copy(
                update={"excluded": True, "exclusion_reason": "policy"}
            )
        }
    )
    with pytest.raises(PreflightError):
        orch.assert_execution_preflight(state)


# --------------------------------------------------------------------------- #
# Durable run-state store: a paused run survives a close/reopen (a restart).   #
# --------------------------------------------------------------------------- #
def test_paused_run_survives_durable_store_reopen(tmp_path):
    from oolu.orchestrator import LocalRunStateStore

    brief = RequirementBrief(
        intent="apply change",
        parameters=[_param("target", value="prod")],
        authorization=AuthorizationGrant(mode=AuthorizationMode.GUIDED),
    )
    orch = _orchestrator(
        brief=brief,
        blueprint=_blueprint(
            operation="write", capability="write", reserved=False, risk="write"
        ),
        executor=ScriptedActionExecutor({"write"}),
        grounding_map={"target": "write"},
    )
    paused = orch.start(TaskContract(intent="apply change"))
    assert paused.pause.kind is PauseKind.CONFIRMATION

    db = tmp_path / "workflows.db"
    store = LocalRunStateStore(db)
    store.save(paused)
    store.close()

    # Reopen as a fresh process would, reload, and resume to completion.
    reopened = LocalRunStateStore(db)
    loaded = reopened.get(paused.run_id)
    reopened.close()
    assert loaded is not None
    assert loaded.pause.kind is PauseKind.CONFIRMATION

    done = orch.resume(loaded, ResumeInput(kind=PauseKind.CONFIRMATION, confirmed=True))
    assert done.phase is Phase.COMPLETED


def test_cli_workflow_status_reports_paused_run(tmp_path):
    import io

    from oolu.cli import main
    from oolu.orchestrator import LocalRunStateStore

    brief = RequirementBrief(
        intent="apply change",
        parameters=[_param("target", value="prod")],
        authorization=AuthorizationGrant(mode=AuthorizationMode.GUIDED),
    )
    orch = _orchestrator(
        brief=brief,
        blueprint=_blueprint(
            operation="write", capability="write", reserved=False, risk="write"
        ),
        executor=ScriptedActionExecutor({"write"}),
        grounding_map={"target": "write"},
    )
    paused = orch.start(TaskContract(intent="apply change"))
    db = tmp_path / "workflows.db"
    store = LocalRunStateStore(db)
    store.save(paused)
    store.close()

    out = io.StringIO()
    code = main(["workflow-status", paused.run_id, "--workflow-db", str(db)], out=out)
    assert code == 0
    rendered = out.getvalue()
    assert paused.run_id in rendered
    assert "confirmation" in rendered

    listing = io.StringIO()
    code = main(["workflow-list", "--workflow-db", str(db), "--json"], out=listing)
    assert code == 0
    assert paused.run_id in listing.getvalue()
