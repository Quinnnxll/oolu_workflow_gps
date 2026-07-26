"""The order state machine (M1): from authorized intent to settled money.

An order is born only from the commercial spine's authorization — the digest
law has already run by the time a record exists here — and then walks the
spec's state machine with the payment discipline the plan fixes: AUTHORIZE
at confirmation, CAPTURE on acceptance, REFUND as an exact compensating
transaction. Order state and payment-provider state are kept separate and
reconciled (a provider webhook replays into the same idempotent transitions
and postings), and every transition lands on the audit chain.

Money-mode: a live payment provider demands the production substrate
(``require_production_money``) before any authorize/capture/refund call; the
pre-launch ``FakePsp`` moves nothing. The local SQLite adapter cannot wrap
the spine's authorization and the order insert in one transaction — the
production PostgreSQL adapter is where that seam closes (plan §5 M1 notes).

The full spec state set, for the record — the intent (M0) owns the first
four; this machine owns the rest:

    draft, policy_pending, approval_pending, approved,      (the intent's)
    payment_authorizing, confirmed, fulfilling, delivered,
    accepted, completed, cancellation_pending, cancelled,
    refund_pending, refunded, disputed, resolved
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from ..billing.doubleentry import DoubleEntryLedger, LedgerEntry, LedgerTransaction
from ..billing.guard import require_production_money
from ..billing.payout import PaymentError
from ..billing.psp import PaymentProviderPort
from .errors import MarketNotFound, WrongState
from .models import Offer
from .service import MarketplaceSpine

_SCHEMA = """CREATE TABLE IF NOT EXISTS market_orders (
    order_id TEXT PRIMARY KEY,
    tenant TEXT NOT NULL,
    buyer TEXT NOT NULL,
    seller TEXT NOT NULL,
    intent_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (tenant, intent_id),
    UNIQUE (tenant, idempotency_key)
)"""

# 500 basis points — the interim seller-side flat take from the plan's
# decision log; a constructor knob, not a constant of nature.
DEFAULT_TAKE_RATE_BPS = 500


class OrderRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    order_id: str
    tenant_id: str
    buyer_principal: str
    seller_id: str
    # The seller's host principal when the order came from a hosted
    # listing; empty for offers sourced outside the host.
    seller_principal: str = ""
    intent_id: str
    authorization_id: str
    intent_digest: str
    offer: Offer
    idempotency_key: str
    take_rate_bps: int
    auth_ref: str | None = None
    charge_ref: str | None = None
    refund_ref: str | None = None
    tracking: str = ""
    delivery_evidence: str = ""
    created_at: datetime


class StoredOrder:
    def __init__(self, record: OrderRecord, *, state: str) -> None:
        self.record = record
        self.state = state


class OrderStore:
    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def add(self, record: OrderRecord, *, state: str) -> tuple[StoredOrder, bool]:
        existing = self.by_intent(record.intent_id, tenant=record.tenant_id)
        if existing is not None:
            return existing, False
        with self._conn.transaction() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO market_orders"
                " (order_id, tenant, buyer, seller, intent_id, idempotency_key,"
                "  state, payload_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.order_id,
                    record.tenant_id,
                    record.buyer_principal,
                    record.seller_principal,
                    record.intent_id,
                    record.idempotency_key,
                    state,
                    record.model_dump_json(),
                    record.created_at.isoformat(),
                ),
            )
            created = cursor.rowcount > 0
        if not created:
            settled = self.by_intent(record.intent_id, tenant=record.tenant_id)
            assert settled is not None
            return settled, False
        return StoredOrder(record, state=state), True

    def get(self, order_id: str, *, tenant: str) -> StoredOrder | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json, state FROM market_orders"
                " WHERE order_id = ? AND tenant = ?",
                (order_id, tenant),
            ).fetchone()
        return None if row is None else self._row(row)

    def by_intent(self, intent_id: str, *, tenant: str) -> StoredOrder | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json, state FROM market_orders"
                " WHERE tenant = ? AND intent_id = ?",
                (tenant, intent_id),
            ).fetchone()
        return None if row is None else self._row(row)

    def set_state(
        self,
        order_id: str,
        *,
        tenant: str,
        state: str,
        expected: tuple[str, ...],
    ) -> bool:
        marks = ",".join("?" for _ in expected)
        with self._conn.transaction() as db:
            cursor = db.execute(
                "UPDATE market_orders SET state = ?"
                f" WHERE order_id = ? AND tenant = ? AND state IN ({marks})",
                (state, order_id, tenant, *expected),
            )
            return cursor.rowcount > 0

    def update_record(self, record: OrderRecord) -> None:
        with self._conn.transaction() as db:
            db.execute(
                "UPDATE market_orders SET payload_json = ?"
                " WHERE order_id = ? AND tenant = ?",
                (record.model_dump_json(), record.order_id, record.tenant_id),
            )

    def list_for(self, *, tenant: str, principal: str) -> list[StoredOrder]:
        """The principal's orders, as buyer or as hosted seller."""
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT payload_json, state FROM market_orders"
                " WHERE tenant = ? AND (buyer = ? OR seller = ?)",
                (tenant, principal, principal),
            ).fetchall()
        orders = [self._row(row) for row in rows]
        orders.sort(key=lambda o: (o.record.created_at.isoformat(), o.record.order_id))
        return orders

    @staticmethod
    def _row(row) -> StoredOrder:
        return StoredOrder(
            OrderRecord.model_validate_json(row["payload_json"]),
            state=row["state"],
        )


