"""Tax and invoices (M2): the compliance layer's first two obligations.

The deployment rule is the law here: country-specific legal and tax modules
must be configured BEFORE transactions are enabled in a jurisdiction. The
registry enforces it — asking for an unconfigured jurisdiction raises, and
the catalog refuses to mint an offer at all, so nothing downstream ever
sees an untaxed price by accident.

Invoices are issued exactly once per order (idempotent on the order id),
numbered sequentially per jurisdiction prefix, and stored append-only —
an invoice is evidence, not a view.
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class UnconfiguredJurisdiction(RuntimeError):
    """No legal/tax module for this jurisdiction: transacting is refused."""


class JurisdictionModule(BaseModel):
    """One jurisdiction's typed tax terms. MVP: one flat rate, tax added
    on top of the subtotal."""

    model_config = ConfigDict(frozen=True)

    code: str  # e.g. "US-CA", "DE"
    currency: str = "USD"
    tax_rate_bps: int = Field(default=0, ge=0)
    invoice_prefix: str = "INV"


class TaxRegistry:
    def __init__(self, modules: tuple[JurisdictionModule, ...] = ()) -> None:
        self._modules = {module.code: module for module in modules}

    def get(self, code: str) -> JurisdictionModule:
        module = self._modules.get(code)
        if module is None:
            raise UnconfiguredJurisdiction(
                f"jurisdiction '{code}' has no configured legal/tax module; "
                "transactions there are refused until it is"
            )
        return module

    def tax_micros(self, code: str, subtotal_micros: int) -> int:
        module = self.get(code)
        return subtotal_micros * module.tax_rate_bps // 10_000


class InvoiceRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    invoice_id: str
    number: str  # "{prefix}-{seq:06d}" within the jurisdiction
    order_id: str
    jurisdiction: str
    seller_id: str
    buyer_principal: str
    item_id: str
    quantity: int
    subtotal_micros: int
    fees_micros: int
    tax_micros: int
    total_micros: int
    currency: str
    issued_at: datetime


_SCHEMA = """CREATE TABLE IF NOT EXISTS marketplace_invoices (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id TEXT NOT NULL UNIQUE,
    order_id TEXT NOT NULL UNIQUE,
    jurisdiction TEXT NOT NULL,
    payload_json TEXT NOT NULL
)"""


class InvoiceBook:
    """Append-only invoices, exactly one per order."""

    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def issue(
        self,
        *,
        order_id: str,
        jurisdiction: JurisdictionModule,
        seller_id: str,
        buyer_principal: str,
        item_id: str,
        quantity: int,
        subtotal_micros: int,
        fees_micros: int,
        tax_micros: int,
        currency: str,
        now: datetime,
    ) -> tuple[InvoiceRecord, bool]:
        """Issue, or return the invoice the order already has."""
        existing = self.for_order(order_id)
        if existing is not None:
            return existing, False
        invoice_id = uuid4().hex
        with self._conn.transaction() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO marketplace_invoices"
                " (invoice_id, order_id, jurisdiction, payload_json)"
                " VALUES (?, ?, ?, ?)",
                (invoice_id, order_id, jurisdiction.code, "{}"),
            )
            if cursor.rowcount == 0:
                settled = self.for_order(order_id)
                assert settled is not None
                return settled, False
            seq = int(cursor.lastrowid or 0)
            record = InvoiceRecord(
                invoice_id=invoice_id,
                number=f"{jurisdiction.invoice_prefix}-{seq:06d}",
                order_id=order_id,
                jurisdiction=jurisdiction.code,
                seller_id=seller_id,
                buyer_principal=buyer_principal,
                item_id=item_id,
                quantity=quantity,
                subtotal_micros=subtotal_micros,
                fees_micros=fees_micros,
                tax_micros=tax_micros,
                total_micros=subtotal_micros + fees_micros + tax_micros,
                currency=currency,
                issued_at=now,
            )
            db.execute(
                "UPDATE marketplace_invoices SET payload_json = ?"
                " WHERE invoice_id = ?",
                (record.model_dump_json(), invoice_id),
            )
        return record, True

    def for_order(self, order_id: str) -> InvoiceRecord | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json FROM marketplace_invoices"
                " WHERE order_id = ? AND payload_json != '{}'",
                (order_id,),
            ).fetchone()
        if row is None:
            return None
        return InvoiceRecord.model_validate_json(row["payload_json"])
