"""Recurring obligations (M3): approved once, renewed exactly, changed never.

The spec's recurring rule, executable: the FIRST subscription walks the
full ladder (M0 already forces approval for any recurring offer), and the
obligation this module records is born only from that authorized intent.
From then on the rule splits clean:

- a renewal whose terms are IDENTICAL to the approved ones proceeds — the
  approved thing happening again is not a new decision; the spine mints an
  auto-approved renewal intent (idempotent per period) whose digest still
  runs against the live offer at placement, and whose delegation is still
  re-checked, so a revoked agent cannot renew;
- a price increase, term extension, or any material change REFUSES here
  and re-enters policy as a brand-new intent through the ordinary door —
  renewed evaluation, renewed approval, new digest.

Comparison is over the offer's material fields (the same set the digest
hashes), never a similarity judgment.
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .errors import MarketplaceError, WrongState
from .models import Offer
from .service import MarketplaceSpine
from .store import StoredIntent

_SCHEMA = """CREATE TABLE IF NOT EXISTS market_recurring (
    obligation_id TEXT PRIMARY KEY,
    tenant TEXT NOT NULL,
    principal TEXT NOT NULL,
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL
)"""


class RequiresNewApproval(MarketplaceError):
    """The renewal's terms changed materially: back through the ladder."""


def _material(offer: Offer) -> dict:
    """The offer's material fields — expiry and signature are bookkeeping."""
    return offer.model_dump(exclude={"expires_at", "signature"})


class RecurringObligation(BaseModel):
    model_config = ConfigDict(frozen=True)

    obligation_id: str
    tenant_id: str
    principal_id: str
    agent_id: str
    # The exact approved terms every renewal is compared against.
    offer: Offer
    founding_intent_id: str
    period_days: int = Field(gt=0)
    state: str = "active"  # active | cancelled
    renewals: int = 0
    category: str = ""
    delivery_destination: str = ""
    created_at: datetime


class RecurringBook:
    def __init__(self, conn, *, audit) -> None:
        self._conn = conn
        self._audit = audit
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def _save(self, obligation: RecurringObligation) -> RecurringObligation:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO market_recurring
                   (obligation_id, tenant, principal, state, payload_json)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(obligation_id) DO UPDATE SET
                     state = excluded.state,
                     payload_json = excluded.payload_json""",
                (
                    obligation.obligation_id,
                    obligation.tenant_id,
                    obligation.principal_id,
                    obligation.state,
                    obligation.model_dump_json(),
                ),
            )
        return obligation

    def get(self, obligation_id: str, *, tenant: str) -> RecurringObligation | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json FROM market_recurring"
                " WHERE obligation_id = ? AND tenant = ?",
                (obligation_id, tenant),
            ).fetchone()
        if row is None:
            return None
        return RecurringObligation.model_validate_json(row["payload_json"])

    def list_for(self, *, tenant: str, principal: str) -> list[RecurringObligation]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT payload_json FROM market_recurring"
                " WHERE tenant = ? AND principal = ?",
                (tenant, principal),
            ).fetchall()
        records = [
            RecurringObligation.model_validate_json(row["payload_json"])
            for row in rows
        ]
        records.sort(key=lambda r: (r.created_at.isoformat(), r.obligation_id))
        return records

    def create_from_intent(
        self,
        spine: MarketplaceSpine,
        intent_id: str,
        *,
        tenant: str,
        period_days: int,
        now: datetime,
    ) -> RecurringObligation:
        """The obligation exists only because the ladder already ran: the
        founding intent must be a RECURRING offer the spine AUTHORIZED —
        which for a recurring offer always meant an approval."""
        stored = spine.get_intent(intent_id, tenant=tenant)
        if not stored.intent.offer_snapshot.recurring_terms:
            raise WrongState("the intent's offer is not recurring")
        if stored.state != "authorized":
            raise WrongState(
                f"the founding intent is {stored.state}, not authorized — "
                "no recurring obligation exists without the full policy pass"
            )
        obligation = RecurringObligation(
            obligation_id=uuid4().hex,
            tenant_id=tenant,
            principal_id=stored.intent.principal_id,
            agent_id=stored.intent.agent_id,
            offer=stored.intent.offer_snapshot,
            founding_intent_id=intent_id,
            period_days=period_days,
            category=stored.intent.category,
            delivery_destination=stored.intent.delivery_destination,
            created_at=now,
        )
        self._save(obligation)
        self._audit.append(
            "market.recurring.created",
            {
                "tenant": tenant,
                "obligation_id": obligation.obligation_id,
                "principal": obligation.principal_id,
                "founding_intent": intent_id,
                "period_days": period_days,
                "amount_micros": obligation.offer.total_micros,
            },
        )
        return obligation

    def renew(
        self,
        spine: MarketplaceSpine,
        obligation_id: str,
        *,
        tenant: str,
        current_offer: Offer,
        now: datetime,
    ) -> StoredIntent:
        """One period's renewal. Identical terms proceed; anything
        material that moved refuses and re-enters policy."""
        obligation = self.get(obligation_id, tenant=tenant)
        if obligation is None or obligation.state != "active":
            raise WrongState("no active obligation here")
        if _material(current_offer) != _material(obligation.offer):
            changed = [
                key
                for key, value in _material(current_offer).items()
                if _material(obligation.offer).get(key) != value
            ]
            self._audit.append(
                "market.recurring.change_refused",
                {
                    "tenant": tenant,
                    "obligation_id": obligation_id,
                    "changed_terms": changed,
                },
            )
            raise RequiresNewApproval(
                "the renewal changes approved terms "
                f"({', '.join(changed)}); a new intent and policy pass "
                "are required"
            )
        renewal = spine.mint_renewal(
            obligation_offer=obligation.offer,
            tenant=tenant,
            principal=obligation.principal_id,
            agent=obligation.agent_id,
            category=obligation.category,
            delivery_destination=obligation.delivery_destination,
            idempotency_key=(
                f"renewal:{obligation_id}:{obligation.renewals + 1}"
            ),
            reason=(
                f"renewal {obligation.renewals + 1} within the approved "
                f"recurring terms of obligation {obligation_id}"
            ),
            now=now,
        )
        self._save(
            obligation.model_copy(update={"renewals": obligation.renewals + 1})
        )
        self._audit.append(
            "market.recurring.renewed",
            {
                "tenant": tenant,
                "obligation_id": obligation_id,
                "renewal": obligation.renewals + 1,
                "intent_id": renewal.intent.intent_id,
            },
        )
        return renewal

    def cancel(
        self, obligation_id: str, *, tenant: str, principal: str, now: datetime
    ) -> RecurringObligation:
        obligation = self.get(obligation_id, tenant=tenant)
        if obligation is None or obligation.principal_id != principal:
            raise WrongState("no such obligation")
        cancelled = obligation.model_copy(update={"state": "cancelled"})
        self._save(cancelled)
        self._audit.append(
            "market.recurring.cancelled",
            {"tenant": tenant, "obligation_id": obligation_id},
        )
        return cancelled
