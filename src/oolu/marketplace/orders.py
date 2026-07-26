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

from datetime import datetime, timedelta
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from ..billing.doubleentry import DoubleEntryLedger, LedgerEntry, LedgerTransaction
from ..billing.escrow import EscrowPolicy, capture_entries, release_entries
from ..billing.guard import require_production_money
from ..billing.payout import PaymentError
from ..billing.psp import PaymentProviderPort
from ..billing.tax import InvoiceBook, JurisdictionModule
from .errors import ListingUnavailable, MarketNotFound, WrongState
from .inventory import InventoryService
from .milestones import MilestoneBook, MilestoneState
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
    # M2: whether captured money waits in escrow until a release
    # condition holds, and the clock the acceptance timeout reads.
    escrow_held: bool = False
    delivered_at: datetime | None = None
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

    def in_state(self, state: str) -> list[StoredOrder]:
        """Every order in one state, host-wide — the sweep's read."""
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT payload_json, state FROM market_orders WHERE state = ?",
                (state,),
            ).fetchall()
        orders = [self._row(row) for row in rows]
        orders.sort(key=lambda o: (o.record.created_at.isoformat(), o.record.order_id))
        return orders

    def for_seller(self, *, seller_id: str) -> list[StoredOrder]:
        """Every order naming this commercial seller — the reputation
        aggregate's read. Rates only ever leave it, never buyer data."""
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT payload_json, state FROM market_orders"
            ).fetchall()
        orders = [self._row(row) for row in rows]
        return [o for o in orders if o.record.seller_id == seller_id]

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
        escrow: EscrowPolicy | None = None,
        inventory: InventoryService | None = None,
        invoices: InvoiceBook | None = None,
        jurisdiction: JurisdictionModule | None = None,
    ) -> None:
        self.orders = OrderStore(conn)
        self.milestones = MilestoneBook(conn)
        self._audit = audit
        self._spine = spine
        self._psp = psp
        self._ledger = ledger
        # The money-mode gate reads is_production_durable off this object;
        # the local connection honestly says False.
        self._durable = durable if durable is not None else conn
        self._providers = tuple(providers)
        self._take_rate_bps = take_rate_bps
        # M2: escrow discipline, atomic stock, and the invoice book.
        # None = the M1 direct-settlement flow (escrow is a policy choice).
        self._escrow = escrow
        self._inventory = inventory
        self._invoices = invoices
        self._jurisdiction = jurisdiction

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
        digital: bool = False,
    ) -> tuple[StoredOrder, bool]:
        """Place the order for an intent — exactly once per intent.

        A duplicate request (same intent, hence same idempotency key)
        returns the existing order without touching the spine or the
        provider — the spec's duplicate-execution acceptance test. When
        the offer's item is inventory-tracked, the units are RESERVED
        atomically before any payment call; a sold-out shelf refuses
        here, never after money moved.
        """
        existing = self.orders.by_intent(intent_id, tenant=tenant)
        if existing is not None:
            return existing, False
        self._guard_money()
        stored_intent = self._spine.get_intent(intent_id, tenant=tenant)
        offer = current_offer or stored_intent.intent.offer_snapshot
        reservation = None
        if self._inventory is not None and self._inventory.tracked(offer.item_id):
            reservation = self._inventory.reserve(
                offer.item_id,
                quantity=offer.quantity,
                holder=intent_id,
                now=now,
            )
            if reservation is None:
                raise ListingUnavailable(
                    "the listing cannot cover the quantity right now"
                )
        try:
            authorization = self._spine.authorize_execution(
                intent_id, tenant=tenant, current_offer=offer, now=now
            )
        except Exception:
            if reservation is not None and self._inventory is not None:
                self._inventory.release(reservation.reservation_id)
            raise
        held = self._escrow is not None and self._escrow.holds(
            total_micros=offer.total_micros,
            seller_id=offer.seller_id,
            digital=digital,
        )
        if offer.milestones:
            # A payment schedule IS an escrow arrangement: tranches release
            # one accepted milestone at a time; no policy exception applies.
            if self._escrow is None:
                raise WrongState(
                    "milestone offers require an escrow policy on this host"
                )
            held = True
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
            escrow_held=held,
            created_at=now,
        )
        order, created = self.orders.add(record, state="payment_authorizing")
        if not created:
            return order, False
        if offer.milestones:
            self.milestones.seed(record.order_id, offer.milestones)
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
                # Round-trips through the provider onto webhook events, so
                # a delivery can find the order it reconciles against.
                metadata={"oolu_order_id": record.order_id, "oolu_tenant": tenant},
            )
        except PaymentError as exc:
            self.orders.set_state(
                record.order_id,
                tenant=tenant,
                state="cancelled",
                expected=("payment_authorizing",),
            )
            if reservation is not None and self._inventory is not None:
                self._inventory.release(reservation.reservation_id)
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
        """Delivery confirmation. Escrow orders CAPTURE here — but only on
        evidence: a delivery nobody can verify keeps the buyer's money
        uncaptured and files an exception instead of trusting the claim."""
        order = self._get(order_id, tenant=tenant)
        self._party(order, actor)
        if not self.orders.set_state(
            order_id, tenant=tenant, state="delivered", expected=("fulfilling",)
        ):
            raise WrongState(f"order is {order.state}, not fulfilling")
        updated = order.record.model_copy(
            update={"delivery_evidence": evidence, "delivered_at": now}
        )
        self.orders.update_record(updated)
        self._audit.append(
            "market.order.delivered",
            {"tenant": tenant, "order_id": order_id, "evidence": evidence},
        )
        if updated.escrow_held:
            if evidence:
                return self._capture_to_escrow(
                    StoredOrder(updated, state="delivered"), now=now
                )
            self._audit.append(
                "market.escrow.exception",
                {
                    "tenant": tenant,
                    "order_id": order_id,
                    "reason": "delivery evidence is missing; escrow unreleased",
                },
            )
        return StoredOrder(updated, state="delivered")

    def attach_evidence(
        self, order_id: str, *, tenant: str, actor: str, now: datetime, evidence: str
    ) -> StoredOrder:
        """Late evidence for a delivered order — the exception's way out."""
        order = self._get(order_id, tenant=tenant)
        self._party(order, actor)
        if order.state != "delivered":
            raise WrongState(f"order is {order.state}, not delivered")
        if not evidence:
            raise WrongState("evidence cannot be empty")
        updated = order.record.model_copy(update={"delivery_evidence": evidence})
        self.orders.update_record(updated)
        self._audit.append(
            "market.order.evidence_attached",
            {"tenant": tenant, "order_id": order_id, "evidence": evidence},
        )
        current = StoredOrder(updated, state="delivered")
        if updated.escrow_held and updated.charge_ref is None:
            return self._capture_to_escrow(current, now=now)
        return current

    def accept(
        self, order_id: str, *, tenant: str, actor: str, now: datetime
    ) -> StoredOrder:
        """Buyer acceptance — a release condition. Direct-settlement
        orders capture and split here; escrow orders must already hold
        captured, evidenced money, which acceptance releases."""
        order = self._get(order_id, tenant=tenant)
        self._party(order, actor, buyer_only=True)
        if order.record.escrow_held and order.record.charge_ref is None:
            raise WrongState(
                "delivery evidence is missing; escrow stays held and the "
                "exception stands"
            )
        if not self.orders.set_state(
            order_id, tenant=tenant, state="accepted", expected=("delivered",)
        ):
            raise WrongState(f"order is {order.state}, not delivered")
        self._audit.append(
            "market.order.accepted", {"tenant": tenant, "order_id": order_id}
        )
        current = self._get(order_id, tenant=tenant)
        if current.record.escrow_held:
            return self._release_escrow(current, now=now, reason="buyer acceptance")
        return self._capture(current, now=now)

    def sweep_acceptance_timeouts(self, *, now: datetime) -> list[str]:
        """Automatic acceptance: a delivered, evidenced, escrow-captured
        order past the timeout releases on its own. Lazy, like every
        clock in this codebase — callers sweep on traffic."""
        if self._escrow is None:
            return []
        released: list[str] = []
        deadline = timedelta(days=self._escrow.acceptance_timeout_days)
        for order in self.orders.in_state("delivered"):
            record = order.record
            if not record.escrow_held or record.charge_ref is None:
                continue
            if record.delivered_at is None or now - record.delivered_at < deadline:
                continue
            if not self.orders.set_state(
                record.order_id,
                tenant=record.tenant_id,
                state="accepted",
                expected=("delivered",),
            ):
                continue
            self._audit.append(
                "market.order.auto_accepted",
                {
                    "tenant": record.tenant_id,
                    "order_id": record.order_id,
                    "reason": "acceptance timeout",
                },
            )
            self._release_escrow(
                self._get(record.order_id, tenant=record.tenant_id),
                now=now,
                reason="acceptance timeout",
            )
            released.append(record.order_id)
        return released

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
        self._settle_extras(updated, now=now)
        return StoredOrder(updated, state="completed")

    # ------------------------------------------------------------------ #
    # Escrow (M2): capture holds, a release condition settles.            #
    # ------------------------------------------------------------------ #
    def _capture_to_escrow(self, order: StoredOrder, *, now: datetime) -> StoredOrder:
        """The provider captures, the book HOLDS: cash against the escrow
        liability, nothing split until a release condition speaks."""
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
                entries=capture_entries(total_micros=record.offer.total_micros),
                memo=f"order {record.order_id} captured into escrow",
                created_at=now,
            )
        )
        updated = record.model_copy(update={"charge_ref": charge_ref})
        self.orders.update_record(updated)
        if posted:
            self._audit.append(
                "market.escrow.captured",
                {
                    "tenant": record.tenant_id,
                    "order_id": record.order_id,
                    "charge_ref": charge_ref,
                    "ledger_txn": txn.txn_id,
                    "amount_micros": record.offer.total_micros,
                },
            )
        return StoredOrder(updated, state=order.state)

    def _release_escrow(
        self, order: StoredOrder, *, now: datetime, reason: str
    ) -> StoredOrder:
        """A release condition holds: escrow empties into the split."""
        record = order.record
        offer = record.offer
        fee = offer.subtotal_micros * record.take_rate_bps // 10_000
        txn, posted = self._ledger.post(
            LedgerTransaction(
                txn_id=uuid4().hex,
                idempotency_key=(
                    f"ledger:release:{record.tenant_id}:{record.idempotency_key}"
                ),
                kind="release",
                order_id=record.order_id,
                currency=offer.currency,
                entries=release_entries(
                    subtotal_micros=offer.subtotal_micros,
                    fees_micros=offer.fees_micros,
                    tax_micros=offer.tax_estimate_micros,
                    take_fee_micros=fee,
                ),
                memo=f"order {record.order_id} escrow released: {reason}",
                created_at=now,
            )
        )
        self.orders.set_state(
            record.order_id,
            tenant=record.tenant_id,
            state="completed",
            expected=("accepted",),
        )
        if posted:
            self._audit.append(
                "market.escrow.released",
                {
                    "tenant": record.tenant_id,
                    "order_id": record.order_id,
                    "ledger_txn": txn.txn_id,
                    "reason": reason,
                },
            )
            self._audit.append(
                "market.order.completed",
                {"tenant": record.tenant_id, "order_id": record.order_id},
            )
        self._settle_extras(record, now=now)
        return StoredOrder(record, state="completed")

    def _settle_extras(self, record: OrderRecord, *, now: datetime) -> None:
        """What completion owes beyond the ledger: the reservation commits
        and the invoice issues — both idempotent."""
        if self._inventory is not None:
            reservation = self._inventory.for_holder(record.intent_id)
            if reservation is not None:
                self._inventory.commit(reservation.reservation_id)
        if self._invoices is not None and self._jurisdiction is not None:
            invoice, issued = self._invoices.issue(
                order_id=record.order_id,
                jurisdiction=self._jurisdiction,
                seller_id=record.seller_id,
                buyer_principal=record.buyer_principal,
                item_id=record.offer.item_id,
                quantity=record.offer.quantity,
                subtotal_micros=record.offer.subtotal_micros,
                fees_micros=record.offer.fees_micros,
                tax_micros=record.offer.tax_estimate_micros,
                currency=record.offer.currency,
                now=now,
            )
            if issued:
                self._audit.append(
                    "market.invoice.issued",
                    {
                        "tenant": record.tenant_id,
                        "order_id": record.order_id,
                        "invoice": invoice.number,
                        "total_micros": invoice.total_micros,
                    },
                )

    # ------------------------------------------------------------------ #
    # Milestones (M3): each tranche releases alone; a failure freezes     #
    # every unreleased sibling.                                           #
    # ------------------------------------------------------------------ #
    def _milestone_split(self, record: OrderRecord, index: int) -> tuple[int, int, int]:
        """(tranche_subtotal, tranche_fees, tranche_tax): fees and tax
        split proportionally, remainders landing on the final milestone so
        the tranches always sum to the offer exactly."""
        offer = record.offer
        tranche = offer.milestones[index][1]

        def share(total: int, amount: int) -> int:
            return total * amount // offer.subtotal_micros

        if index == len(offer.milestones) - 1:
            prior = offer.milestones[:index]
            fees = offer.fees_micros - sum(
                share(offer.fees_micros, amount) for _, amount in prior
            )
            tax = offer.tax_estimate_micros - sum(
                share(offer.tax_estimate_micros, amount) for _, amount in prior
            )
            return tranche, fees, tax
        return (
            tranche,
            share(offer.fees_micros, tranche),
            share(offer.tax_estimate_micros, tranche),
        )

    def _frozen(self, order_id: str) -> bool:
        return any(
            milestone.state == "failed"
            for milestone in self.milestones.for_order(order_id)
        )

    def deliver_milestone(
        self,
        order_id: str,
        *,
        tenant: str,
        actor: str,
        index: int,
        evidence: str,
        now: datetime,
    ) -> MilestoneState:
        """One milestone delivered, with evidence — never without. The
        first evidenced delivery captures the WHOLE total into escrow;
        every release after that is a tranche leaving it."""
        order = self._get(order_id, tenant=tenant)
        self._party(order, actor)
        record = order.record
        if not record.offer.milestones:
            raise WrongState("the order has no milestone schedule")
        if order.state not in ("confirmed", "fulfilling"):
            raise WrongState(f"order is {order.state}, not in fulfillment")
        if not evidence:
            raise WrongState("a milestone delivery needs evidence")
        if self._frozen(order_id):
            raise WrongState("a failed milestone froze this order")
        if not self.milestones.set_state(
            order_id, index, state="delivered", expected=("pending",),
            evidence=evidence,
        ):
            current = self.milestones.get(order_id, index)
            state = current.state if current else "absent"
            raise WrongState(f"milestone {index} is {state}, not pending")
        self.orders.set_state(
            order_id, tenant=tenant, state="fulfilling", expected=("confirmed",)
        )
        self._audit.append(
            "market.milestone.delivered",
            {
                "tenant": tenant,
                "order_id": order_id,
                "index": index,
                "evidence": evidence,
            },
        )
        fresh = self._get(order_id, tenant=tenant)
        if fresh.record.charge_ref is None:
            self._capture_to_escrow(fresh, now=now)
        settled = self.milestones.get(order_id, index)
        assert settled is not None
        return settled

    def accept_milestone(
        self, order_id: str, *, tenant: str, actor: str, index: int, now: datetime
    ) -> MilestoneState:
        """Buyer acceptance of ONE milestone: exactly its tranche leaves
        escrow — no sibling's money moves, released or pending."""
        order = self._get(order_id, tenant=tenant)
        self._party(order, actor, buyer_only=True)
        record = order.record
        if self._frozen(order_id):
            raise WrongState("a failed milestone froze the remainder")
        milestone = self.milestones.get(order_id, index)
        if milestone is None or milestone.state != "delivered":
            state = milestone.state if milestone else "absent"
            raise WrongState(f"milestone {index} is {state}, not delivered")
        if record.charge_ref is None:
            raise WrongState("nothing is captured; deliver with evidence first")
        tranche, fees, tax = self._milestone_split(record, index)
        fee = tranche * record.take_rate_bps // 10_000
        txn, posted = self._ledger.post(
            LedgerTransaction(
                txn_id=uuid4().hex,
                idempotency_key=(
                    f"ledger:release:m{index}:{tenant}:{record.idempotency_key}"
                ),
                kind="release",
                order_id=order_id,
                currency=record.offer.currency,
                entries=release_entries(
                    subtotal_micros=tranche,
                    fees_micros=fees,
                    tax_micros=tax,
                    take_fee_micros=fee,
                ),
                memo=(
                    f"order {order_id} milestone {index} "
                    f"({record.offer.milestones[index][0]}) released"
                ),
                created_at=now,
            )
        )
        self.milestones.set_state(
            order_id, index, state="released", expected=("delivered",)
        )
        if posted:
            self._audit.append(
                "market.milestone.released",
                {
                    "tenant": tenant,
                    "order_id": order_id,
                    "index": index,
                    "ledger_txn": txn.txn_id,
                    "tranche_micros": tranche,
                },
            )
        book = self.milestones.for_order(order_id)
        if all(m.state == "released" for m in book):
            self.orders.set_state(
                order_id, tenant=tenant, state="completed",
                expected=("fulfilling",),
            )
            self._audit.append(
                "market.order.completed",
                {"tenant": tenant, "order_id": order_id},
            )
            self._settle_extras(record, now=now)
        settled = self.milestones.get(order_id, index)
        assert settled is not None
        return settled

    def fail_milestone(
        self,
        order_id: str,
        *,
        tenant: str,
        actor: str,
        index: int,
        now: datetime,
        reason: str,
    ) -> MilestoneState:
        """The buyer declares a milestone failed: the order disputes and
        every unreleased tranche freezes where it sits — in escrow."""
        order = self._get(order_id, tenant=tenant)
        self._party(order, actor, buyer_only=True)
        if not self.milestones.set_state(
            order_id, index, state="failed", expected=("pending", "delivered")
        ):
            current = self.milestones.get(order_id, index)
            state = current.state if current else "absent"
            raise WrongState(f"milestone {index} is {state}; nothing to fail")
        self.orders.set_state(
            order_id,
            tenant=tenant,
            state="disputed",
            expected=("confirmed", "fulfilling"),
        )
        self._audit.append(
            "market.milestone.failed",
            {
                "tenant": tenant,
                "order_id": order_id,
                "index": index,
                "reason": reason,
            },
        )
        settled = self.milestones.get(order_id, index)
        assert settled is not None
        return settled

    def refund_unreleased(
        self,
        order_id: str,
        *,
        tenant: str,
        actor: str,
        now: datetime,
        reason: str = "",
    ) -> StoredOrder:
        """Resolution for a frozen milestone order: the captured-but-
        unreleased remainder returns to the buyer; released tranches
        stand — they were accepted."""
        order = self._get(order_id, tenant=tenant)
        self._party(order, actor)
        record = order.record
        if order.state != "disputed":
            raise WrongState(f"order is {order.state}, not disputed")
        if record.charge_ref is None:
            raise WrongState("nothing was captured; cancel instead")
        captured = sum(
            entry.amount_micros
            for txn in self._ledger.transactions(order_id=order_id)
            if txn.kind == "capture"
            for entry in txn.entries
            if entry.account == "marketplace_cash"
        )
        released = sum(
            entry.amount_micros
            for txn in self._ledger.transactions(order_id=order_id)
            if txn.kind == "release"
            for entry in txn.entries
            if entry.account == "escrow_liability"
        )
        remainder = captured - released
        if remainder <= 0:
            raise WrongState("nothing unreleased remains in escrow")
        self._guard_money()
        refund_ref = self._psp.refund(
            record.charge_ref,
            amount_micros=remainder,
            idempotency_key=f"refund:unreleased:{tenant}:{record.idempotency_key}",
        )
        self._ledger.post(
            LedgerTransaction(
                txn_id=uuid4().hex,
                idempotency_key=(
                    f"ledger:refund:unreleased:{tenant}:{record.idempotency_key}"
                ),
                kind="refund",
                order_id=order_id,
                currency=record.offer.currency,
                entries=(
                    LedgerEntry(
                        account="marketplace_cash", amount_micros=-remainder
                    ),
                    LedgerEntry(
                        account="escrow_liability", amount_micros=remainder
                    ),
                ),
                memo=f"order {order_id} unreleased escrow refunded: {reason}",
                created_at=now,
            )
        )
        updated = record.model_copy(update={"refund_ref": refund_ref})
        self.orders.update_record(updated)
        self.orders.set_state(
            order_id, tenant=tenant, state="resolved", expected=("disputed",)
        )
        self._audit.append(
            "market.order.resolved",
            {
                "tenant": tenant,
                "order_id": order_id,
                "refunded_micros": remainder,
                "refund_ref": refund_ref,
                "resolution": reason or "unreleased escrow refunded",
            },
        )
        return self._get(order_id, tenant=tenant)

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
        if self._inventory is not None:
            reservation = self._inventory.for_holder(order.record.intent_id)
            if reservation is not None:
                self._inventory.release(reservation.reservation_id)
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
        charge and the ledger posts the exact negation of EVERY settlement
        transaction the order made — the capture alone for direct
        settlement, the capture and the release for escrow."""
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
        settled = [
            txn
            for txn in self._ledger.transactions(order_id=record.order_id)
            if txn.kind in ("capture", "release")
        ]
        assert settled  # a completed order always settled something
        for txn in settled:
            self._ledger.post(
                LedgerTransaction(
                    txn_id=uuid4().hex,
                    idempotency_key=(
                        f"ledger:refund:{txn.kind}:{tenant}:{record.idempotency_key}"
                    ),
                    kind="refund",
                    order_id=record.order_id,
                    currency=record.offer.currency,
                    entries=tuple(entry.negated() for entry in txn.entries),
                    memo=f"order {record.order_id} refunded: {reason}",
                    created_at=now,
                    reverses_txn_id=txn.txn_id,
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
                settled = [
                    txn
                    for txn in self._ledger.transactions(order_id=record.order_id)
                    if txn.kind in ("capture", "release")
                ]
                if not settled:
                    return {"applied": False, "reason": "nothing settled to reverse"}
                for txn in settled:
                    self._ledger.post(
                        LedgerTransaction(
                            txn_id=uuid4().hex,
                            idempotency_key=(
                                f"ledger:refund:{txn.kind}:{tenant}:"
                                f"{record.idempotency_key}"
                            ),
                            kind="refund",
                            order_id=record.order_id,
                            currency=record.offer.currency,
                            entries=tuple(e.negated() for e in txn.entries),
                            memo=f"order {record.order_id} refunded (provider event)",
                            created_at=now,
                            reverses_txn_id=txn.txn_id,
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
