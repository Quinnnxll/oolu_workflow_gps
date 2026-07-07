"""The unified workflow orchestrator: a deterministic, resumable phase machine.

``WorkflowOrchestrator`` drives one ``RunState`` through the unified flow

    intake -> clarification -> grounding -> route optimization
    -> human control -> confirmation -> approval -> execution
    -> monitoring -> (recovery | incident) -> finalization

``step()`` executes exactly the phase named by ``state.phase``; phases advance
only by passing their own gate, so there is no path that jumps to execution.
Human-in-the-loop waits are explicit: a phase that needs input sets ``pause`` and
returns, and ``resume()`` folds the matching input back in. ``EXECUTION`` always
re-derives every preflight control from the recorded sub-records, so safety is a
property of the state rather than of any single call site (ADR-0002).
"""

from __future__ import annotations

from datetime import UTC, datetime

from ..skills.models import ExecutionStatus
from ..skills.ports import EventSink
from ..skills.requirements import (
    BriefStatus,
    ParameterSource,
    RequirementConstraintCompiler,
)
from .ports import (
    FeedbackSink,
    Grounder,
    HumanControlPolicy,
    Intaker,
    OutcomeMonitor,
    PreflightError,
    RecoveryPolicy,
    ResumeError,
    RouteOptimizer,
    WorkflowExecutor,
)
from .state import (
    ConfirmationRecord,
    Incident,
    PauseKind,
    PauseToken,
    Phase,
    PhaseTransition,
    ResumeInput,
    RunState,
    TaskContract,
)

_BLOCKING_BRIEF_STATUSES = frozenset({BriefStatus.CLARIFYING, BriefStatus.BLOCKED})


