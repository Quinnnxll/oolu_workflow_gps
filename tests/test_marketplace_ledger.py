"""M1: the double-entry ledger — balanced, append-only, idempotent, replayed.

Exit gates (docs/marketplace-build-plan.md §5 M1): every transaction
balances or is refused; a replayed key returns the existing transaction
instead of a second one; balances, GMV, and the platform's take are pure
projections of the entries; a refund negates its capture exactly and the
trial balance returns to zero.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from oolu.billing.doubleentry import (
    ACCOUNTS,
    DoubleEntryLedger,
    LedgerEntry,
    LedgerTransaction,
    UnbalancedTransaction,
)
from oolu.durable import DurableConnection

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
M = 1_000_000


def _ledger(tmp_path):
    conn = DurableConnection(tmp_path / "durable.db")
    return DoubleEntryLedger(conn), conn


def _capture_txn(key="cap-1", txn_id="t1"):
    return LedgerTransaction(
        txn_id=txn_id,
        idempotency_key=key,
        kind="capture",
        order_id="o1",
        entries=(
            LedgerEntry(account="marketplace_cash", amount_micros=108 * M),
            LedgerEntry(account="seller_payable", amount_micros=-95 * M),
            LedgerEntry(account="marketplace_fee_revenue", amount_micros=-5 * M),
            LedgerEntry(account="tax_payable", amount_micros=-8 * M),
        ),
        created_at=NOW,
    )


def test_unbalanced_unknown_zero_and_empty_posts_are_refused(tmp_path):
    ledger, conn = _ledger(tmp_path)
    with pytest.raises(UnbalancedTransaction):
        ledger.post(
            LedgerTransaction(
                txn_id="t1", idempotency_key="k1", kind="capture",
                entries=(
                    LedgerEntry(account="marketplace_cash", amount_micros=10),
                ),
                created_at=NOW,
            )
        )
    with pytest.raises(UnbalancedTransaction):
        ledger.post(
            LedgerTransaction(
                txn_id="t2", idempotency_key="k2", kind="capture",
                entries=(
                    LedgerEntry(account="not_an_account", amount_micros=10),
                    LedgerEntry(account="marketplace_cash", amount_micros=-10),
                ),
                created_at=NOW,
            )
        )
    with pytest.raises(UnbalancedTransaction):
        ledger.post(
            LedgerTransaction(
                txn_id="t3", idempotency_key="k3", kind="capture",
                entries=(
                    LedgerEntry(account="marketplace_cash", amount_micros=0),
                    LedgerEntry(account="seller_payable", amount_micros=0),
                ),
                created_at=NOW,
            )
        )
    with pytest.raises(UnbalancedTransaction):
        ledger.post(
            LedgerTransaction(
                txn_id="t4", idempotency_key="k4", kind="capture",
                entries=(), created_at=NOW,
            )
        )
    assert ledger.transactions() == []
    conn.close()


def test_a_replayed_key_returns_the_existing_transaction(tmp_path):
    ledger, conn = _ledger(tmp_path)
    first, created = ledger.post(_capture_txn())
    replay, again = ledger.post(_capture_txn(txn_id="different-id"))
    assert created and not again
    assert replay.txn_id == first.txn_id
    assert len(ledger.transactions()) == 1
    conn.close()


def test_balances_are_projections_and_a_refund_reverses_exactly(tmp_path):
    ledger, conn = _ledger(tmp_path)
    capture, _ = ledger.post(_capture_txn())
    assert ledger.balance("marketplace_cash") == 108 * M
    assert ledger.take_micros() == 5 * M
    assert ledger.gmv_micros() == 108 * M
    assert sum(ledger.trial_balance().values()) == 0

    refund = LedgerTransaction(
        txn_id="r1",
        idempotency_key="ref-1",
        kind="refund",
        order_id="o1",
        entries=tuple(entry.negated() for entry in capture.entries),
        created_at=NOW,
        reverses_txn_id=capture.txn_id,
    )
    ledger.post(refund)
    balances = ledger.trial_balance()
    assert all(balance == 0 for balance in balances.values())
    assert set(balances) == set(ACCOUNTS)
    assert ledger.take_micros() == 0
    assert ledger.gmv_micros() == 0
    conn.close()
