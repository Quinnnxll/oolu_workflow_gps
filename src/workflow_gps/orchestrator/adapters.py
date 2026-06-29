"""Deterministic, fully-offline default adapters for the unified orchestrator.

These are real implementations (not mocks): they compose the existing skill core
— the Requirement and Constraint Compiler, the ``ActionExecutor`` contract, and
``ExecutionOutcome`` — so the orchestrator drives the same domain the rest of the
system already uses. Networked, durable, or model-backed adapters can replace any
one of them without touching the run state or the phase machine (ADR-0002).
"""

from __future__ import annotations

from datetime import UTC, datetime

from ..skills.models import ExecutionOutcome, ExecutionStatus
from ..skills.ports import ActionExecutor
from ..skills.requirements import RequirementBrief
from .ports import OrchestratorError
from .state import (
    Blueprint,
    ExecutionRecord,
    FeedbackRecord,
    HumanControlDecision,
    MonitorReport,
    RecoveryDecision,
    RoutePlan,
    SemanticEdge,
    SemanticGrounding,
    TaskContract,
)


# --------------------------------------------------------------------------- #
# Intake.                                                                      #
# --------------------------------------------------------------------------- #
class StaticIntaker:
    """Intake from a pre-built brief.

    Intake (parsing a natural-language request into parameters and constraints)
    belongs to a model-backed adapter later; for the deterministic baseline the
    brief is supplied directly so the rest of the flow is testable offline.
    """

    def __init__(self, brief: RequirementBrief):
        self._brief = brief

    def intake(self, contract: TaskContract) -> RequirementBrief:
        # Keep the brief's intent aligned with the contract's destination.
        if self._brief.intent != contract.intent:
            return self._brief.model_copy(update={"intent": contract.intent})
        return self._brief


# --------------------------------------------------------------------------- #
# Semantic grounding.                                                          #
# --------------------------------------------------------------------------- #
class CapabilityGrounder:
    """Ground a brief onto a known capability set.

    Required terms come from the brief's resolved parameters; each is grounded to
    a capability via the supplied ``term_to_capability`` map. Terms with no
    mapping are reported as unresolved so the machine can refuse to navigate
    blindly.
    """

    def __init__(
        self,
        term_to_capability: dict[str, str] | None = None,
        *,
        always_resolved: frozenset[str] = frozenset(),
    ):
        self._map = dict(term_to_capability or {})
        self._always = frozenset(always_resolved)

    def ground(self, brief: RequirementBrief) -> SemanticGrounding:
        edges: list[SemanticEdge] = []
        resolved: set[str] = set(self._always)
        unresolved: list[str] = []
        terms = [param.name for param in brief.parameters]
        for term in terms:
            capability = self._map.get(term)
            if capability is None:
                unresolved.append(term)
                continue
            resolved.add(capability)
            edges.append(SemanticEdge(source=term, target=capability))
        return SemanticGrounding(
            edges=edges,
            resolved_capabilities=frozenset(resolved),
            unresolved_terms=unresolved,
        )


# --------------------------------------------------------------------------- #
# Route optimization.                                                          #
# --------------------------------------------------------------------------- #
class LeastCostRouteOptimizer:
    """Pick the cheapest non-excluded blueprint from a fixed candidate set.

    Candidate blueprints are provided up front (a model or planner can generate
    them later). Blueprints whose actions need a capability that grounding did not
    resolve are excluded with a reason rather than silently chosen.
    """

    def __init__(self, blueprints: list[Blueprint]):
        if not blueprints:
            raise OrchestratorError("route optimizer requires at least one blueprint")
        self._blueprints = list(blueprints)

    def optimize(
        self, brief: RequirementBrief, grounding: SemanticGrounding
    ) -> RoutePlan:
        scored: list[Blueprint] = []
        for blueprint in self._blueprints:
            needed: set[str] = set()
            for item in blueprint.actions:
                needed |= set(item.required_capabilities)
            missing = needed - set(grounding.resolved_capabilities)
            if missing:
                scored.append(
                    blueprint.model_copy(
                        update={
                            "excluded": True,
                            "exclusion_reason": "unresolved capabilities: "
                            + ", ".join(sorted(missing)),
                        }
                    )
                )
            else:
                scored.append(blueprint)
        ranked = sorted(scored, key=lambda item: item.estimated_cost)
        viable = [item for item in ranked if not item.excluded]
        chosen = viable[0] if viable else ranked[0]
        alternatives = [item for item in ranked if item.id != chosen.id]
        return RoutePlan(
            chosen=chosen,
            alternatives=alternatives,
            total_cost=chosen.estimated_cost,
        )