class WorkflowOrchestrator:
    def __init__(
        self,
        *,
        intaker: Intaker,
        grounder: Grounder,
        optimizer: RouteOptimizer,
        human_control: HumanControlPolicy,
        executor: WorkflowExecutor,
        monitor: OutcomeMonitor,
        recovery: RecoveryPolicy,
        feedback: FeedbackSink,
        events: EventSink | None = None,
        compiler: RequirementConstraintCompiler | None = None,
    ):
        self._intaker = intaker
        self._grounder = grounder
        self._optimizer = optimizer
        self._human_control = human_control
        self._executor = executor
        self._monitor = monitor
        self._recovery = recovery
        self._feedback = feedback
        self._events = events
        self._compiler = compiler or RequirementConstraintCompiler()

    # ------------------------------------------------------------------ #
    # Public API.                                                         #
    # ------------------------------------------------------------------ #
    def start(
        self, contract: TaskContract, *, max_recovery_attempts: int = 1
    ) -> RunState:
        state = RunState(
            intent=contract.intent,
            contract=contract,
            max_recovery_attempts=max_recovery_attempts,
        )
        self._emit("workflow.started", {"run_id": state.run_id, "intent": state.intent})
        return self.run(state)

    def run(self, state: RunState) -> RunState:
        """Advance the run until it pauses or reaches a terminal phase."""
        while not state.is_paused and not state.is_terminal:
            state = self.step(state)
        return state

    def step(self, state: RunState) -> RunState:
        """Execute exactly the phase named by ``state.phase`` once."""
        if state.is_paused:
            raise ResumeError("run is paused; call resume() before stepping")
        if state.is_terminal:
            return state
        handler = self._handlers().get(state.phase)
        if handler is None:
            raise PreflightError([f"no handler for phase {state.phase.value}"])
        return handler(state)

    def resume(self, state: RunState, resume: ResumeInput) -> RunState:
        """Fold human input into a paused run, then continue."""
        if state.pause is None:
            raise ResumeError("run is not paused")
        if resume.kind is not state.pause.kind:
            raise ResumeError(
                f"resume kind {resume.kind.value} does not match pause "
                f"{state.pause.kind.value}"
            )
        applier = {
            PauseKind.CLARIFICATION: self._resume_clarification,
            PauseKind.CONFIRMATION: self._resume_confirmation,
            PauseKind.APPROVAL: self._resume_approval,
            PauseKind.INCIDENT: self._resume_incident,
        }[resume.kind]
        state = applier(state, resume)
        state.pause = None
        state.updated_at = datetime.now(UTC)
        self._emit(
            "workflow.resumed", {"run_id": state.run_id, "kind": resume.kind.value}
        )
        return self.run(state)

    # ------------------------------------------------------------------ #
    # Hard preflight guard (re-derived from recorded state every time).   #
    # ------------------------------------------------------------------ #
    def assert_execution_preflight(self, state: RunState) -> None:
        reasons: list[str] = []

        if (
            state.compilation is None
            or state.compilation.status in _BLOCKING_BRIEF_STATUSES
        ):
            reasons.append("requirements are not resolved")
        if state.grounding is None:
            reasons.append("semantic grounding is missing")
        elif state.grounding.unresolved_terms:
            reasons.append(
                "unresolved grounding terms: "
                + ", ".join(state.grounding.unresolved_terms)
            )
        if state.route is None:
            reasons.append("no route was chosen")
        elif state.route.chosen.excluded:
            reasons.append(
                "chosen route is excluded: "
                + (state.route.chosen.exclusion_reason or "unknown")
            )
        if state.human_control is None:
            reasons.append("human control was not evaluated")
        else:
            hc = state.human_control
            if hc.requires_confirmation and not (
                state.confirmation and state.confirmation.confirmed
            ):
                reasons.append("confirmation required but not granted")
            if hc.requires_approval:
                granted = len(state.granted_approvals)
                if granted < hc.approvers_required:
                    reasons.append(
                        f"approvals required: {hc.approvers_required}, granted: {granted}"
                    )

        # Capability check: every action's required capabilities must be available.
        if state.route is not None:
            available = self._executor.capabilities()
            for item in state.route.chosen.actions:
                missing = set(item.required_capabilities) - set(available)
                if missing:
                    reasons.append(
                        f"missing capabilities for {item.action.adapter}/"
                        f"{item.action.operation}: " + ", ".join(sorted(missing))
                    )

        if reasons:
            self._emit(
                "workflow.preflight_failed",
                {"run_id": state.run_id, "reasons": reasons},
            )
            raise PreflightError(reasons)

    # ------------------------------------------------------------------ #
    # Phase handlers.                                                     #
    # ------------------------------------------------------------------ #
    def _handlers(self):
        return {
            Phase.INTAKE: self._phase_intake,
            Phase.CLARIFICATION: self._phase_clarification,
            Phase.GROUNDING: self._phase_grounding,
            Phase.ROUTE_OPTIMIZATION: self._phase_route,
            Phase.HUMAN_CONTROL: self._phase_human_control,
            Phase.CONFIRMATION: self._phase_confirmation,
            Phase.APPROVAL: self._phase_approval,
            Phase.EXECUTION: self._phase_execution,
            Phase.MONITORING: self._phase_monitoring,
            Phase.RECOVERY: self._phase_recovery,
            Phase.FINALIZATION: self._phase_finalization,
        }

    def _phase_intake(self, state: RunState) -> RunState:
        state.brief = self._intaker.intake(state.contract)
        return self._advance(state, Phase.CLARIFICATION, "brief compiled")

    def _phase_clarification(self, state: RunState) -> RunState:
        assert state.brief is not None
        result = self._compiler.compile(state.brief)
        state.compilation = result
        if result.status is BriefStatus.BLOCKED:
            return self._fail(
                state,
                "blocked by hard constraints: "
                + ", ".join(result.violated_hard_constraints),
            )
        if result.status is BriefStatus.CLARIFYING:
            return self._pause(
                state,
                PauseKind.CLARIFICATION,
                "answers required to resolve the brief",
                {
                    "questions": [q.model_dump(mode="json") for q in result.questions],
                    "unresolved_parameters": result.unresolved_parameters,
                },
            )
        return self._advance(state, Phase.GROUNDING, "requirements resolved")

    def _phase_grounding(self, state: RunState) -> RunState:
        assert state.brief is not None
        grounding = self._grounder.ground(state.brief)
        state.grounding = grounding
        if grounding.unresolved_terms:
            return self._fail(
                state,
                "could not ground terms: " + ", ".join(grounding.unresolved_terms),
            )
        return self._advance(state, Phase.ROUTE_OPTIMIZATION, "intent grounded")

    def _phase_route(self, state: RunState) -> RunState:
        assert state.brief is not None and state.grounding is not None
        route = self._optimizer.optimize(state.brief, state.grounding)
        state.route = route
        if route.chosen.excluded:
            return self._fail(
                state,
                "no viable route: " + (route.chosen.exclusion_reason or "unknown"),
            )
        return self._advance(state, Phase.HUMAN_CONTROL, f"route '{route.chosen.name}'")

    def _phase_human_control(self, state: RunState) -> RunState:
        assert state.brief is not None and state.route is not None
        state.human_control = self._human_control.evaluate(state.brief, state.route)
        return self._advance(state, Phase.CONFIRMATION, state.human_control.rationale)

    def _phase_confirmation(self, state: RunState) -> RunState:
        assert state.human_control is not None
        if not state.human_control.requires_confirmation:
            return self._advance(state, Phase.APPROVAL, "no confirmation required")
        if state.confirmation is None:
            return self._pause(
                state,
                PauseKind.CONFIRMATION,
                "confirm the route before it executes",
                {"rationale": state.human_control.rationale},
            )
        if not state.confirmation.confirmed:
            return self._cancel(state, "route was not confirmed")
        return self._advance(state, Phase.APPROVAL, "confirmed")

    def _phase_approval(self, state: RunState) -> RunState:
        assert state.human_control is not None
        hc = state.human_control
        if not hc.requires_approval:
            return self._advance(state, Phase.EXECUTION, "no approval required")
        if any(item.decision == "denied" for item in state.approvals):
            return self._cancel(state, "an approver denied the route")
        if len(state.granted_approvals) < hc.approvers_required:
            remaining = hc.approvers_required - len(state.granted_approvals)
            return self._pause(
                state,
                PauseKind.APPROVAL,
                f"{remaining} more approval(s) required",
                {
                    "approvers_required": hc.approvers_required,
                    "granted": len(state.granted_approvals),
                    "reserved_actions": hc.reserved_actions,
                },
            )
        return self._advance(state, Phase.EXECUTION, "approved")

    def _phase_execution(self, state: RunState) -> RunState:
        # Hard gate: no execution without every preflight control, every time.
        self.assert_execution_preflight(state)
        assert state.route is not None
        attempt = state.recovery_attempts + 1
        key = f"{state.run_id}:exec:{attempt}"
        # Resolved brief values (stated by the user, or answered in
        # clarification) flow into the actions right before execution;
        # the recorded route itself stays exactly as planned.
        from .adapters import bind_brief_parameters

        state.execution = self._executor.execute(
            bind_brief_parameters(state.route, state.brief),
            idempotency_key=key,
            attempt=attempt,
        )
        self._emit(
            "workflow.executed",
            {
                "run_id": state.run_id,
                "status": state.execution.status.value,
                "idempotency_key": key,
            },
        )
        return self._advance(state, Phase.MONITORING, f"attempt {attempt}")

    def _phase_monitoring(self, state: RunState) -> RunState:
        assert state.execution is not None
        report = self._monitor.assess(state.execution)
        state.monitoring = report
        if report.healthy:
            return self._advance(state, Phase.FINALIZATION, "healthy")
        return self._advance(state, Phase.RECOVERY, report.summary)

    def _phase_recovery(self, state: RunState) -> RunState:
        assert state.monitoring is not None
        decision = self._recovery.recover(
            report=state.monitoring,
            attempts=state.recovery_attempts,
            max_attempts=state.max_recovery_attempts,
        )
        if decision.recoverable and decision.strategy == "retry":
            state.recovery_attempts += 1
            return self._advance(
                state, Phase.EXECUTION, f"recovering: {decision.reason}"
            )
        incident = Incident(
            reason=state.monitoring.summary or "execution unhealthy",
            severity="high",
            escalated=True,
        )
        state.incidents.append(incident)
        self._emit(
            "workflow.incident", {"run_id": state.run_id, "incident_id": incident.id}
        )
        return self._pause(
            state,
            PauseKind.INCIDENT,
            "execution escalated to an incident; operator decision required",
            {"incident_id": incident.id, "reason": incident.reason},
        )

    def _phase_finalization(self, state: RunState) -> RunState:
        assert state.route is not None
        success = (
            state.execution is not None
            and state.execution.status is ExecutionStatus.SUCCEEDED
        )
        state.feedback = self._feedback.learn(
            route=state.route,
            success=success,
            summary=(state.monitoring.summary if state.monitoring else ""),
        )
        state.result = {
            "status": state.execution.status.value if state.execution else "unknown",
            "attempts": state.recovery_attempts + 1,
            "route": state.route.chosen.name,
            "actions": len(state.route.chosen.actions),
        }
        # What the hands actually brought back (executors keep evidence
        # bounded) — the part of the run the user came for.
        outputs = [
            outcome.evidence
            for outcome in (
                state.execution.action_outcomes if state.execution else []
            )
            if outcome.evidence
        ]
        if outputs:
            state.result["outputs"] = outputs
        self._emit("workflow.completed", {"run_id": state.run_id})
        return self._advance(state, Phase.COMPLETED, "finalized")

    # ------------------------------------------------------------------ #
    # Resume appliers.                                                    #
    # ------------------------------------------------------------------ #
    def _resume_clarification(self, state: RunState, resume: ResumeInput) -> RunState:
        assert state.brief is not None
        if not resume.answers:
            raise ResumeError("clarification resume requires at least one answer")
        bound = []
        for param in state.brief.parameters:
            if param.name in resume.answers:
                bound.append(
                    param.model_copy(
                        update={
                            "value": resume.answers[param.name],
                            "source": ParameterSource.USER,
                        }
                    )
                )
            else:
                bound.append(param)
        state.brief = state.brief.model_copy(update={"parameters": bound})
        return state

    def _resume_confirmation(self, state: RunState, resume: ResumeInput) -> RunState:
        if resume.confirmed is None:
            raise ResumeError("confirmation resume requires `confirmed`")
        state.confirmation = ConfirmationRecord(
            confirmed=resume.confirmed, principal=resume.principal
        )
        return state

    def _resume_approval(self, state: RunState, resume: ResumeInput) -> RunState:
        if not resume.approvals:
            raise ResumeError("approval resume requires at least one approval record")
        state.approvals.extend(resume.approvals)
        return state

    def _resume_incident(self, state: RunState, resume: ResumeInput) -> RunState:
        decision = (resume.incident_decision or "").lower()
        if decision not in {"retry", "abort"}:
            raise ResumeError("incident resume requires decision 'retry' or 'abort'")
        incident = state.latest_incident
        if incident is None:
            raise ResumeError("no incident to resolve")
        if decision == "retry":
            state.incidents[-1] = incident.model_copy(update={"resolution": "retried"})
            state.recovery_attempts += 1
            self._advance(state, Phase.EXECUTION, "operator retried after incident")
            return state
        state.incidents[-1] = incident.model_copy(update={"resolution": "aborted"})
        self._fail(state, "operator aborted after incident")
        return state

    # ------------------------------------------------------------------ #
    # State transitions.                                                  #
    # ------------------------------------------------------------------ #
    def _advance(self, state: RunState, to_phase: Phase, note: str) -> RunState:
        state.history.append(
            PhaseTransition(from_phase=state.phase, to_phase=to_phase, note=note)
        )
        state.phase = to_phase
        state.updated_at = datetime.now(UTC)
        return state

    def _pause(
        self, state: RunState, kind: PauseKind, prompt: str, payload: dict
    ) -> RunState:
        state.pause = PauseToken(kind=kind, prompt=prompt, payload=payload)
        state.updated_at = datetime.now(UTC)
        self._emit("workflow.paused", {"run_id": state.run_id, "kind": kind.value})
        return state

    def _fail(self, state: RunState, reason: str) -> RunState:
        state.failure_reason = reason
        self._emit("workflow.failed", {"run_id": state.run_id, "reason": reason})
        return self._advance(state, Phase.FAILED, reason)

    def _cancel(self, state: RunState, reason: str) -> RunState:
        state.failure_reason = reason
        self._emit("workflow.cancelled", {"run_id": state.run_id, "reason": reason})
        return self._advance(state, Phase.CANCELLED, reason)

    def _emit(self, event_type: str, payload: dict) -> None:
        if self._events is not None:
            self._events.append(event_type, payload)
