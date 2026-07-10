"""The single versioned, serializable run state for the unified orchestrator.

``RunState`` is the one source of truth for an end-to-end workflow. It round-trips
losslessly through ``model_dump_json()`` / ``model_validate_json()`` so pause,
resume, transport, and durability all reduce to storing and reloading this object
(see ADR-0002). Every phase writes its result as a typed sub-record here, an
append-only ``history`` makes the path auditable, and a nullable ``pause`` is the
only signal that the machine is waiting on a human.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from ..skills.models import (
    ActionEvent,
    ApprovalRecord,
    ExecutionOutcome,
    ExecutionStatus,
)
from ..skills.requirements import CompilationResult, RequirementBrief

ORCHESTRATOR_SCHEMA_VERSION = 1


def _id() -> str:
    return uuid4().hex


def _now() -> datetime:
    return datetime.now(UTC)


class Phase(str, Enum):
    """Where a workflow is in the unified flow."""

    INTAKE = "intake"
    CLARIFICATION = "clarification"
    GROUNDING = "grounding"
    ROUTE_OPTIMIZATION = "route_optimization"
    HUMAN_CONTROL = "human_control"
    CONFIRMATION = "confirmation"
    APPROVAL = "approval"
    EXECUTION = "execution"
    MONITORING = "monitoring"
    RECOVERY = "recovery"
    FINALIZATION = "finalization"
    # Terminal phases.
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


_TERMINAL_PHASES = frozenset({Phase.COMPLETED, Phase.FAILED, Phase.CANCELLED})


class PauseKind(str, Enum):
    """The four supported human-in-the-loop wait points."""

    CLARIFICATION = "clarification"
    CONFIRMATION = "confirmation"
    APPROVAL = "approval"
    INCIDENT = "incident"


# --------------------------------------------------------------------------- #
# Phase output records (each frozen and self-describing).                      #
# --------------------------------------------------------------------------- #
class TaskContract(BaseModel):
    """The intake contract — the destination plus who is asking for it."""

    model_config = ConfigDict(frozen=True)

    schema_version: int = ORCHESTRATOR_SCHEMA_VERSION
    intent: str
    submitted_by: str = "local-user"
    metadata: dict[str, Any] = Field(default_factory=dict)


class SemanticEdge(BaseModel):
    """One grounded link from an intent term to a concrete capability."""

    model_config = ConfigDict(frozen=True)

    source: str
    target: str
    relation: str = "resolves_to"
    confidence: float = 1.0


class SemanticGrounding(BaseModel):
    """The result of grounding intent terms onto the capability road network."""

    model_config = ConfigDict(frozen=True)

    edges: list[SemanticEdge] = Field(default_factory=list)
    resolved_capabilities: frozenset[str] = Field(default_factory=frozenset)
    unresolved_terms: list[str] = Field(default_factory=list)


class ReservedAction(BaseModel):
    """A planned action plus the capability it needs and whether it is gated.

    ``reserved`` marks an action whose risk requires explicit human authorization
    before it may run (the orchestrator's "reserved actions").
    """

    model_config = ConfigDict(frozen=True)

    action: ActionEvent
    required_capabilities: frozenset[str] = Field(default_factory=frozenset)
    reserved: bool = False
    risk: str = "read"


class BlueprintEdge(BaseModel):
    """A dependency between two actions in a blueprint, by ``ActionEvent.id``.

    ``relation="before"``: the target may run only after the source verified.
    ``relation="fallback"``: the target runs only if the source failed.

    ``provenance`` keeps human-authored structure distinguishable from learned
    structure: ``"sop"`` edges may only be changed by the human who owns the
    SOP, while ``"learned"``/``"data"`` edges may be pruned by new evidence.
    """

    model_config = ConfigDict(frozen=True)

    source: str
    target: str
    relation: Literal["before", "fallback"] = "before"
    provenance: Literal["sop", "learned", "data"] = "learned"


class Blueprint(BaseModel):
    """A candidate route: reserved actions plus the dependency edges between them.

    ``ordering="sequential"`` (the default, and the pre-DAG behaviour) chains
    the actions in list order — explicit edges are added *on top* of the
    chain, so an SOP edge that contradicts the demonstrated order surfaces as
    a cycle instead of being silently ignored. ``ordering="graph"`` means the
    edges are the complete ordering: actions with no path between them are
    independent and may run in parallel.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=_id)
    name: str
    actions: list[ReservedAction] = Field(default_factory=list)
    edges: list[BlueprintEdge] = Field(default_factory=list)
    ordering: Literal["sequential", "graph"] = "sequential"
    estimated_cost: float = 0.0
    excluded: bool = False
    exclusion_reason: str | None = None
    # Where this route came from. "planned" routes are assembled from known
    # nodes; "llm_rebuild" routes were planned and written by a model after
    # the user's retries were exhausted. ``plan_notes`` carries the planner's
    # own step narrative (the model's numbered plan) for the user to read.
    # "node_function" routes execute a node's OWN stored function.
    origin: Literal["planned", "llm_rebuild", "node_function"] = "planned"
    plan_notes: list[str] = Field(default_factory=list)