class OrderService:
    """Placement, fulfillment, capture, and refunds over the spine's law."""

    def __init__(
        self,
        conn,
        *,
        audit,
        spine: MarketplaceSpine,
        psp: PaymentProviderPort,
        ledger: DoubleEntryLedger,
        durable=None,
        providers: tuple = (),
        take_rate_bps: int = DEFAULT_TAKE_RATE_BPS,
    ) -> None:
        self.orders = OrderStore(conn)
        self._audit = audit
        self._spine = spine
        self._psp = psp
        self._ledger = ledger
        # The money-mode gate reads is_production_durable off this object;
        # the local connection honestly says False.
        self._durable = durable if durable is not None else conn
        self._providers = tuple(providers)
        self._take_rate_bps = take_rate_bps

    @property
    def psp_mode(self) -> str:
        return self._psp.mode

    def _guard_money(self) -> None:
        """A live provider moves real money: production substrate only.
        The pre-launch fake moves nothing and needs no gate."""
        if self._psp.mode == "live":
            require_production_money(self._durable, self._providers)

    # ------------------------------------------------------------------ #
    # Placement: the spine authorizes, the provider authorizes, no capture.#
    # ------------------------------------------------------------------ #
    def place_order(
        self,
        *,
        intent_id: str,
        tenant: str,
        now: datetime,
        customer_ref: str,
        payment_method_ref: str,
        current_offer: Offer | None = None,
        seller_principal: str = "",
    ) -> tuple[StoredOrder, bool]:
        """Place the order for an intent — exactly once per intent.

        A duplicate request (same intent, hence same idempotency key)
        returns the existing order without touching the spine or the
        provider — the spec's duplicate-execution acceptance test.
        """
        existing = self.orders.by_intent(intent_id, tenant=tenant)
        if existing is not None:
            return existing, False
        self._guard_money()
        stored_intent = self._spine.get_intent(intent_id, tenant=tenant)
        offer = current_offer or stored_intent.intent.offer_snapshot
        authorization = self._spine.authorize_execution(
            intent_id, tenant=tenant, current_offer=offer, now=now
        )
        record = OrderRecord(
            order_id=uuid4().hex,
            tenant_id=tenant,
            buyer_principal=stored_intent.intent.principal_id,
            seller_id=offer.seller_id,
            seller_principal=seller_principal,
            intent_id=intent_id,
            authorization_id=authorization.authorization_id,
            intent_digest=authorization.intent_digest,
            offer=offer,
            idempotency_key=stored_intent.intent.idempotency_key,
            take_rate_bps=self._take_rate_bps,
            created_at=now,
        )
        order, created = self.orders.add(record, state="payment_authorizing")
        if not created:
            return order, False
        self._audit.append(
            "market.order.placed",
            {
                "tenant": tenant,
                "order_id": record.order_id,
                "intent_id": intent_id,
                "buyer": record.buyer_principal,
                "seller": record.seller_id,
                "total_micros": offer.total_micros,
                "currency": offer.currency,
            },
        )
        try:
            auth_ref = self._psp.authorize(
                amount_micros=offer.total_micros,
                currency=offer.currency,
                customer_ref=customer_ref,
                payment_method_ref=payment_method_ref,
                idempotency_key=f"auth:{tenant}:{record.idempotency_key}",
                metadata={"oolu_order_id": record.order_id},
            )
        except PaymentError as exc:
            self.orders.set_state(
                record.order_id,
                tenant=tenant,
                state="cancelled",
                expected=("payment_authorizing",),
            )
            self._audit.append(
                "market.payment.declined",
                {"tenant": tenant, "order_id": record.order_id, "reason": str(exc)},
            )
            raise
        updated = record.model_copy(update={"auth_ref": auth_ref})
        self.orders.update_record(updated)
        self.orders.set_state(
            record.order_id,
            tenant=tenant,
            state="confirmed",
            expected=("payment_authorizing",),
        )
        self._audit.append(
            "market.payment.authorized",
            {
                "tenant": tenant,
                "order_id": record.order_id,
                "auth_ref": auth_ref,
                "amount_micros": offer.total_micros,
            },
        )
        self._audit.append(
            "market.order.confirmed",
            {"tenant": tenant, "order_id": record.order_id},
        )
        return StoredOrder(updated, state="confirmed"), True

    # ------------------------------------------------------------------ #
    # Fulfillment: ship, deliver, accept — capture only on acceptance.    #
    # ------------------------------------------------------------------ #
    def _get(self, order_id: str, *, tenant: str) -> StoredOrder:
        order = self.orders.get(order_id, tenant=tenant)
        if order is None:
            raise MarketNotFound("no such order")
        return order

    def _party(self, order: StoredOrder, actor: str, *, buyer_only: bool = False) -> None:
        record = order.record
        allowed = {record.buyer_principal}
        if not buyer_only and record.seller_principal:
            allowed.add(record.seller_principal)
        if actor not in allowed:
            raise MarketNotFound("no such order")  # a stranger sees nothing

    def mark_shipped(
        self, order_id: str, *, tenant: str, actor: str, now: datetime, tracking: str = ""
    ) -> StoredOrder:
        order = self._get(order_id, tenant=tenant)
        self._party(order, actor)
        if not self.orders.set_state(
            order_id, tenant=tenant, state="fulfilling", expected=("confirmed",)
        ):
            raise WrongState(f"order is {order.state}, not confirmed")
        updated = order.record.model_copy(update={"tracking": tracking})
        self.orders.update_record(updated)
        self._audit.append(
            "market.order.shipped",
            {"tenant": tenant, "order_id": order_id, "tracking": tracking},
        )
        return StoredOrder(updated, state="fulfilling")

    def mark_delivered(
        self, order_id: str, *, tenant: str, actor: str, now: datetime, evidence: str = ""
    ) -> StoredOrder:
        order = self._get(order_id, tenant=tenant)
        self._party(order, actor)
        if not self.orders.set_state(
            order_id, tenant=tenant, state="delivered", expected=("fulfilling",)
        ):
            raise WrongState(f"order is {order.state}, not fulfilling")
        updated = order.record.model_copy(update={"delivery_evidence": evidence})
        self.orders.update_record(updated)
        self._audit.append(
            "market.order.delivered",
            {"tenant": tenant, "order_id": order_id, "evidence": evidence},
        )
        return StoredOrder(updated, state="delivered")

    def accept(
        self, order_id: str, *, tenant: str, actor: str, now: datetime
    ) -> StoredOrder:
        """Buyer acceptance: the irreversible step, so capture happens here
        — and only here."""
        order = self._get(order_id, tenant=tenant)
        self._party(order, actor, buyer_only=True)
        if not self.orders.set_state(
            order_id, tenant=tenant, state="accepted", expected=("delivered",)
        ):
            raise WrongState(f"order is {order.state}, not delivered")
        self._audit.append(
            "market.order.accepted", {"tenant": tenant, "order_id": order_id}
        )
        return self._capture(self._get(order_id, tenant=tenant), now=now)

    def _capture_entries(self, record: OrderRecord) -> tuple[LedgerEntry, ...]:
        offer = record.offer
        fee = offer.subtotal_micros * record.take_rate_bps // 10_000
        seller_share = offer.subtotal_micros + offer.fees_micros - fee
        entries = [
            LedgerEntry(account="marketplace_cash", amount_micros=offer.total_micros),
            LedgerEntry(account="seller_payable", amount_micros=-seller_share),
        ]
        if fee:
            entries.append(
                LedgerEntry(account="marketplace_fee_revenue", amount_micros=-fee)
            )
        if offer.tax_estimate_micros:
            entries.append(
                LedgerEntry(
                    account="tax_payable", amount_micros=-offer.tax_estimate_micros
                )
            )
        return tuple(entries)

    def _capture(self, order: StoredOrder, *, now: datetime) -> StoredOrder:
        record = order.record
        if record.auth_ref is None:
            raise WrongState("no payment authorization to capture")
        self._guard_money()
        charge_ref = self._psp.capture(
            record.auth_ref,
            amount_micros=record.offer.total_micros,
            idempotency_key=f"capture:{record.tenant_id}:{record.idempotency_key}",
        )
        txn, posted = self._ledger.post(
            LedgerTransaction(
                txn_id=uuid4().hex,
                idempotency_key=(
                    f"ledger:capture:{record.tenant_id}:{record.idempotency_key}"
                ),
                kind="capture",
                order_id=record.order_id,
                currency=record.offer.currency,
                entries=self._capture_entries(record),
                memo=f"order {record.order_id} captured",
                created_at=now,
            )
        )
        updated = record.model_copy(update={"charge_ref": charge_ref})
        self.orders.update_record(updated)
        self.orders.set_state(
            record.order_id,
            tenant=record.tenant_id,
            state="completed",
            expected=("accepted",),
        )
        if posted:
            self._audit.append(
                "market.payment.captured",
                {
                    "tenant": record.tenant_id,
                    "order_id": record.order_id,
                    "charge_ref": charge_ref,
                    "ledger_txn": txn.txn_id,
                    "amount_micros": record.offer.total_micros,
                },
            )
            self._audit.append(
                "market.order.completed",
                {"tenant": record.tenant_id, "order_id": record.order_id},
            )
        return StoredOrder(updated, state="completed")

    # ------------------------------------------------------------------ #
    # Cancellation and refund: always a compensating step, never an edit. #
    # ------------------------------------------------------------------ #
    def cancel(
        self, order_id: str, *, tenant: str, actor: str, now: datetime
    ) -> StoredOrder:
        order = self._get(order_id, tenant=tenant)
        self._party(order, actor)
        if not self.orders.set_state(
            order_id,
            tenant=tenant,
            state="cancellation_pending",
            expected=("payment_authorizing", "confirmed"),
        ):
            raise WrongState(f"order is {order.state}; cancel before fulfillment")
        if order.record.auth_ref is not None:
            self._guard_money()
            self._psp.void(
                order.record.auth_ref,
                idempotency_key=f"void:{tenant}:{order.record.idempotency_key}",
            )
        self.orders.set_state(
            order_id,
            tenant=tenant,
            state="cancelled",
            expected=("cancellation_pending",),
        )
        self._audit.append(
            "market.order.cancelled", {"tenant": tenant, "order_id": order_id}
        )
        return self._get(order_id, tenant=tenant)

    def refund(
        self,
        order_id: str,
        *,
        tenant: str,
        actor: str,
        now: datetime,
        reason: str = "",
    ) -> StoredOrder:
        """Full refund of a completed order: the provider reverses the
        charge and the ledger posts the capture's exact negation."""
        order = self._get(order_id, tenant=tenant)
        self._party(order, actor)
        record = order.record
        if record.charge_ref is None or not self.orders.set_state(
            order_id, tenant=tenant, state="refund_pending", expected=("completed",)
        ):
            raise WrongState(f"order is {order.state}, not completed")
        self._guard_money()
        refund_ref = self._psp.refund(
            record.charge_ref,
            amount_micros=record.offer.total_micros,
            idempotency_key=f"refund:{tenant}:{record.idempotency_key}",
        )
        capture = self._ledger.by_key(
            f"ledger:capture:{tenant}:{record.idempotency_key}"
        )
        assert capture is not None  # a completed order always has its capture
        self._ledger.post(
            LedgerTransaction(
                txn_id=uuid4().hex,
                idempotency_key=f"ledger:refund:{tenant}:{record.idempotency_key}",
                kind="refund",
                order_id=record.order_id,
                currency=record.offer.currency,
                entries=tuple(entry.negated() for entry in capture.entries),
                memo=f"order {record.order_id} refunded: {reason}",
                created_at=now,
                reverses_txn_id=capture.txn_id,
            )
        )
        updated = record.model_copy(update={"refund_ref": refund_ref})
        self.orders.update_record(updated)
        self.orders.set_state(
            order_id, tenant=tenant, state="refunded", expected=("refund_pending",)
        )
        self._audit.append(
            "market.payment.refunded",
            {
                "tenant": tenant,
                "order_id": order_id,
                "refund_ref": refund_ref,
                "amount_micros": record.offer.total_micros,
                "reason": reason,
            },
        )
        return self._get(order_id, tenant=tenant)

    # ------------------------------------------------------------------ #
    # Disputes: manual review in M1 — states and audit only.              #
    # ------------------------------------------------------------------ #
    def open_dispute(
        self, order_id: str, *, tenant: str, actor: str, now: datetime, reason: str
    ) -> StoredOrder:
        order = self._get(order_id, tenant=tenant)
        self._party(order, actor)
        if not self.orders.set_state(
            order_id,
            tenant=tenant,
            state="disputed",
            expected=("delivered", "accepted", "completed"),
        ):
            raise WrongState(f"order is {order.state}; nothing to dispute")
        self._audit.append(
            "market.order.disputed",
            {"tenant": tenant, "order_id": order_id, "reason": reason},
        )
        return self._get(order_id, tenant=tenant)

    def resolve_dispute(
        self, order_id: str, *, tenant: str, actor: str, now: datetime, resolution: str
    ) -> StoredOrder:
        order = self._get(order_id, tenant=tenant)
        self._party(order, actor)
        if not self.orders.set_state(
            order_id, tenant=tenant, state="resolved", expected=("disputed",)
        ):
            raise WrongState(f"order is {order.state}, not disputed")
        self._audit.append(
            "market.order.resolved",
            {"tenant": tenant, "order_id": order_id, "resolution": resolution},
        )
        return self._get(order_id, tenant=tenant)

    # ------------------------------------------------------------------ #
    # Provider reconciliation: webhooks replay into the same idempotent   #
    # transitions and postings — duplicates change nothing.               #
    # ------------------------------------------------------------------ #
    def process_psp_event(self, event: dict, *, now: datetime) -> dict:
        """Reconcile a provider event against order and ledger state.

        The provider is never the source of truth: an event can only drive
        the order's own idempotent machinery. A duplicate delivery finds
        the transition already made and the posting already keyed, and
        changes nothing — the spec's duplicate-webhook acceptance test.
        """
        kind = str(event.get("type") or "")
        tenant = str(event.get("tenant") or "")
        order_id = str(event.get("order_id") or "")
        order = self.orders.get(order_id, tenant=tenant)
        if order is None:
            return {"applied": False, "reason": "unknown order"}
        record = order.record
        if kind == "payment.captured":
            # Completion whose confirmation was lost: finish it. Replays
            # find state already 'completed' and the posting already keyed.
            if order.state == "accepted":
                self._capture(order, now=now)
                return {"applied": True}
            return {"applied": False, "reason": f"order is {order.state}"}
        if kind == "payment.refunded":
            if order.state == "refunded":
                return {"applied": False, "reason": "already refunded"}
            if order.state == "refund_pending":
                capture = self._ledger.by_key(
                    f"ledger:capture:{tenant}:{record.idempotency_key}"
                )
                if capture is None:
                    return {"applied": False, "reason": "no capture to reverse"}
                self._ledger.post(
                    LedgerTransaction(
                        txn_id=uuid4().hex,
                        idempotency_key=(
                            f"ledger:refund:{tenant}:{record.idempotency_key}"
                        ),
                        kind="refund",
                        order_id=record.order_id,
                        currency=record.offer.currency,
                        entries=tuple(e.negated() for e in capture.entries),
                        memo=f"order {record.order_id} refunded (provider event)",
                        created_at=now,
                        reverses_txn_id=capture.txn_id,
                    )
                )
                self.orders.set_state(
                    order_id,
                    tenant=tenant,
                    state="refunded",
                    expected=("refund_pending",),
                )
                return {"applied": True}
            return {"applied": False, "reason": f"order is {order.state}"}
        return {"applied": False, "reason": f"unknown event type '{kind}'"}
