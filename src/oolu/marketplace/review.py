"""The review surface's pure half: what an approver must be, and see.

The review service's laws (spec ``approval.lifecycle``) split cleanly into
pure rules — which authentication strength and how many approvers a verdict
demands — and a rendering that shows the human the EXACT terms the digest
covers, never a paraphrase. The summary is generated from the same fields
the digest hashes, so what the approver reads is what the approval binds.
"""

from __future__ import annotations

from .models import CommercialIntent
from .policy import Decision, PolicyVerdict

_M = 1_000_000


def required_strength(decision: Decision) -> str:
    """The authentication floor for approving under this decision."""
    if decision in (
        Decision.REQUIRE_STRONG_APPROVAL,
        Decision.REQUIRE_MULTIPLE_APPROVERS,
    ):
        return "strong"
    if decision is Decision.REQUIRE_APPROVAL:
        return "normal"
    return "none"


def required_approvers(decision: Decision) -> int:
    if decision is Decision.REQUIRE_MULTIPLE_APPROVERS:
        return 2
    if decision in (Decision.REQUIRE_APPROVAL, Decision.REQUIRE_STRONG_APPROVAL):
        return 1
    return 0


def approval_summary(
    intent: CommercialIntent, verdict: PolicyVerdict, *, digest: str
) -> dict:
    """The approval preview: exact terms, material risks, and the digest.

    Every number here is read from the offer snapshot the digest hashes.
    The ``risks`` are the verdict's own reasons — the policy engine already
    named everything worth a human's attention.
    """
    offer = intent.offer_snapshot
    return {
        "intent_id": intent.intent_id,
        "action": intent.action.value,
        "item": offer.item_id,
        "quantity": offer.quantity,
        "seller": offer.seller_id,
        "offer_version": offer.offer_version,
        "subtotal": f"{offer.subtotal_micros / _M:.2f} {offer.currency}",
        "taxes": f"{offer.tax_estimate_micros / _M:.2f} {offer.currency}",
        "fees": f"{offer.fees_micros / _M:.2f} {offer.currency}",
        "total": f"{offer.total_micros / _M:.2f} {offer.currency}",
        "delivery_terms": offer.fulfillment_terms,
        "refund_terms": offer.refund_terms,
        "recurring_terms": offer.recurring_terms,
        "data_permissions": sorted(intent.data_permissions),
        "expires_at": intent.expires_at.isoformat(),
        "decision": verdict.decision.value,
        "risks": list(verdict.reasons),
        "required_approvers": verdict.required_approvers,
        "approval_strength": verdict.approval_strength,
        "policy_version": verdict.policy_version,
        "intent_digest": digest,
    }
