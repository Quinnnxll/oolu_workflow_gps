"""Safety-gated skill recording and deterministic replay runtime."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from .cli_adapter import CliActionExecutor, CliExecutionPolicy
from .compiler import DemonstrationCompiler
from .models import (
    ActionEvent,
    ApprovalRecord,
    ConstraintSeverity,
    ConstraintSpec,
    ConstraintStatus,
    Demonstration,
    ExecutionOutcome,
    ExecutionStatus,
    ReusableSkill,
    SkillSignature,
    StateSnapshot,
)
from .ports import (
    ActionExecutor,
    ApprovalProvider,
    ConstraintValidator,
    EventSink,
    ExecutionStore,
    StateProbe,
)
from .requirements import RequirementBrief, RequirementConstraintCompiler
from .store import InMemoryExecutionStore


class WorkspaceConstraintValidator:
    name = "workspace"

    def validate(
        self,
        constraint: ConstraintSpec,
        *,
        before: StateSnapshot | None,
        after: StateSnapshot,
    ) -> ConstraintSpec:
        if constraint.validator == "workspace.before_fingerprint":
            expected = constraint.evidence.get("expected_fingerprint")
            satisfied = after.fingerprint == expected
            actual = after.fingerprint
        elif constraint.validator == "workspace.expected_artifacts":
            expected_files = constraint.evidence.get("expected_files", {})
            actual_files = after.state.get("files", {})
            satisfied = all(
                actual_files.get(path) == details
                for path, details in expected_files.items()
            )
            actual = {path: actual_files.get(path) for path in expected_files}
        else:
            satisfied = False
            actual = "unsupported validator"
        return constraint.model_copy(
            update={
                "status": (
                    ConstraintStatus.SATISFIED
                    if satisfied
                    else ConstraintStatus.VIOLATED
                ),
                "evidence": {**constraint.evidence, "actual": actual},
            }
        )


class StaticApprovalProvider:
    def __init__(self, approved: bool, *, principal: str = "local-user"):
        self._approved = approved
        self._principal = principal

    def request(self, *, policy: str, scope: dict) -> ApprovalRecord:
        return ApprovalRecord(
            principal=self._principal,
            policy=policy,
            decision="approved" if self._approved else "denied",
            scope=scope,
        )


class InMemoryEventSink:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def append(self, event_type: str, payload: dict) -> None:
        self.events.append((event_type, dict(payload)))


class SafeSkillRuntime:
    def __init__(
        self,
        *,
        executors: dict[str, ActionExecutor],
        validators: dict[str, ConstraintValidator],
        probe: StateProbe,
        approval: ApprovalProvider,
        events: EventSink | None = None,
        outcomes: ExecutionStore | None = None,
    ):
        self._executors = executors
        self._validators = validators
        self._probe = probe
        self._approval = approval
        self._events = events or InMemoryEventSink()
        self._outcomes = outcomes or InMemoryExecutionStore()

    def run(
        self,
        skill: ReusableSkill,
        *,
        idempotency_key: str,
        requirements: RequirementBrief | None = None,
    ) -> ExecutionOutcome:
        completed = self._outcomes.get(skill.id, idempotency_key)
        if completed is not None:
            return completed
        started = datetime.now(UTC)
        required_bindings = [item.name for item in skill.parameters if item.required]
        if required_bindings:
            return self._blocked(
                skill,
                idempotency_key,
                started,
                "skill parameters require binding before execution",
                {"required_parameters": required_bindings},
            )
        if requirements is not None:
            compilation = RequirementConstraintCompiler().compile(requirements)
            if not compilation.can_produce:
                return self._blocked(
                    skill,
                    idempotency_key,
                    started,
                    "requirements are not ready for production",
                    {"requirements": compilation.model_dump(mode="json")},
                )

        before = self._probe.capture()
        preconditions = self._validate(skill.preconditions, before=None, after=before)
        failed_preconditions = self._hard_failures(preconditions)
        if failed_preconditions:
            return self._blocked(
                skill,
                idempotency_key,
                started,
                "hard precondition failed",
                {
                    "preconditions": [
                        item.model_dump(mode="json") for item in preconditions
                    ]
                },
            )

        writes = any(
            bool(action.parameters.get("writes_workspace")) for action in skill.actions
        )
        if writes:
            approval = self._approval.request(
                policy="workspace-write",
                scope={
                    "skill_id": skill.id,
                    "workspace": before.state.get("workspace"),
                },
            )
            if approval.decision != "approved":
                return self._blocked(
                    skill,
                    idempotency_key,
                    started,
                    "workspace write was not approved",
                    {"approval": approval.model_dump(mode="json")},
                )

        action_outcomes: list[ExecutionOutcome] = []
        self._events.append(
            "skill.started", {"skill_id": skill.id, "key": idempotency_key}
        )
        for index, action in enumerate(skill.actions):
            executor = self._executors.get(action.adapter)
            if executor is None or action.operation not in executor.capabilities():
                return self._blocked(
                    skill,
                    idempotency_key,
                    started,
                    f"missing executor capability: {action.adapter}/{action.operation}",
                    {"action_index": index},
                )
            executable_action = action.model_copy(
                update={"parameters": {**action.parameters, "skill_id": skill.id}}
            )
            outcome = executor.execute(
                executable_action, idempotency_key=f"{idempotency_key}:{index}"
            )
            action_outcomes.append(outcome)
            if outcome.status is not ExecutionStatus.SUCCEEDED:
                return self._finished(
                    skill,
                    idempotency_key,
                    started,
                    ExecutionStatus.FAILED,
                    outcome.error or "action failed",
                    action_outcomes,
                )

        after = self._probe.capture()
        validations = self._validate(skill.validators, before=before, after=after)
        if self._hard_failures(validations):
            return self._finished(
                skill,
                idempotency_key,
                started,
                ExecutionStatus.FAILED,
                "result validation failed",
                action_outcomes,
                validations=validations,
            )
        return self._finished(
            skill,
            idempotency_key,
            started,
            ExecutionStatus.SUCCEEDED,
            None,
            action_outcomes,
            validations=validations,
            state_delta={"before": before.fingerprint, "after": after.fingerprint},
        )

    def _validate(
        self,
        constraints: list[ConstraintSpec],
        *,
        before: StateSnapshot | None,
        after: StateSnapshot,
    ) -> list[ConstraintSpec]:
        results: list[ConstraintSpec] = []
        for constraint in constraints:
            namespace = constraint.validator.split(".", 1)[0]
            validator = self._validators.get(namespace)
            if validator is None:
                results.append(
                    constraint.model_copy(update={"status": ConstraintStatus.VIOLATED})
                )
            else:
                results.append(
                    validator.validate(constraint, before=before, after=after)
                )
        return results

    @staticmethod
    def _hard_failures(constraints: list[ConstraintSpec]) -> list[ConstraintSpec]:
        return [
            item
            for item in constraints
            if item.severity is ConstraintSeverity.HARD
            and item.status is not ConstraintStatus.SATISFIED
        ]

    def _blocked(
        self,
        skill: ReusableSkill,
        key: str,
        started: datetime,
        error: str,
        evidence: dict,
    ) -> ExecutionOutcome:
        outcome = ExecutionOutcome(
            idempotency_key=key,
            skill_id=skill.id,
            status=ExecutionStatus.BLOCKED,
            evidence=evidence,
            error=error,
            started_at=started,
            completed_at=datetime.now(UTC),
        )
        self._events.append("skill.blocked", outcome.model_dump(mode="json"))
        self._outcomes.save(outcome)
        return outcome

    def _finished(
        self,
        skill: ReusableSkill,
        key: str,
        started: datetime,
        status: ExecutionStatus,
        error: str | None,
        action_outcomes: list[ExecutionOutcome],
        *,
        validations: list[ConstraintSpec] | None = None,
        state_delta: dict | None = None,
    ) -> ExecutionOutcome:
        outcome = ExecutionOutcome(
            idempotency_key=key,
            skill_id=skill.id,
            status=status,
            state_delta=state_delta or {},
            evidence={
                "actions": [item.model_dump(mode="json") for item in action_outcomes],
                "validations": [
                    item.model_dump(mode="json") for item in validations or []
                ],
            },
            error=error,
            started_at=started,
            completed_at=datetime.now(UTC),
        )
        self._events.append(f"skill.{status.value}", outcome.model_dump(mode="json"))
        self._outcomes.save(outcome)
        return outcome


class CliSkillRecorder:
    def __init__(self, policy: CliExecutionPolicy, probe: StateProbe):
        self._policy = policy
        self._probe = probe
        self._executor = CliActionExecutor(policy)

    def record(
        self,
        argv: list[str],
        *,
        name: str,
        description: str,
        approved_write: bool = False,
    ) -> tuple[ReusableSkill, Demonstration]:
        if not approved_write:
            raise PermissionError(
                "recording a CLI demonstration requires write approval"
            )
        validated_argv = self._policy.validate_argv(argv)
        before = self._probe.capture()
        correlation_id = uuid4().hex
        action = ActionEvent(
            correlation_id=correlation_id,
            adapter="cli",
            operation="run",
            parameters={"argv": validated_argv, "writes_workspace": True},
            actor="local-user",
        )
        outcome = self._executor.execute(
            action, idempotency_key=f"record:{correlation_id}"
        )
        after = self._probe.capture()
        demonstration = Demonstration(
            intent=description,
            actions=[action],
            before=before,
            after=after,
            outcome=outcome.status,
            evidence=outcome.evidence,
            application=validated_argv[0],
        )
        if outcome.status is not ExecutionStatus.SUCCEEDED:
            raise RuntimeError(outcome.error or "demonstrated command failed")
        skill = DemonstrationCompiler().compile_exact(
            demonstration,
            name=name,
            description=description,
            signature=SkillSignature(
                application="cli",
                application_version=None,
                adapter="cli",
                environment_fingerprint=before.fingerprint,
            ),
        )
        return skill, demonstration