class RoutePlan(BaseModel):
    """The optimizer's chosen blueprint plus the alternatives it ranked."""

    model_config = ConfigDict(frozen=True)

    chosen: Blueprint
    alternatives: list[Blueprint] = Field(default_factory=list)
    total_cost: float = 0.0

    @property
    def reserved_action_ids(self) -> list[str]:
        return [item.action.id for item in self.chosen.actions if item.reserved]


class HumanControlDecision(BaseModel):
    """What human control this route requires before it may execute."""

    model_config = ConfigDict(frozen=True)

    requires_confirmation: bool = False
    requires_approval: bool = False
    approvers_required: int = 0
    reserved_actions: list[str] = Field(default_factory=list)
    rationale: str = ""


class ConfirmationRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    confirmed: bool
    principal: str = "local-user"
    at: datetime = Field(default_factory=_now)


class ExecutionRecord(BaseModel):
    """The aggregate outcome of executing every action in the chosen route."""

    model_config = ConfigDict(frozen=True)

    schema_version: int = ORCHESTRATOR_SCHEMA_VERSION
    idempotency_key: str
    attempt: int = 1
    status: ExecutionStatus
    action_outcomes: list[ExecutionOutcome] = Field(default_factory=list)
    error: str | None = None
    # The exact node that caused the failure: the FIRST action that failed
    # (cascade-cancelled dependents are consequences, not causes). ``label``
    # is the human-readable ``adapter/operation`` name.
    failed_action_id: str | None = None
    failed_action_label: str | None = None
    started_at: datetime = Field(default_factory=_now)
    completed_at: datetime | None = None


class MonitorReport(BaseModel):
    """The outcome-monitoring verdict on an execution."""

    model_config = ConfigDict(frozen=True)

    healthy: bool
    status: ExecutionStatus
    signals: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""


class RecoveryDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    recoverable: bool
    strategy: str  # "retry" | "escalate"
    reason: str = ""


class Incident(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=_id)
    reason: str
    severity: str = "high"
    escalated: bool = True
    resolution: str | None = None  # "retried" | "aborted"
    at: datetime = Field(default_factory=_now)


class FeedbackRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    learned_route: str
    success: bool
    notes: str = ""
    at: datetime = Field(default_factory=_now)


class PauseToken(BaseModel):
    """The only signal that the machine is waiting on a human."""

    model_config = ConfigDict(frozen=True)

    kind: PauseKind
    prompt: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_now)


class PhaseTransition(BaseModel):
    model_config = ConfigDict(frozen=True)

    from_phase: Phase
    to_phase: Phase
    note: str = ""
    at: datetime = Field(default_factory=_now)


class ResumeInput(BaseModel):
    """Human input supplied to ``resume()`` for a specific pause kind."""

    model_config = ConfigDict(frozen=True)

    kind: PauseKind
    principal: str = "local-user"
    answers: dict[str, Any] = Field(default_factory=dict)
    confirmed: bool | None = None
    approvals: list[ApprovalRecord] = Field(default_factory=list)
    incident_decision: str | None = None  # "retry" | "abort"


class RunState(BaseModel):
    """The single versioned, serializable run state (see ADR-0002)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    schema_version: int = ORCHESTRATOR_SCHEMA_VERSION
    run_id: str = Field(default_factory=_id)
    intent: str
    phase: Phase = Phase.INTAKE
    contract: TaskContract

    # Per-phase outputs.
    brief: RequirementBrief | None = None
    compilation: CompilationResult | None = None
    grounding: SemanticGrounding | None = None
    route: RoutePlan | None = None
    human_control: HumanControlDecision | None = None
    confirmation: ConfirmationRecord | None = None
    approvals: list[ApprovalRecord] = Field(default_factory=list)
    execution: ExecutionRecord | None = None
    monitoring: MonitorReport | None = None
    incidents: list[Incident] = Field(default_factory=list)
    feedback: FeedbackRecord | None = None

    # Recovery loop control. ``recovery_attempts`` counts every re-execution
    # (automatic and operator-driven); ``user_retries`` counts only the
    # operator's explicit incident "retry" decisions — after two of those
    # fail, the machine escalates to the LLM rebuild instead of asking a
    # third time. ``rebuild_attempts`` caps the rebuild at one per run so a
    # broken synthesis can never loop.
    recovery_attempts: int = 0
    max_recovery_attempts: int = 1
    user_retries: int = 0
    rebuild_attempts: int = 0

    # Human-in-the-loop + audit.
    pause: PauseToken | None = None
    history: list[PhaseTransition] = Field(default_factory=list)

    # Terminal outputs.
    result: dict[str, Any] | None = None
    failure_reason: str | None = None

    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    # ------------------------------------------------------------------ #
    # Derived helpers (not serialized fields).                            #
    # ------------------------------------------------------------------ #
    @property
    def is_paused(self) -> bool:
        return self.pause is not None

    @property
    def is_terminal(self) -> bool:
        return self.phase in _TERMINAL_PHASES

    @property
    def granted_approvals(self) -> list[ApprovalRecord]:
        return [item for item in self.approvals if item.decision == "approved"]

    @property
    def latest_incident(self) -> Incident | None:
        return self.incidents[-1] if self.incidents else None