# --------------------------------------------------------------------------- #
# Human control.                                                              #
# --------------------------------------------------------------------------- #
class RiskBasedHumanControl:
    """Derive required human controls from route risk and authorization mode.

    - Any reserved action requires approval.
    - Write/irreversible risk requires confirmation.
    - A fully-delegated authorization grant drops confirmation but never drops
      approval for reserved actions (delegation is not a substitute for authority).
    """

    def __init__(self, *, dual_approval_risks: frozenset[str] = frozenset()):
        self._dual = frozenset(dual_approval_risks)

    def evaluate(
        self, brief: RequirementBrief, route: RoutePlan
    ) -> HumanControlDecision:
        reserved = [item for item in route.chosen.actions if item.reserved]
        risks = {item.risk for item in route.chosen.actions}
        writes = bool(risks & {"write", "irreversible"})

        fully_delegated = brief.authorization.mode.value == "fully_delegated"
        requires_confirmation = writes and not fully_delegated
        requires_approval = bool(reserved)
        approvers_required = 0
        if requires_approval:
            approvers_required = 2 if (risks & self._dual) else 1

        rationale_parts: list[str] = []
        if requires_confirmation:
            rationale_parts.append("route writes to the workspace")
        if requires_approval:
            rationale_parts.append(
                f"{len(reserved)} reserved action(s) require {approvers_required} approval(s)"
            )
        if not rationale_parts:
            rationale_parts.append("no human control required (read-only, autonomous)")

        return HumanControlDecision(
            requires_confirmation=requires_confirmation,
            requires_approval=requires_approval,
            approvers_required=approvers_required,
            reserved_actions=[item.action.id for item in reserved],
            rationale="; ".join(rationale_parts),
        )


# --------------------------------------------------------------------------- #
# Execution.                                                                  #
# --------------------------------------------------------------------------- #
class ActionExecutorRouteRunner:
    """Execute a route's actions through the existing ``ActionExecutor`` contract.

    Each reserved action's ``adapter`` selects an executor; the action's
    ``operation`` must be in that executor's capabilities (the same check the
    safe skill runtime enforces). Outcomes are aggregated into one
    ``ExecutionRecord``; the first non-success stops the run.
    """

    def __init__(self, executors: dict[str, ActionExecutor]):
        self._executors = dict(executors)

    def capabilities(self) -> frozenset[str]:
        caps: set[str] = set()
        for executor in self._executors.values():
            caps |= set(executor.capabilities())
        return frozenset(caps)

    def execute(
        self, route: RoutePlan, *, idempotency_key: str, attempt: int
    ) -> ExecutionRecord:
        started = datetime.now(UTC)
        outcomes: list[ExecutionOutcome] = []
        for index, item in enumerate(route.chosen.actions):
            executor = self._executors.get(item.action.adapter)
            if executor is None or item.action.operation not in executor.capabilities():
                return ExecutionRecord(
                    idempotency_key=idempotency_key,
                    attempt=attempt,
                    status=ExecutionStatus.BLOCKED,
                    action_outcomes=outcomes,
                    error=(
                        "missing executor capability: "
                        f"{item.action.adapter}/{item.action.operation}"
                    ),
                    started_at=started,
                    completed_at=datetime.now(UTC),
                )
            outcome = executor.execute(
                item.action, idempotency_key=f"{idempotency_key}:{index}"
            )
            outcomes.append(outcome)
            if outcome.status is not ExecutionStatus.SUCCEEDED:
                return ExecutionRecord(
                    idempotency_key=idempotency_key,
                    attempt=attempt,
                    status=ExecutionStatus.FAILED,
                    action_outcomes=outcomes,
                    error=outcome.error or "action failed",
                    started_at=started,
                    completed_at=datetime.now(UTC),
                )
        return ExecutionRecord(
            idempotency_key=idempotency_key,
            attempt=attempt,
            status=ExecutionStatus.SUCCEEDED,
            action_outcomes=outcomes,
            started_at=started,
            completed_at=datetime.now(UTC),
        )


# --------------------------------------------------------------------------- #
# Monitoring, recovery, feedback.                                            #
# --------------------------------------------------------------------------- #
class StatusOutcomeMonitor:
    """Healthy iff the execution succeeded."""

    def assess(self, execution: ExecutionRecord) -> MonitorReport:
        healthy = execution.status is ExecutionStatus.SUCCEEDED
        return MonitorReport(
            healthy=healthy,
            status=execution.status,
            signals={"attempt": execution.attempt, "error": execution.error},
            summary="execution succeeded"
            if healthy
            else (execution.error or "unhealthy"),
        )


class BoundedRetryRecovery:
    """Retry a failed (but not blocked) execution while attempts remain.

    A ``BLOCKED`` execution is a control/capability failure, not a transient one,
    so it escalates immediately rather than retrying.
    """

    def recover(
        self, *, report: MonitorReport, attempts: int, max_attempts: int
    ) -> RecoveryDecision:
        if report.status is ExecutionStatus.BLOCKED:
            return RecoveryDecision(
                recoverable=False,
                strategy="escalate",
                reason="execution blocked by a control or capability gate",
            )
        if attempts < max_attempts:
            return RecoveryDecision(
                recoverable=True,
                strategy="retry",
                reason=f"retrying ({attempts + 1}/{max_attempts})",
            )
        return RecoveryDecision(
            recoverable=False,
            strategy="escalate",
            reason="recovery attempts exhausted",
        )


class CollectingFeedbackSink:
    """Record route-learning feedback in memory (and optionally forward it)."""

    def __init__(self, forward=None):
        self.records: list[FeedbackRecord] = []
        self._forward = forward

    def learn(self, *, route: RoutePlan, success: bool, summary: str) -> FeedbackRecord:
        record = FeedbackRecord(
            learned_route=route.chosen.name,
            success=success,
            notes=summary,
        )
        self.records.append(record)
        if self._forward is not None:
            self._forward(record)
        return record
