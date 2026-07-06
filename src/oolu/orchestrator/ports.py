"""Deployment-neutral ports for each stage of the unified orchestrator.

Each phase of the run is driven through one of these ``Protocol``s, so the
deterministic offline adapters that ship on this branch can be swapped for
durable, networked, or model-backed implementations later without touching the
run state or the phase machine (ADR-0002).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .state import (
    ExecutionRecord,
    FeedbackRecord,
    HumanControlDecision,
    MonitorReport,
    RecoveryDecision,
    RoutePlan,
    SemanticGrounding,
    TaskContract,
)


class OrchestratorError(RuntimeError):
    """Base class for orchestrator runtime failures."""


class PreflightError(OrchestratorError):
    """Raised when an execution path fails a preflight control or capability check.

    Carries the individual gate failures so callers can surface every reason at
    once rather than one at a time.
    """

    def __init__(self, reasons: list[str]):
        self.reasons = list(reasons)
        super().__init__("; ".join(self.reasons) or "preflight failed")


class ResumeError(OrchestratorError):
    """Raised when a resume input does not match the current pause."""


# Forward references resolved lazily to avoid importing the requirements module
# here; the engine passes already-constructed briefs/results.
from ..skills.requirements import RequirementBrief  # noqa: E402


@runtime_checkable
class Intaker(Protocol):
    """Turn a task contract into a requirement brief (parameters/constraints)."""

    def intake(self, contract: TaskContract) -> RequirementBrief: ...


@runtime_checkable
class Grounder(Protocol):
    """Ground intent terms onto the concrete capability road network."""

    def ground(self, brief: RequirementBrief) -> SemanticGrounding: ...


@runtime_checkable
class RouteOptimizer(Protocol):
    """Rank candidate blueprints and pick a route."""

    def optimize(
        self, brief: RequirementBrief, grounding: SemanticGrounding
    ) -> RoutePlan: ...


@runtime_checkable
class HumanControlPolicy(Protocol):
    """Decide which human controls a route requires before execution."""

    def evaluate(
        self, brief: RequirementBrief, route: RoutePlan
    ) -> HumanControlDecision: ...


@runtime_checkable
class WorkflowExecutor(Protocol):
    """Execute a chosen route. ``capabilities`` is consulted at preflight."""

    def capabilities(self) -> frozenset[str]: ...

    def execute(
        self, route: RoutePlan, *, idempotency_key: str, attempt: int
    ) -> ExecutionRecord: ...


@runtime_checkable
class OutcomeMonitor(Protocol):
    """Assess an execution and report whether it is healthy."""

    def assess(self, execution: ExecutionRecord) -> MonitorReport: ...


@runtime_checkable
class RecoveryPolicy(Protocol):
    """Decide whether an unhealthy execution can be recovered or must escalate."""

    def recover(
        self, *, report: MonitorReport, attempts: int, max_attempts: int
    ) -> RecoveryDecision: ...


@runtime_checkable
class FeedbackSink(Protocol):
    """Record route-learning feedback at finalization."""

    def learn(
        self, *, route: RoutePlan, success: bool, summary: str
    ) -> FeedbackRecord: ...
