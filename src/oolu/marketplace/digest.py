"""The immutable intent digest — the one hash an approval ever binds to.

Canonical JSON (sorted keys, tight separators — the audit chain's own
discipline) over exactly the spec's ``intent_digest_fields``, hashed with
SHA-256. The field set is the definition of "material": change any of these
and the digest changes, so the approval dies; change anything else (an
idempotency key, a stored ceiling, bookkeeping timestamps) and the digest
holds. That equivalence is pinned by a property test, because the digest IS
the approval contract — a field accidentally left out of this set would be a
term a seller could change under an approved purchase.

Pure functions only: no clock, no store, no I/O.
"""

from __future__ import annotations

import hashlib
import json

from .models import CommercialIntent, Offer

# The spec's intent_digest_fields, in its order — documentation of scope;
# the canonical serialization below sorts keys itself.
DIGEST_FIELDS = (
    "principal_id",
    "agent_id",
    "action",
    "offer_version",
    "counterparty_id",
    "product_or_service_id",
    "quantity",
    "total_amount",
    "currency",
    "taxes",
    "fees",
    "delivery_terms",
    "refund_terms",
    "recurring_terms",
    "milestones",
    "data_permissions",
    "expiration",
    "policy_version",
)


def intent_digest(
    intent: CommercialIntent,
    *,
    offer: Offer | None = None,
    policy_version: str,
) -> str:
    """Digest the intent against ``offer`` (default: its own snapshot).

    Approval time digests the snapshot — the exact terms the human saw.
    Execution time digests the LIVE offer; if the seller changed anything
    material (price, version, quantity, terms, even the tax line), the two
    digests differ and execution is refused.
    """
    offer = offer if offer is not None else intent.offer_snapshot
    material = {
        "principal_id": intent.principal_id,
        "agent_id": intent.agent_id,
        "action": intent.action.value,
        "offer_version": offer.offer_version,
        "counterparty_id": offer.seller_id,
        "product_or_service_id": offer.item_id,
        "quantity": offer.quantity,
        "total_amount": offer.total_micros,
        "currency": offer.currency,
        "taxes": offer.tax_estimate_micros,
        "fees": offer.fees_micros,
        "delivery_terms": offer.fulfillment_terms,
        "refund_terms": offer.refund_terms,
        "recurring_terms": offer.recurring_terms,
        "milestones": [list(m) for m in offer.milestones],
        "data_permissions": sorted(intent.data_permissions),
        "expiration": intent.expires_at.isoformat(),
        "policy_version": policy_version,
    }
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
