"""The commercial spine's typed contracts (M0 of docs/marketplace-build-plan.md).

These are the spec's ``contracts`` block as frozen records: an offer whose
terms are exact, an intent that binds a principal's agent to one versioned
offer, the approval that signs its digest, and the execution authorization
that M1's order service will consume. Frozen matters structurally — a record
that can be edited in place cannot anchor a digest, and the digest is the
whole law: approval of digest A can never execute digest B.

Money is integer micros of the named currency, never floats. Deliberately
absent from every record here: payment credentials, provider references,
capture instructions — M0 has no code path that can move money, and these
models are where that absence starts.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class IntentAction(str, Enum):
    """What the agent asks to do commercially. M0 knows the buy side and the
    sell side's publication; fulfillment-era actions arrive with M1+."""

    PURCHASE = "purchase"
    LISTING = "listing"


class Offer(BaseModel):
    """One seller's exact, versioned terms — the thing an intent binds to.

    ``offer_version`` is the digest's tripwire: a seller who changes any
    term publishes a new version, and an approval bound to the old version
    can never execute against the new one.
    """

    model_config = ConfigDict(frozen=True)

    offer_id: str
    seller_id: str
    offer_version: int = Field(ge=1)
    item_id: str
    quantity: int = Field(gt=0)
    subtotal_micros: int = Field(ge=0)
    currency: str = "USD"
    tax_estimate_micros: int = Field(default=0, ge=0)
    fees_micros: int = Field(default=0, ge=0)
    fulfillment_terms: str = ""
    refund_terms: str = ""
    refundable: bool = True
    # "" = a one-time purchase. Any text here is a recurring obligation and
    # trips the recurring policy trigger.
    recurring_terms: str = ""
    expires_at: datetime | None = None
    signature: str = ""

    @property
    def total_micros(self) -> int:
        return self.subtotal_micros + self.tax_estimate_micros + self.fees_micros


class CommercialIntent(BaseModel):
    """An immutable ask: this principal's agent, this exact offer, this window.

    The intent embeds the offer *as seen* (``offer_snapshot``). At execution
    time the digest is recomputed from the LIVE offer and compared against
    the approved one — the snapshot is what was promised, the live offer is
    what would happen, and they must be identical.
    """

    model_config = ConfigDict(frozen=True)

    intent_id: str
    tenant_id: str
    principal_id: str
    agent_id: str
    action: IntentAction
    offer_snapshot: Offer
    category: str = ""
    delivery_destination: str = ""
    data_permissions: tuple[str, ...] = ()
    # The principal's stated ceiling — an intent whose offer total exceeds
    # it is refused at creation, not negotiated down.
    maximum_total_micros: int = Field(ge=0)
    approval_policy_id: str
    idempotency_key: str
    created_at: datetime
    expires_at: datetime


class ApprovalRecord(BaseModel):
    """A human's authenticated decision, bound to one digest, once.

    ``authentication_strength`` records how the approver proved themselves
    ("normal" session vs "strong" step-up); the verdict dictates the floor.
    Consumption is tracked by the store, not here — the record itself is
    immutable evidence.
    """

    model_config = ConfigDict(frozen=True)

    approval_id: str
    tenant_id: str
    intent_id: str
    intent_digest: str
    approver_id: str
    authentication_strength: str  # "normal" | "strong"
    decision: str  # "approved" | "rejected"
    created_at: datetime
    expires_at: datetime


class ExecutionAuthorization(BaseModel):
    """The spine's terminal artifact: proof that execution *may* proceed.

    This is the seam M1's order service consumes. It carries no payment
    method, no provider reference, no capture instruction — deliberately.
    In M0 nothing consumes it, and that is the phase's exit gate: the law
    exists before the money does.
    """

    model_config = ConfigDict(frozen=True)

    authorization_id: str
    tenant_id: str
    intent_id: str
    intent_digest: str
    # Empty for auto-executed / notify decisions; every consumed approval
    # otherwise. Multi-approver intents list one per approver.
    approval_ids: tuple[str, ...] = ()
    authorized_at: datetime
