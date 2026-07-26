"""RFQ (M2): one buyer's specification, many sellers' quotes, one award.

The request names a typed specification — category, required attributes,
quantity — and quotes are judged against it BEFORE anything else looks at
them: a quote whose attributes miss a required value is stored as
INELIGIBLE with its gaps named, never comparable, never awardable, never
reaching policy evaluation (the spec's substitute acceptance test). The
seller side is gated by the seller's own signed sales policy at submission
— a quote below the absolute floor is refused without model discretion.

Comparison is normalized: eligible quotes, ordered by landed total, each
row carrying the same fields so an agent (or a human) compares like with
like. The award returns the winning quote's exact versioned offer — which
feeds the commercial spine's intent door unchanged; RFQ discovers terms,
it never bypasses the law.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .errors import MarketNotFound, MarketplaceError, WrongState
from .models import Offer
from .policy import Decision, SaleFacts, SalesPolicy, evaluate_sale

_RFQ_SCHEMA = """CREATE TABLE IF NOT EXISTS market_rfqs (
    rfq_id TEXT PRIMARY KEY,
    tenant TEXT NOT NULL,
    buyer TEXT NOT NULL,
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL
)"""

_QUOTES_SCHEMA = """CREATE TABLE IF NOT EXISTS market_quotes (
    quote_id TEXT PRIMARY KEY,
    rfq_id TEXT NOT NULL,
    payload_json TEXT NOT NULL
)"""


class QuoteRefused(MarketplaceError):
    """The quote cannot be taken as submitted."""


class RfqSpecification(BaseModel):
    """What the buyer requires — the eligibility bar every quote meets
    or is marked by."""

    model_config = ConfigDict(frozen=True)

    category: str
    required_attributes: tuple[tuple[str, str], ...] = ()
    quantity: int = Field(default=1, gt=0)
    destination_reference: str = ""
    deadline: datetime | None = None


class RequestForQuote(BaseModel):
    model_config = ConfigDict(frozen=True)

    rfq_id: str
    tenant_id: str
    buyer_principal: str
    specification: RfqSpecification
    state: str = "open"  # open | awarded | closed | expired
    awarded_quote_id: str | None = None
    created_at: datetime
    expires_at: datetime


class QuoteRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    quote_id: str
    rfq_id: str
    seller_principal: str
    attributes: tuple[tuple[str, str], ...] = ()
    offer: Offer
    eligible: bool
    ineligible_reasons: tuple[str, ...] = ()
    submitted_at: datetime


def specification_gaps(
    specification: RfqSpecification, *, attributes: dict[str, str], offer: Offer
) -> tuple[str, ...]:
    """Every requirement the quote misses. Non-empty = ineligible, and it
    is marked so BEFORE any policy evaluation ever sees the offer."""
    gaps: list[str] = []
    if offer.quantity != specification.quantity:
        gaps.append(
            f"quantity {offer.quantity} does not meet the required "
            f"{specification.quantity}"
        )
    for name, required in specification.required_attributes:
        supplied = attributes.get(name)
        if supplied is None:
            gaps.append(f"required attribute '{name}' is missing")
        elif supplied != required:
            gaps.append(
                f"attribute '{name}' is '{supplied}', not the required "
                f"'{required}'"
            )
    return tuple(gaps)


class RfqService:
    def __init__(self, conn, *, audit) -> None:
        self._conn = conn
        self._audit = audit
        with self._conn.transaction() as db:
            db.execute(_RFQ_SCHEMA)
            db.execute(_QUOTES_SCHEMA)

    # ------------------------------------------------------------------ #
    # The request.                                                        #
    # ------------------------------------------------------------------ #
    def open(
        self,
        *,
        tenant: str,
        buyer: str,
        specification: RfqSpecification,
        now: datetime,
        expires_in_minutes: int = 7 * 24 * 60,
    ) -> RequestForQuote:
        rfq = RequestForQuote(
            rfq_id=uuid4().hex,
            tenant_id=tenant,
            buyer_principal=buyer,
            specification=specification,
            created_at=now,
            expires_at=now + timedelta(minutes=expires_in_minutes),
        )
        self._save(rfq)
        self._audit.append(
            "market.rfq.opened",
            {
                "tenant": tenant,
                "rfq_id": rfq.rfq_id,
                "buyer": buyer,
                "category": specification.category,
                "quantity": specification.quantity,
            },
        )
        return rfq

    def get(self, rfq_id: str, *, tenant: str) -> RequestForQuote:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json FROM market_rfqs"
                " WHERE rfq_id = ? AND tenant = ?",
                (rfq_id, tenant),
            ).fetchone()
        if row is None:
            raise MarketNotFound("no such request for quotes")
        return RequestForQuote.model_validate_json(row["payload_json"])

    def open_requests(self, *, tenant: str, now: datetime) -> list[RequestForQuote]:
        """What sellers browse: every live request in the tenant."""
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT payload_json FROM market_rfqs"
                " WHERE tenant = ? AND state = 'open'",
                (tenant,),
            ).fetchall()
        requests = [
            RequestForQuote.model_validate_json(row["payload_json"])
            for row in rows
        ]
        live = [r for r in requests if r.expires_at > now]
        live.sort(key=lambda r: (r.created_at.isoformat(), r.rfq_id))
        return live

    # ------------------------------------------------------------------ #
    # Quotes.                                                             #
    # ------------------------------------------------------------------ #
    def submit_quote(
        self,
        rfq_id: str,
        *,
        tenant: str,
        seller_principal: str,
        offer: Offer,
        attributes: dict[str, str] | None = None,
        sales_policy: SalesPolicy | None = None,
        sale_facts: SaleFacts | None = None,
        now: datetime,
    ) -> QuoteRecord:
        """File a quote. The seller's own signed floor gates it — a quote
        below the absolute floor never enters the book — and eligibility
        against the buyer's specification is judged and STORED here."""
        rfq = self.get(rfq_id, tenant=tenant)
        if rfq.state != "open":
            raise WrongState(f"the request is {rfq.state}")
        if rfq.expires_at <= now:
            self._save(rfq.model_copy(update={"state": "expired"}))
            raise WrongState("the request expired")
        if sales_policy is not None:
            facts = sale_facts or SaleFacts(
                final_price_micros=offer.subtotal_micros // max(offer.quantity, 1),
                currency=offer.currency,
                quantity=offer.quantity,
                inventory_available=True,
                fulfillment_capacity_available=True,
                buyer_risk_acceptable=True,
            )
            verdict = evaluate_sale(sales_policy, facts)
            if verdict.decision is Decision.DENY:
                self._audit.append(
                    "market.quote.refused",
                    {
                        "tenant": tenant,
                        "rfq_id": rfq_id,
                        "seller": seller_principal,
                        "reasons": list(verdict.reasons),
                    },
                )
                raise QuoteRefused("; ".join(verdict.reasons))
        gaps = specification_gaps(
            rfq.specification, attributes=dict(attributes or {}), offer=offer
        )
        record = QuoteRecord(
            quote_id=uuid4().hex,
            rfq_id=rfq_id,
            seller_principal=seller_principal,
            attributes=tuple(sorted((attributes or {}).items())),
            offer=offer,
            eligible=not gaps,
            ineligible_reasons=gaps,
            submitted_at=now,
        )
        with self._conn.transaction() as db:
            db.execute(
                "INSERT INTO market_quotes (quote_id, rfq_id, payload_json)"
                " VALUES (?, ?, ?)",
                (record.quote_id, rfq_id, record.model_dump_json()),
            )
        self._audit.append(
            "market.quote.submitted",
            {
                "tenant": tenant,
                "rfq_id": rfq_id,
                "quote_id": record.quote_id,
                "seller": seller_principal,
                "total_micros": offer.total_micros,
                "eligible": record.eligible,
                "ineligible_reasons": list(gaps),
            },
        )
        return record

    def quotes(self, rfq_id: str, *, tenant: str) -> list[QuoteRecord]:
        """The normalized comparison: eligible quotes by landed total,
        ineligible quotes after them with their gaps named."""
        self.get(rfq_id, tenant=tenant)  # tenancy + existence
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT payload_json FROM market_quotes WHERE rfq_id = ?",
                (rfq_id,),
            ).fetchall()
        records = [
            QuoteRecord.model_validate_json(row["payload_json"]) for row in rows
        ]
        records.sort(
            key=lambda q: (not q.eligible, q.offer.total_micros, q.quote_id)
        )
        return records

    def award(
        self, rfq_id: str, quote_id: str, *, tenant: str, buyer: str, now: datetime
    ) -> Offer:
        """Award the request to one ELIGIBLE quote and hand back its exact
        offer — the thing the buyer's intent binds to. Discovery ends
        here; the spine's law takes over."""
        rfq = self.get(rfq_id, tenant=tenant)
        if rfq.buyer_principal != buyer:
            raise MarketNotFound("no such request for quotes")
        if rfq.state != "open":
            raise WrongState(f"the request is {rfq.state}")
        chosen = next(
            (q for q in self.quotes(rfq_id, tenant=tenant) if q.quote_id == quote_id),
            None,
        )
        if chosen is None:
            raise MarketNotFound("no such quote")
        if not chosen.eligible:
            raise WrongState(
                "the quote is ineligible: "
                + "; ".join(chosen.ineligible_reasons)
            )
        self._save(
            rfq.model_copy(update={"state": "awarded", "awarded_quote_id": quote_id})
        )
        self._audit.append(
            "market.rfq.awarded",
            {
                "tenant": tenant,
                "rfq_id": rfq_id,
                "quote_id": quote_id,
                "seller": chosen.seller_principal,
                "total_micros": chosen.offer.total_micros,
            },
        )
        return chosen.offer

    def _save(self, rfq: RequestForQuote) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO market_rfqs
                   (rfq_id, tenant, buyer, state, payload_json)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(rfq_id) DO UPDATE SET
                     state = excluded.state,
                     payload_json = excluded.payload_json""",
                (
                    rfq.rfq_id,
                    rfq.tenant_id,
                    rfq.buyer_principal,
                    rfq.state,
                    rfq.model_dump_json(),
                ),
            )
