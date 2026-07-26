"""The double-entry ledger — the marketplace's financial source of truth.

Every transaction is a set of signed entries over the eight marketplace
accounts that MUST sum to zero (positive = debit, negative = credit); an
unbalanced post is refused, not corrected. Transactions are append-only and
idempotent on their key — a replayed post (a retried capture, a duplicate
provider webhook) returns the transaction that already exists instead of
minting a second one. Nothing is ever edited: a refund is a compensating
transaction that negates its capture exactly.

Balances, GMV, and the platform's take are PROJECTIONS — computed by
replaying entries, never stored, never adjusted. The book is revenue; no
report computes it independently (docs/marketplace-build-plan.md §4).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

# The spec's account set, verbatim.
ACCOUNTS = (
    "buyer_payable",
    "marketplace_cash",
    "seller_payable",
    "escrow_liability",
    "marketplace_fee_revenue",
    "tax_payable",
    "refund_payable",
    "dispute_reserve",
    # The ad house (agents-expansion A4): campaign funding arrives as
    # cash against a standing liability — unspent budget is owed, not
    # earned. Recognition (liability → revenue + contributor payable)
    # is A5's posting; nothing in the adhouse package writes here.
    "advertiser_payable",
    "ad_budget_liability",
)

_SCHEMA = """CREATE TABLE IF NOT EXISTS marketplace_ledger (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_id TEXT NOT NULL UNIQUE,
    idempotency_key TEXT NOT NULL UNIQUE,
    order_id TEXT,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
)"""


class UnbalancedTransaction(ValueError):
    """The entries do not sum to zero; the post is refused outright."""


class LedgerEntry(BaseModel):
    """One signed line: positive debits the account, negative credits it."""

    model_config = ConfigDict(frozen=True)

    account: str
    amount_micros: int

    def negated(self) -> "LedgerEntry":
        return LedgerEntry(account=self.account, amount_micros=-self.amount_micros)


class LedgerTransaction(BaseModel):
    model_config = ConfigDict(frozen=True)

    txn_id: str
    idempotency_key: str
    kind: str  # "capture" | "refund" | ...
    order_id: str | None = None
    currency: str = "USD"
    entries: tuple[LedgerEntry, ...]
    memo: str = ""
    created_at: datetime
    # A compensating transaction names what it reverses.
    reverses_txn_id: str | None = None

    @property
    def total(self) -> int:
        return sum(entry.amount_micros for entry in self.entries)


class DoubleEntryLedger:
    """Append-only balanced transactions on the shared durable connection."""

    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def post(self, txn: LedgerTransaction) -> tuple[LedgerTransaction, bool]:
        """Post if the key is new; return the existing transaction otherwise.

        Refusals are structural: an empty entry set, a zero line, an
        unknown account, or a non-zero sum never reaches the table.
        """
        if not txn.entries:
            raise UnbalancedTransaction("a transaction needs entries")
        for entry in txn.entries:
            if entry.account not in ACCOUNTS:
                raise UnbalancedTransaction(f"unknown account '{entry.account}'")
            if entry.amount_micros == 0:
                raise UnbalancedTransaction("a zero entry says nothing")
        if txn.total != 0:
            raise UnbalancedTransaction(
                f"entries sum to {txn.total} micros, not zero"
            )
        with self._conn.transaction() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO marketplace_ledger"
                " (txn_id, idempotency_key, order_id, kind, payload_json,"
                "  created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    txn.txn_id,
                    txn.idempotency_key,
                    txn.order_id,
                    txn.kind,
                    txn.model_dump_json(),
                    txn.created_at.isoformat(),
                ),
            )
            created = cursor.rowcount > 0
        if created:
            return txn, True
        existing = self.by_key(txn.idempotency_key)
        assert existing is not None
        return existing, False

    def by_key(self, idempotency_key: str) -> LedgerTransaction | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json FROM marketplace_ledger"
                " WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        return LedgerTransaction.model_validate_json(row["payload_json"])

    def transactions(self, *, order_id: str | None = None) -> list[LedgerTransaction]:
        """In posting order — the explicit ``seq``, never the clock, which
        can tie, and never SQLite's implicit row order, which PostgreSQL
        does not have."""
        with self._conn.lock:
            if order_id is None:
                rows = self._conn.db.execute(
                    "SELECT payload_json FROM marketplace_ledger ORDER BY seq"
                ).fetchall()
            else:
                rows = self._conn.db.execute(
                    "SELECT payload_json FROM marketplace_ledger"
                    " WHERE order_id = ? ORDER BY seq",
                    (order_id,),
                ).fetchall()
        return [
            LedgerTransaction.model_validate_json(row["payload_json"])
            for row in rows
        ]

    # ------------------------------------------------------------------ #
    # Projections. Computed by replay, never stored.                      #
    # ------------------------------------------------------------------ #
    def balance(self, account: str) -> int:
        if account not in ACCOUNTS:
            raise UnbalancedTransaction(f"unknown account '{account}'")
        return sum(
            entry.amount_micros
            for txn in self.transactions()
            for entry in txn.entries
            if entry.account == account
        )

    def trial_balance(self) -> dict[str, int]:
        """Every account's balance. Sums to zero or the ledger is broken."""
        balances = {account: 0 for account in ACCOUNTS}
        for txn in self.transactions():
            for entry in txn.entries:
                balances[entry.account] += entry.amount_micros
        return balances

    def gmv_micros(self) -> int:
        """Gross merchandise value: cash debited by captures, net of the
        cash credited back by refunds — read from the book alone."""
        return sum(
            entry.amount_micros
            for txn in self.transactions()
            if txn.kind in ("capture", "refund")
            for entry in txn.entries
            if entry.account == "marketplace_cash"
        )

    def take_micros(self) -> int:
        """The platform's share: the fee-revenue credit balance, positive."""
        return -self.balance("marketplace_fee_revenue")
