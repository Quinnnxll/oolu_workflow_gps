"""Reconciliation (M3): every finished order proves itself, or files why not.

The reconciliation agent's deterministic core: match the order against its
ledger transactions, its invoice, its payment references, and its delivery
evidence. A completed order must show exactly one capture, settlement that
sums to its total, an issued invoice with the same numbers, and evidence
someone could verify. A refunded order must show a book that nets to zero.

Matched orders CLOSE (audited, recorded, never re-examined). Mismatched
orders file EXCEPTIONS with every issue named and the evidence trail
attached — ledger transaction ids, references, the invoice number — and a
duplicate charge goes further: the order itself is moved to DISPUTED with
the assembled evidence riding the audit chain, because a double capture is
not a bookkeeping note, it is somebody's money.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from ..billing.doubleentry import DoubleEntryLedger
from ..billing.tax import InvoiceBook
from .orders import OrderStore, StoredOrder

_SCHEMA = """CREATE TABLE IF NOT EXISTS market_reconciliations (
    order_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL
)"""


class ReconciliationReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    order_id: str
    order_state: str
    matched: bool
    issues: tuple[str, ...] = ()
    # The trail a reviewer (or a dispute) starts from.
    evidence: tuple[str, ...] = ()
    examined_at: datetime


class ReconciliationDesk:
    def __init__(
        self,
        conn,
        *,
        audit,
        orders: OrderStore,
        ledger: DoubleEntryLedger,
        invoices: InvoiceBook | None = None,
    ) -> None:
        self._conn = conn
        self._audit = audit
        self._orders = orders
        self._ledger = ledger
        self._invoices = invoices
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    # ------------------------------------------------------------------ #
    # The match itself: arithmetic, never judgment.                       #
    # ------------------------------------------------------------------ #
    def reconcile(self, order: StoredOrder, *, now: datetime) -> ReconciliationReport:
        record = order.record
        issues: list[str] = []
        evidence: list[str] = []
        txns = self._ledger.transactions(order_id=record.order_id)
        evidence.extend(f"ledger:{txn.txn_id}" for txn in txns)
        captures = [txn for txn in txns if txn.kind == "capture"]
        if len(captures) > 1:
            issues.append(f"duplicate charge: {len(captures)} captures posted")
        if record.charge_ref is None:
            issues.append("no payment reference on a settled order")
        else:
            evidence.append(f"charge:{record.charge_ref}")
        cash_net = sum(
            entry.amount_micros
            for txn in txns
            for entry in txn.entries
            if entry.account == "marketplace_cash"
        )
        if order.state == "completed":
            if cash_net != record.offer.total_micros:
                issues.append(
                    f"cash {cash_net} does not match the order total "
                    f"{record.offer.total_micros}"
                )
            escrow_net = sum(
                entry.amount_micros
                for txn in txns
                for entry in txn.entries
                if entry.account == "escrow_liability"
            )
            if record.escrow_held and escrow_net != 0:
                issues.append(
                    f"escrow not fully released: {escrow_net} micros remain"
                )
            if record.escrow_held and not (
                record.delivery_evidence or record.offer.milestones
            ):
                issues.append("no delivery evidence on an escrow order")
            elif record.delivery_evidence:
                evidence.append(f"delivery:{record.delivery_evidence}")
            if self._invoices is not None:
                invoice = self._invoices.for_order(record.order_id)
                if invoice is None:
                    issues.append("no invoice issued for a completed order")
                elif invoice.total_micros != record.offer.total_micros:
                    issues.append(
                        f"invoice total {invoice.total_micros} does not "
                        f"match the order total {record.offer.total_micros}"
                    )
                else:
                    evidence.append(f"invoice:{invoice.number}")
        elif order.state == "refunded":
            if cash_net != 0:
                issues.append(
                    f"a refunded order's cash must net to zero, not {cash_net}"
                )
            if record.refund_ref is None:
                issues.append("no refund reference on a refunded order")
            else:
                evidence.append(f"refund:{record.refund_ref}")
        return ReconciliationReport(
            order_id=record.order_id,
            order_state=order.state,
            matched=not issues,
            issues=tuple(issues),
            evidence=tuple(evidence),
            examined_at=now,
        )

    # ------------------------------------------------------------------ #
    # The sweep: close the matched, file the rest.                        #
    # ------------------------------------------------------------------ #
    def sweep(self, *, now: datetime) -> dict:
        closed: list[str] = []
        exceptions: list[str] = []
        disputes: list[str] = []
        for state in ("completed", "refunded"):
            for order in self._orders.in_state(state):
                if self._already(order.record.order_id):
                    continue
                report = self.reconcile(order, now=now)
                self._record(report)
                if report.matched:
                    closed.append(report.order_id)
                    self._audit.append(
                        "market.reconciliation.closed",
                        {
                            "tenant": order.record.tenant_id,
                            "order_id": report.order_id,
                            "evidence": list(report.evidence),
                        },
                    )
                    continue
                exceptions.append(report.order_id)
                self._audit.append(
                    "market.reconciliation.exception",
                    {
                        "tenant": order.record.tenant_id,
                        "order_id": report.order_id,
                        "issues": list(report.issues),
                        "evidence": list(report.evidence),
                    },
                )
                if any("duplicate charge" in issue for issue in report.issues):
                    # A double capture is somebody's money: the order
                    # disputes itself, evidence attached.
                    if self._orders.set_state(
                        report.order_id,
                        tenant=order.record.tenant_id,
                        state="disputed",
                        expected=("completed",),
                    ):
                        disputes.append(report.order_id)
                        self._audit.append(
                            "market.order.disputed",
                            {
                                "tenant": order.record.tenant_id,
                                "order_id": report.order_id,
                                "reason": "reconciliation: duplicate charge",
                                "evidence": list(report.evidence),
                            },
                        )
        return {"closed": closed, "exceptions": exceptions, "disputes": disputes}

    def exceptions(self) -> list[ReconciliationReport]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT payload_json FROM market_reconciliations"
                " WHERE state = 'exception'"
            ).fetchall()
        reports = [
            ReconciliationReport.model_validate_json(row["payload_json"])
            for row in rows
        ]
        reports.sort(key=lambda r: (r.examined_at.isoformat(), r.order_id))
        return reports

    def _already(self, order_id: str) -> bool:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT 1 FROM market_reconciliations WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        return row is not None

    def _record(self, report: ReconciliationReport) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO market_reconciliations
                   (order_id, state, payload_json) VALUES (?, ?, ?)
                   ON CONFLICT(order_id) DO UPDATE SET
                     state = excluded.state,
                     payload_json = excluded.payload_json""",
                (
                    report.order_id,
                    "matched" if report.matched else "exception",
                    report.model_dump_json(),
                ),
            )
