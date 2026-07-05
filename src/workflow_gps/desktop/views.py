"""Serializable, secret-free view-models for the desktop shell.

These DTOs are exactly what crosses the local loopback boundary to the UI. They are
deliberately *projections* of backend state: they never carry a provider secret, a
token, or an execution backend handle, so the UI cannot leak credentials or reach
around backend policy. Every model is frozen and JSON-serializable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class QuestionView(BaseModel):
    model_config = ConfigDict(frozen=True)

    parameter: str
    question: str
    suggested_values: list[Any] = Field(default_factory=list)
    priority: int = 0


class TaskView(BaseModel):
    """The primary task screen: where the workflow is and what the UI should show."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    intent: str
    phase: str
    awaiting: str | None = None  # the pause kind, if any
    prompt: str | None = None
    questions: list[QuestionView] = Field(default_factory=list)
    can_cancel: bool = False
    failure_reason: str | None = None
    result: dict[str, Any] | None = None


class ActionView(BaseModel):
    model_config = ConfigDict(frozen=True)

    adapter: str
    operation: str
    reserved: bool
    risk: str


class BlueprintView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    estimated_cost: float
    excluded: bool
    exclusion_reason: str | None = None
    actions: list[ActionView] = Field(default_factory=list)
    reserved_action_count: int = 0


class RoutePreview(BaseModel):
    """Route preview with cost and human-readable exclusion explanations."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    chosen: BlueprintView | None = None
    alternatives: list[BlueprintView] = Field(default_factory=list)
    total_cost: float = 0.0
    exclusions: list[dict[str, str]] = Field(default_factory=list)


class InboxItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    kind: str  # confirmation | approval | incident
    intent: str
    prompt: str
    created_at: datetime


class TimelineEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    at: datetime
    label: str
    detail: str = ""


class AuditEntryView(BaseModel):
    model_config = ConfigDict(frozen=True)

    seq: int
    event_type: str
    at: datetime


class AuditView(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    verified: bool
    entries: list[AuditEntryView] = Field(default_factory=list)


class ProviderConnectionView(BaseModel):
    """A provider connection as shown in settings — never includes the secret."""

    model_config = ConfigDict(frozen=True)

    connection_id: str
    provider: str
    status: str  # connected | disconnected
    scopes: list[str] = Field(default_factory=list)
    connected_at: datetime
    # An opaque vault handle for management; not a secret.
    credential_ref_id: str


class ExecutionLabel(BaseModel):
    model_config = ConfigDict(frozen=True)

    trust_level: str
    allowed_backends: list[str]
    isolated: bool
    label: str


class WorkerHealthView(BaseModel):
    model_config = ConfigDict(frozen=True)

    docker_available: bool
    labels: list[ExecutionLabel] = Field(default_factory=list)


class AssemblyPayoutView(BaseModel):
    """Who would earn what if this step's run verifies. A forecast only."""

    model_config = ConfigDict(frozen=True)

    noder: str
    amount: float


class AssemblyStepView(BaseModel):
    """One planned step: what runs, what it costs, who gets paid."""

    model_config = ConfigDict(frozen=True)

    name: str
    kind: str  # actions | script | subgraph
    gap: bool = False  # a synthesized fill-in for a slot nobody produces yet
    version_id: str | None = None
    price: float | None = None
    price_notes: list[str] = Field(default_factory=list)  # the clearing forces
    payouts: list[AssemblyPayoutView] = Field(default_factory=list)


class AssemblyPreviewView(BaseModel):
    """The assembly screen: the plan, its prices, and its payees — before
    anything runs. ``contract`` is the runnable artifact the user confirms."""

    model_config = ConfigDict(frozen=True)

    goal: str
    complete: bool
    selected: list[str] = Field(default_factory=list)
    gap_filled: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)  # slot names, human-scale
    steps: list[AssemblyStepView] = Field(default_factory=list)
    estimated_gross_total: float = 0.0
    platform_margin_preview: float = 0.0
    contract: dict[str, Any] | None = None
    # Orderings this desktop's own runs consistently exhibited, already
    # stamped onto the contract as learned edges the run will honor.
    learned_order: list[dict[str, str]] = Field(default_factory=list)
    # The plan's cost judged against caps, thresholds, this desktop's own
    # spending behavior, and the (possibly partial) linked wallet.
    budget: dict[str, Any] | None = None


class AssemblyRunStepView(BaseModel):
    """One executed action's outcome inside a confirmed assembly run."""

    model_config = ConfigDict(frozen=True)

    status: str
    error: str | None = None


class AssemblyRunView(BaseModel):
    """What the confirm button gets back: the run's outcome plus the
    committed economics (who is owed what once the platform verifies it)."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    status: str
    error: str | None = None
    steps: list[AssemblyRunStepView] = Field(default_factory=list)
    gross: float = 0.0
    provider_cost: float = 0.0
    noders: list[str] = Field(default_factory=list)


class EarningsEntryView(BaseModel):
    """One ledger line, in currency units (micros stay in the ledger)."""

    model_config = ConfigDict(frozen=True)

    kind: str  # accrual | reserve | clawback | payout
    amount: float
    event_id: str | None = None
    available_at: datetime


class PayoutBatchView(BaseModel):
    model_config = ConfigDict(frozen=True)

    batch_id: str
    amount: float
    status: str  # pending | paid | failed
    provider_ref: str | None = None
    created_at: datetime


class PayoutAccountView(BaseModel):
    """Onboarding state for receiving payouts — never a credential.

    ``provider_account_id`` is an opaque management handle (like a vault
    ref), not a secret; KYC itself happens on the processor's side.
    """

    model_config = ConfigDict(frozen=True)

    onboarded: bool
    kyc_status: str  # not_onboarded | pending | verified | rejected
    payouts_enabled: bool = False
    provider_account_id: str | None = None
    country: str | None = None
    currency: str | None = None


class EarningsView(BaseModel):
    """The earnings screen: what the local noder has earned, holds, and
    was paid — a projection of the shared ledger, never a write path."""

    model_config = ConfigDict(frozen=True)

    noder: str
    available: float
    pending: float
    reserved: float
    lifetime_paid: float
    entries: list[EarningsEntryView] = Field(default_factory=list)
    batches: list[PayoutBatchView] = Field(default_factory=list)


class ExportBundle(BaseModel):
    """A local data export for one workflow — secrets are never included."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    generated_at: datetime
    run_state: dict[str, Any]
    approvals: list[dict[str, Any]] = Field(default_factory=list)
    incidents: list[dict[str, Any]] = Field(default_factory=list)
    audit: list[AuditEntryView] = Field(default_factory=list)
