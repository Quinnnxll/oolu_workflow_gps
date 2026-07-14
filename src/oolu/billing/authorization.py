"""The payment-consent gate: what lets OoLu place an order or booking.

OoLu can now spend money on the user's behalf — order goods, book a table,
reserve a room — but never silently. Every such action is a RESERVED
operation whose release valve is this service, and the valve has two locks
the user alone holds:

1. **Consent to the exact amount.** The authorization request records what
   the run intends to spend (merchant, amount, currency, a plain-language
   description). Releasing it requires the user to re-state that amount to
   the cent — a draft that quietly grew from $20 to $200 cannot be waved
   through by muscle memory.
2. **A second factor.** The consent must carry a fresh TOTP code from the
   user's authenticator. A stolen session is not enough to spend money.

Until an order has an ``authorized`` record here, its action must not
execute — that is the whole security layer. Pre-launch, the LaunchGuard
keeps the real transaction port shut, so the flow is verifiable end to end
while no money can actually move; the record is the durable proof of
consent either way.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Callable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

# An order needs a real payee and a positive amount; the description is
# what the consent screen shows the user in words.
MAX_DESCRIPTION_CHARS = 500


class PaymentAuthorizationError(ValueError):
    """A refused release: the amount didn't match, the code was wrong, or
    the request was already spent."""


class OrderRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    merchant: str
    amount_micros: int  # the payee's currency, x 1e6
    currency: str
    description: str


class PaymentAuthorization(BaseModel):
    model_config = ConfigDict(frozen=True)

    auth_id: str
    scope: str  # tenant:principal — whose money, whose second factor
    run_id: str | None
    merchant: str
    amount_micros: int
    currency: str
    description: str
    status: str  # pending | authorized | cancelled
    created_at: str
    decided_at: str | None = None


_SCHEMA = """CREATE TABLE IF NOT EXISTS payment_authorizations (
    auth_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    run_id TEXT,
    merchant TEXT NOT NULL,
    amount_micros INTEGER NOT NULL,
    currency TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    decided_at TEXT
)"""


class PaymentAuthorizationStore:
    """Durable order-consent records over the host's one connection.

    ``verify_second_factor(scope, code)`` is injected — the TOTP store's
    ``verify`` bound to the account — so this service owns the consent
    logic without knowing how the second factor is checked."""

    def __init__(
        self,
        conn,
        *,
        verify_second_factor: Callable[[str, str], bool],
        second_factor_enrolled: Callable[[str], bool],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._verify = verify_second_factor
        self._enrolled = second_factor_enrolled
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def request(
        self, scope: str, order: OrderRequest, *, run_id: str | None = None
    ) -> PaymentAuthorization:
        """Record an intended order awaiting the user's consent."""
        if not order.merchant.strip():
            raise PaymentAuthorizationError("an order needs a payee")
        if order.amount_micros <= 0:
            raise PaymentAuthorizationError("an order needs a positive amount")
        if len(order.description) > MAX_DESCRIPTION_CHARS:
            raise PaymentAuthorizationError("that order description is too long")
        auth_id = uuid4().hex
        now = self._iso()
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO payment_authorizations
                     (auth_id, scope, run_id, merchant, amount_micros, currency,
                      description, status, created_at, decided_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, NULL)""",
                (
                    auth_id,
                    scope,
                    run_id,
                    order.merchant.strip(),
                    int(order.amount_micros),
                    order.currency,
                    order.description,
                    now,
                ),
            )
        record = self.get(auth_id)
        assert record is not None
        return record

    def authorize(
        self,
        scope: str,
        auth_id: str,
        *,
        confirm_amount_micros: int,
        code: str,
    ) -> PaymentAuthorization:
        """Release an order — both locks, in order of blame.

        The account must own the request; it must still be pending; the
        second factor must be enrolled AND the code valid; and the amount
        the user confirms must equal the requested amount to the cent.
        Any failure raises with the reason and leaves the record pending.
        """
        record = self.get(auth_id)
        if record is None or record.scope != scope:
            raise PaymentAuthorizationError("no such order to authorize")
        if record.status != "pending":
            raise PaymentAuthorizationError(
                f"this order was already {record.status}"
            )
        if not self._enrolled(scope):
            raise PaymentAuthorizationError(
                "set up two-factor authentication before authorizing a payment"
            )
        if not self._verify(scope, code):
            raise PaymentAuthorizationError(
                "that authenticator code is wrong or expired — try the current one"
            )
        if int(confirm_amount_micros) != record.amount_micros:
            raise PaymentAuthorizationError(
                "the amount you confirmed doesn't match the order — check it and"
                " re-confirm the exact total"
            )
        with self._conn.transaction() as db:
            db.execute(
                """UPDATE payment_authorizations
                   SET status = 'authorized', decided_at = ?
                   WHERE auth_id = ? AND status = 'pending'""",
                (self._iso(), auth_id),
            )
        released = self.get(auth_id)
        assert released is not None
        return released

    def cancel(self, scope: str, auth_id: str) -> PaymentAuthorization | None:
        """Cancel a pending order. Returns the record only when it is this
        account's — a stranger's order (or a wrong id) is None, never
        leaked, and a non-pending one is left untouched."""
        record = self.get(auth_id)
        if record is None or record.scope != scope:
            return None
        with self._conn.transaction() as db:
            db.execute(
                """UPDATE payment_authorizations SET status = 'cancelled', decided_at = ?
                   WHERE auth_id = ? AND scope = ? AND status = 'pending'""",
                (self._iso(), auth_id, scope),
            )
        return self.get(auth_id)

    def is_authorized(self, auth_id: str) -> bool:
        record = self.get(auth_id)
        return record is not None and record.status == "authorized"

    def match(
        self,
        scope: str,
        *,
        run_id: str | None,
        merchant: str,
        amount_micros: int,
    ) -> PaymentAuthorization | None:
        """The one authorization that covers this run's order, if any.

        Ties an order action back to a consent record by the run it belongs
        to and the exact merchant + amount it will spend — so the ``run_id``
        the request recorded is finally read, not inert. An ``authorized``
        record wins over a still-``pending`` one, and the newest wins among
        equals, so a resolver sees "released" the instant the user consents.
        Cancelled records never match.
        """
        with self._conn.lock:
            rows = self._conn.db.execute(
                """SELECT * FROM payment_authorizations
                   WHERE scope = ? AND run_id IS ? AND merchant = ?
                     AND amount_micros = ? AND status != 'cancelled'
                   ORDER BY (status = 'authorized') DESC, created_at DESC
                   LIMIT 1""",
                (scope, run_id, merchant.strip(), int(amount_micros)),
            ).fetchall()
        return self._row(rows[0]) if rows else None

    def pending(self, scope: str) -> list[PaymentAuthorization]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                """SELECT * FROM payment_authorizations
                   WHERE scope = ? AND status = 'pending'
                   ORDER BY created_at DESC""",
                (scope,),
            ).fetchall()
        return [self._row(r) for r in rows]

    def get(self, auth_id: str) -> PaymentAuthorization | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT * FROM payment_authorizations WHERE auth_id = ?",
                (auth_id,),
            ).fetchone()
        return self._row(row) if row is not None else None

    @staticmethod
    def _row(row) -> PaymentAuthorization:
        return PaymentAuthorization(
            auth_id=row["auth_id"],
            scope=row["scope"],
            run_id=row["run_id"],
            merchant=row["merchant"],
            amount_micros=int(row["amount_micros"]),
            currency=row["currency"],
            description=row["description"],
            status=row["status"],
            created_at=row["created_at"],
            decided_at=row["decided_at"],
        )

    def _iso(self) -> str:
        moment = self._clock()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment.isoformat()


# The order-intent keys an order action carries so a resolver can reconcile it
# against the consent store. A plan that decides to buy something knows the
# payee and the price; it stamps them here alongside the run it belongs to.
ORDER_INTENT_KEYS = (
    "authorization_scope",  # tenant:principal — whose money, whose 2FA
    "run_id",
    "merchant",
    "amount_micros",
    "currency",
    "description",
)


class PaymentAuthorizationResolver:
    """The mint-and-attach seam: a released ``auth_id`` for an order action.

    The gateway can mint (``request``) and release (``authorize``) an
    authorization, and the executor can verify one — but nothing connected the
    two: the released ``auth_id`` and the plan's order action lived in disjoint
    worlds, so an order action never actually carried an ``authorization_id``.
    This resolver is that missing wire. Given an order action that declares its
    intent (payee, exact amount, the run it belongs to, the account scope), it:

    1. finds the consent record for this run + merchant + amount, and returns
       its ``auth_id`` the moment the user has authorized it;
    2. otherwise, files the pending request the FIRST time it sees the order
       (so it appears on the user's ``/v1/payment-authorizations`` list to
       consent to) and returns ``None`` — the executor blocks, unspent, until
       the user acts. A subsequent run of the same action finds the pending
       (or, once consented, the authorized) record instead of filing again.

    It never authorizes anything — that needs the user's 2FA + exact-amount
    re-confirmation through ``authorize``. It only reconciles a plan's order
    action with the consent the user gives out of band.
    """

    def __init__(self, store: PaymentAuthorizationStore, *, auto_file: bool = True):
        self._store = store
        self._auto_file = auto_file

    def resolve(self, action) -> str | None:
        """A released ``auth_id`` for this order action, or ``None``.

        ``action`` is anything carrying a ``parameters`` mapping (an
        ``ActionEvent``). An action with no order intent resolves to ``None``
        (the executor then blocks with the generic "needs consent" reason)."""
        params = getattr(action, "parameters", None) or {}
        scope = params.get("authorization_scope")
        merchant = params.get("merchant")
        amount = params.get("amount_micros")
        if not scope or not merchant or amount is None:
            return None  # no declared intent — nothing to reconcile
        try:
            amount_micros = int(amount)
        except (TypeError, ValueError):
            return None
        run_id = params.get("run_id")
        found = self._store.match(
            scope, run_id=run_id, merchant=str(merchant), amount_micros=amount_micros
        )
        if found is not None:
            return found.auth_id if found.status == "authorized" else None
        if self._auto_file:
            try:
                self._store.request(
                    scope,
                    OrderRequest(
                        merchant=str(merchant),
                        amount_micros=amount_micros,
                        currency=str(params.get("currency") or "USD"),
                        description=str(params.get("description") or ""),
                    ),
                    run_id=run_id,
                )
            except PaymentAuthorizationError:
                return None  # a malformed intent files nothing; the gate holds
        return None
