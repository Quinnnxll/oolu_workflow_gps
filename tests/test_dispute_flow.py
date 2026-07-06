"""Dispute deepening: reserve-funded clawbacks, final decisions, debt.

The reserve the settlement holds back exists for exactly one moment — a
dispute upheld AFTER the noder was paid. The invariants under test: a
clawback reverses only what the event minted; a shortfall is funded from
the noder's reserve first; only what the reserve cannot cover remains as
debt that future accruals repay before anything else pays out; decisions
are final (and idempotent the same way twice); and every resolution is
audited.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from oolu.billing import (
    BalanceProjection,
    DisputeService,
    DisputeStore,
    EarningsEntry,
    EarningsKind,
    EarningsLedger,
    KycStatus,
    PayoutAccount,
    PayoutStatus,
    PayoutStore,
    SettlementService,
)
from oolu.billing.models import PayoutReceipt
from oolu.durable.audit import DurableAuditLog
from oolu.durable.connection import DurableConnection
from oolu.durable.idempotency import IdempotencyLedger

NOW = datetime(2026, 7, 1, tzinfo=UTC)
CLEARED = NOW - timedelta(days=30)


class _PaysEverything:
    def payout(self, *, idempotency_key, provider_account_id, amount_micros, currency):
        return PayoutReceipt(
            provider_ref=f"po_{idempotency_key}",
            amount_micros=amount_micros,
            currency=currency,
            status=PayoutStatus.PAID,
        )


def _build(tmp_path):
    conn = DurableConnection(tmp_path / "disputes.db")
    ledger = EarningsLedger(conn)
    accounts = PayoutStore(conn)
    durable = SimpleNamespace(is_production_durable=True, audit=DurableAuditLog(conn))
    idem = IdempotencyLedger(conn)
    settlement = SettlementService(
        ledger=ledger,
        payout_store=accounts,
        payout=_PaysEverything(),
        durable=durable,
        providers=[],
        idempotency=idem,
        clock=lambda: NOW,
    )
    disputes = DisputeService(
        ledger=ledger,
        disputes=DisputeStore(conn),
        durable=durable,
        providers=[],
        idempotency=idem,
        clock=lambda: NOW,
    )
    return conn, ledger, accounts, durable, settlement, disputes


def _earn(ledger, noder, event_id, micros):
    ledger.append(
        EarningsEntry(
            noder_principal=noder,
            event_id=event_id,
            amount_micros=micros,
            kind=EarningsKind.ACCRUAL,
            available_at=CLEARED,
        )
    )


def _verified(accounts, noder):
    accounts.save_account(
        PayoutAccount(
            noder_principal=noder,
            provider_account_id=f"acct_{noder}",
            kyc_status=KycStatus.VERIFIED,
        )
    )


def test_reserve_funds_a_post_payout_clawback(tmp_path):
    """The holdback doing its job: a small event disputed after payout is
    absorbed entirely by the reserve — no debt, conservation intact."""
    conn, ledger, accounts, durable, settlement, disputes = _build(tmp_path)
    _earn(ledger, "alice", "evt-big", 92_000_000)
    _earn(ledger, "alice", "evt-small", 8_000_000)
    _verified(accounts, "alice")
    paid = settlement.settle("alice", period_key="2026-07")
    assert paid["paid"] and paid["amount_micros"] == 90_000_000  # 10% reserved

    result = disputes.refund(event_id="evt-small")
    assert result["clawed_back_micros"] == 8_000_000
    assert result["reserve_drawn_micros"] == 8_000_000  # inside the 10M held
    assert result["outstanding_debt_micros"] == 0

    balance = BalanceProjection(ledger).balance("alice", now=NOW)
    assert balance.available_micros == 0  # made whole, nothing owed
    assert balance.reserved_micros == 2_000_000  # what remains of the 10M
    (event,) = [r for r in durable.audit.records() if r.event_type == "dispute.upheld"]
    assert event.payload["reserve_drawn_micros"] == 8_000_000
    conn.close()


def test_clawback_beyond_the_reserve_becomes_debt_that_accruals_repay(tmp_path):
    """A dispute bigger than the reserve leaves honest debt: the balance
    goes negative, and new earnings fill the hole before paying out."""
    conn, ledger, accounts, durable, settlement, disputes = _build(tmp_path)
    _earn(ledger, "alice", "evt-1", 100_000_000)
    _verified(accounts, "alice")
    settlement.settle("alice", period_key="2026-07")  # paid 90M, reserved 10M

    result = disputes.refund(event_id="evt-1")
    assert result["clawed_back_micros"] == 100_000_000
    assert result["reserve_drawn_micros"] == 10_000_000  # the whole reserve
    # 10M was still held; the other 90M is already in alice's pocket.
    assert result["outstanding_debt_micros"] == 90_000_000

    balance = BalanceProjection(ledger).balance("alice", now=NOW)
    assert balance.available_micros == -90_000_000
    assert balance.reserved_micros == 0

    # New verified work repays the debt before anything pays out again.
    _earn(ledger, "alice", "evt-2", 50_000_000)
    august = settlement.settle("alice", period_key="2026-08")
    assert august["paid"] is False and august["reason"] == "below_threshold"
    still_owing = BalanceProjection(ledger).balance("alice", now=NOW)
    # 50 earned fills the hole (5 of it re-reserved on the new net).
    assert still_owing.available_micros == -45_000_000
    conn.close()


def test_clawback_before_payout_needs_no_reserve(tmp_path):
    conn, ledger, accounts, durable, settlement, disputes = _build(tmp_path)
    _earn(ledger, "alice", "evt-1", 50_000_000)
    _verified(accounts, "alice")

    result = disputes.refund(event_id="evt-1")  # disputed before any payout
    assert result["clawed_back_micros"] == 50_000_000
    assert result["reserve_drawn_micros"] == 0
    assert result["outstanding_debt_micros"] == 0
    assert BalanceProjection(ledger).balance("alice", now=NOW).available_micros == 0

    # And a later settlement does NOT re-collect reserve for reversed
    # earnings: the target nets clawbacks, so nothing tops up from zero.
    outcome = settlement.settle("alice", period_key="2026-07")
    assert outcome["reason"] == "below_threshold"
    assert BalanceProjection(ledger).balance("alice", now=NOW).reserved_micros == 0
    conn.close()


def test_multi_noder_event_claws_back_and_draws_per_noder(tmp_path):
    conn, ledger, accounts, durable, settlement, disputes = _build(tmp_path)
    _earn(ledger, "alice", "evt-shared", 60_000_000)
    _earn(ledger, "bob", "evt-shared", 40_000_000)
    _verified(accounts, "alice")
    _verified(accounts, "bob")
    settlement.settle_all(period_key="2026-07")  # both paid, both reserved

    result = disputes.refund(event_id="evt-shared")
    assert result["clawed_back_micros"] == 100_000_000
    alice = result["by_noder"]["alice"]
    bob = result["by_noder"]["bob"]
    assert alice["reserve_drawn_micros"] == 6_000_000  # each their own reserve
    assert bob["reserve_drawn_micros"] == 4_000_000
    assert alice["outstanding_debt_micros"] == 54_000_000
    assert bob["outstanding_debt_micros"] == 36_000_000
    conn.close()


def test_decisions_are_final_and_idempotent(tmp_path):
    conn, ledger, accounts, durable, settlement, disputes = _build(tmp_path)
    _earn(ledger, "alice", "evt-1", 50_000_000)
    _verified(accounts, "alice")

    rejected = disputes.open(event_id="evt-1", reason="looks fine")
    assert disputes.reject(rejected.dispute_id).state.value == "rejected"
    disputes.reject(rejected.dispute_id)  # same decision twice: no-op
    with pytest.raises(ValueError, match="already rejected"):
        disputes.uphold(rejected.dispute_id)
    # The rejected dispute never touched the ledger.
    assert BalanceProjection(ledger).balance("alice", now=NOW).available_micros > 0

    upheld = disputes.open(event_id="evt-1", reason="chargeback")
    first = disputes.uphold(upheld.dispute_id)
    assert first["clawed_back_micros"] == 50_000_000
    replay = disputes.uphold(upheld.dispute_id)  # idempotent replay
    assert replay == first
    with pytest.raises(ValueError, match="already upheld"):
        disputes.reject(upheld.dispute_id)
    # Both resolutions are on the audit log.
    kinds = [r.event_type for r in durable.audit.records()]
    assert kinds.count("dispute.rejected") == 1
    assert kinds.count("dispute.upheld") == 1
    conn.close()
