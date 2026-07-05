"""Settlement cycles: pay everyone eligible, contain every failure.

`settle_all` runs one period over every noder on the ledger. The invariants
under test: one processor failure never blocks anyone else's payout; a
failed payout debits nothing and leaves the period honestly retryable
(the idempotency claim is released, the FAILED batch stays for the record);
a paid period replays its receipt forever — re-running a cycle is exactly
how failures are retried, never how money is doubled.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from workflow_gps.billing import (
    EarningsEntry,
    EarningsKind,
    EarningsLedger,
    KycStatus,
    PayoutAccount,
    PayoutStatus,
    PayoutStore,
    SettlementService,
)
from workflow_gps.billing.models import PayoutReceipt
from workflow_gps.billing.payout import PaymentError
from workflow_gps.durable.audit import DurableAuditLog
from workflow_gps.durable.connection import DurableConnection
from workflow_gps.durable.idempotency import IdempotencyLedger

NOW = datetime(2026, 7, 1, tzinfo=UTC)
CLEARED = NOW - timedelta(days=30)  # past any holdback: fully available


class _ScriptedPayout:
    """Fails for chosen accounts until told otherwise; records every call."""

    def __init__(self, failing=()):
        self.failing = set(failing)
        self.calls: list[str] = []

    def payout(self, *, idempotency_key, provider_account_id, amount_micros, currency):
        self.calls.append(idempotency_key)
        if provider_account_id in self.failing:
            raise PaymentError("processor unavailable")
        return PayoutReceipt(
            provider_ref=f"po_{len(self.calls)}",
            amount_micros=amount_micros,
            currency=currency,
            status=PayoutStatus.PAID,
        )


def _build(tmp_path, *, failing=(), clock=None, risk_window_days=90):
    conn = DurableConnection(tmp_path / "settle.db")
    ledger = EarningsLedger(conn)
    accounts = PayoutStore(conn)
    adapter = _ScriptedPayout(failing=failing)
    service = SettlementService(
        ledger=ledger,
        payout_store=accounts,
        payout=adapter,
        durable=SimpleNamespace(
            is_production_durable=True, audit=DurableAuditLog(conn)
        ),
        providers=[],  # no HMAC verifiers: passes the production gate
        idempotency=IdempotencyLedger(conn),
        risk_window_days=risk_window_days,
        clock=clock or (lambda: NOW),
    )
    return conn, ledger, accounts, adapter, service


def _earn(ledger, noder, micros):
    ledger.append(
        EarningsEntry(
            noder_principal=noder,
            event_id=f"evt-{noder}",
            amount_micros=micros,
            kind=EarningsKind.ACCRUAL,
            available_at=CLEARED,
        )
    )


def _account(accounts, noder, *, kyc=KycStatus.VERIFIED):
    accounts.save_account(
        PayoutAccount(
            noder_principal=noder,
            provider_account_id=f"acct_{noder}",
            kyc_status=kyc,
        )
    )


def test_cycle_pays_skips_and_contains_failures(tmp_path):
    conn, ledger, accounts, adapter, service = _build(tmp_path, failing={"acct_carol"})
    _earn(ledger, "alice", 100_000_000)  # verified, well above threshold
    _account(accounts, "alice")
    _earn(ledger, "bob", 5_000_000)  # below the $20 minimum
    _account(accounts, "bob")
    _earn(ledger, "carol", 50_000_000)  # verified, but her processor is down
    _account(accounts, "carol")
    _earn(ledger, "dave", 40_000_000)  # money waiting, KYC not done
    _account(accounts, "dave", kyc=KycStatus.PENDING)

    cycle = service.settle_all(period_key="2026-07")
    assert cycle["noders"] == 4
    assert cycle["paid"] == 1 and cycle["failed"] == 1 and cycle["skipped"] == 2

    alice = cycle["outcomes"]["alice"]
    assert alice["paid"] and alice["amount_micros"] == 90_000_000  # 10% reserved
    assert cycle["outcomes"]["bob"]["reason"] == "below_threshold"
    assert cycle["outcomes"]["dave"]["reason"] == "kyc_pending"

    carol = cycle["outcomes"]["carol"]
    assert carol["reason"] == "payment_failed"
    assert "processor unavailable" in carol["error"]
    # Carol's failure is on the record but the money never moved.
    (batch,) = accounts.batches("carol")
    assert batch.status is PayoutStatus.FAILED
    assert not any(e.kind == EarningsKind.PAYOUT for e in ledger.entries("carol"))
    # Alice's payout is fully settled: PAID batch + ledger debit.
    (paid_batch,) = accounts.batches("alice")
    assert paid_batch.status is PayoutStatus.PAID
    assert sum(e.amount_micros for e in ledger.entries("alice")) == 20_000_000

    # The cycle itself is audited (without the per-noder detail).
    (event,) = [
        r
        for r in service._durable.audit.records()
        if r.event_type == "settlement.cycle"
    ]
    assert event.payload["paid"] == 1 and event.payload["failed"] == 1
    assert event.payload["paid_micros"] == 90_000_000
    conn.close()


def test_failed_period_retries_and_paid_periods_replay(tmp_path):
    """Re-running the SAME period is the retry mechanism: the failure was
    released (not poisoned), the success is cached (never re-paid)."""
    conn, ledger, accounts, adapter, service = _build(tmp_path, failing={"acct_carol"})
    _earn(ledger, "alice", 100_000_000)
    _account(accounts, "alice")
    _earn(ledger, "carol", 50_000_000)
    _account(accounts, "carol")

    first = service.settle_all(period_key="2026-07")
    assert first["outcomes"]["carol"]["reason"] == "payment_failed"
    calls_after_first = len(adapter.calls)

    adapter.failing.clear()  # the processor recovers
    second = service.settle_all(period_key="2026-07")

    # Carol got a fresh attempt and was paid; alice replayed her cached
    # receipt — her processor was called exactly once across both cycles.
    carol = second["outcomes"]["carol"]
    assert carol["paid"] and carol["amount_micros"] == 45_000_000
    assert len(adapter.calls) == calls_after_first + 1
    assert second["outcomes"]["alice"]["paid"]
    assert sum(1 for k in adapter.calls if "alice" in k) == 1

    # Carol's history shows both attempts: the FAILED batch and the PAID one.
    statuses = [b.status for b in accounts.batches("carol")]
    assert statuses == [PayoutStatus.FAILED, PayoutStatus.PAID]

    # A third cycle moves no money at all: both replay, balances stand.
    from workflow_gps.billing import BalanceProjection

    third = service.settle_all(period_key="2026-07")
    assert third["paid"] == 2 and len(adapter.calls) == calls_after_first + 1
    balance = BalanceProjection(ledger).balance("carol", now=NOW)
    assert balance.available_micros == 0  # everything payable went out
    assert balance.reserved_micros == 5_000_000  # the 10% reserve held back
    assert balance.lifetime_paid_micros == 45_000_000
    conn.close()


def test_reserve_releases_after_the_risk_window(tmp_path):
    """The holdback is a loan, not a fee: once the chargeback window
    passes with no dispute, the reserve pays out — the noder eventually
    receives 100% of undisputed earnings."""
    from workflow_gps.billing import BalanceProjection

    current = {"now": NOW}
    conn, ledger, accounts, adapter, service = _build(
        tmp_path, clock=lambda: current["now"]
    )
    _earn(ledger, "alice", 300_000_000)  # cleared 30 days ago
    _account(accounts, "alice")

    july = service.settle("alice", period_key="2026-07")
    assert july["paid"] and july["amount_micros"] == 270_000_000  # 30M held

    current["now"] = NOW + timedelta(days=120)  # the accrual is 150d old now
    october = service.settle("alice", period_key="2026-10")
    assert october["paid"] and october["amount_micros"] == 30_000_000

    balance = BalanceProjection(ledger).balance("alice", now=current["now"])
    assert balance.reserved_micros == 0
    assert balance.available_micros == 0
    assert balance.lifetime_paid_micros == 300_000_000  # every micro, eventually
    conn.close()


def test_only_at_risk_earnings_demand_reserve(tmp_path):
    """The target is scoped to the window: aged-out accruals carry no
    chargeback risk, so only fresh work is held against."""
    from workflow_gps.billing import BalanceProjection

    conn, ledger, accounts, adapter, service = _build(tmp_path)
    ledger.append(
        EarningsEntry(
            noder_principal="alice",
            event_id="evt-ancient",
            amount_micros=200_000_000,
            kind=EarningsKind.ACCRUAL,
            available_at=NOW - timedelta(days=200),  # far outside the window
        )
    )
    _earn(ledger, "alice", 100_000_000)  # fresh: inside the window
    _account(accounts, "alice")

    outcome = service.settle("alice", period_key="2026-07")
    # 10% of the fresh 100M is held; the ancient 200M pays in full.
    assert outcome["amount_micros"] == 290_000_000
    balance = BalanceProjection(ledger).balance("alice", now=NOW)
    assert balance.reserved_micros == 10_000_000
    conn.close()


def test_disabled_window_holds_the_reserve_forever(tmp_path):
    """risk_window_days=None restores accumulate-forever semantics."""
    from workflow_gps.billing import BalanceProjection

    current = {"now": NOW}
    conn, ledger, accounts, adapter, service = _build(
        tmp_path, clock=lambda: current["now"], risk_window_days=None
    )
    _earn(ledger, "alice", 300_000_000)
    _account(accounts, "alice")
    service.settle("alice", period_key="2026-07")

    current["now"] = NOW + timedelta(days=365)
    later = service.settle("alice", period_key="2027-07")
    assert later["paid"] is False and later["reason"] == "below_threshold"
    balance = BalanceProjection(ledger).balance("alice", now=current["now"])
    assert balance.reserved_micros == 30_000_000  # still held, a year on
    conn.close()


def test_release_unpoisons_a_crashed_claim(tmp_path):
    """The idempotency primitive the retry rests on: a claim whose fn
    raised replays None forever — release makes it runnable again."""
    import pytest

    conn = DurableConnection(tmp_path / "idem.db")
    idem = IdempotencyLedger(conn)

    def boom():
        raise RuntimeError("crashed partway")

    with pytest.raises(RuntimeError):
        idem.run("job:1", boom)
    assert idem.run("job:1", lambda: "recovered") is None  # poisoned
    assert idem.release("job:1") is True
    assert idem.run("job:1", lambda: "recovered") == "recovered"
    assert idem.release("missing") is False
    conn.close()
