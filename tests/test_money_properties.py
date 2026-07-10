"""Property-style fuzzing of the money invariants.

A seeded operation machine drives the whole settlement economy — random
accruals, clock advances, settlement cycles (with a flaky processor),
upheld and rejected disputes — and checks, after every step and for every
noder:

1. the reserve is never negative;
2. lifetime payouts never exceed gross accruals (money is never minted);
3. the balance only goes negative for noders hit by an upheld clawback
   (debt exists, but only disputes create it);
4. the ledger's PAYOUT outflow equals what the processor actually paid.

At the end of every run the clock jumps past the risk window and a final
clean cycle settles: undisputed noders' remaining balances must be fully
accounted (gross == paid + available + reserved) with the residue below
the payout threshold — the "eventually 100%" property, under fire.

No hypothesis dependency: seeds are explicit, failures replay exactly.
"""

from __future__ import annotations

import random
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
from oolu.billing.payout import PaymentError
from oolu.durable.audit import DurableAuditLog
from oolu.durable.connection import DurableConnection
from oolu.durable.idempotency import IdempotencyLedger

START = datetime(2026, 1, 1, tzinfo=UTC)
NODERS = ("alice", "bob", "carol")
MIN_PAYOUT = 20_000_000


class _FlakyPayout:
    """Pays, but fails a slice of calls — deterministic per seed."""

    def __init__(self, rng: random.Random, failure_rate: float = 0.15):
        self._rng = rng
        self._rate = failure_rate
        self.paid_micros: dict[str, int] = {}

    def payout(
        self,
        *,
        idempotency_key,
        provider_account_id,
        amount_micros,
        currency,
        metadata=None,
    ):
        if self._rng.random() < self._rate:
            raise PaymentError("flaky processor")
        noder = provider_account_id.removeprefix("acct_")
        self.paid_micros[noder] = self.paid_micros.get(noder, 0) + amount_micros
        return PayoutReceipt(
            provider_ref=f"po_{idempotency_key}",
            amount_micros=amount_micros,
            currency=currency,
            status=PayoutStatus.PAID,
        )


class _Machine:
    def __init__(self, tmp_path, seed: int):
        self.rng = random.Random(seed)
        self.conn = DurableConnection(tmp_path / f"money-{seed}.db")
        self.ledger = EarningsLedger(self.conn)
        self.accounts = PayoutStore(self.conn)
        self.adapter = _FlakyPayout(self.rng)
        self.now = START
        durable = SimpleNamespace(
            is_production_durable=True, audit=DurableAuditLog(self.conn)
        )
        idem = IdempotencyLedger(self.conn)
        self.settlement = SettlementService(
            ledger=self.ledger,
            payout_store=self.accounts,
            payout=self.adapter,
            durable=durable,
            providers=[],
            idempotency=idem,
            clock=lambda: self.now,
        )
        self.disputes = DisputeService(
            ledger=self.ledger,
            disputes=DisputeStore(self.conn),
            durable=durable,
            providers=[],
            idempotency=idem,
            clock=lambda: self.now,
        )
        for noder in NODERS:
            self.accounts.save_account(
                PayoutAccount(
                    noder_principal=noder,
                    provider_account_id=f"acct_{noder}",
                    kyc_status=KycStatus.VERIFIED,
                )
            )
        self.events: list[str] = []
        self.gross: dict[str, int] = dict.fromkeys(NODERS, 0)
        self.clawed_noders: set[str] = set()
        self.periods = 0
        self.last_period = "p0"

    # ------------------------------- operations ------------------------ #
    def accrue(self):
        noder = self.rng.choice(NODERS)
        amount = self.rng.randrange(1_000_000, 150_000_000)
        event_id = f"evt-{len(self.events)}"
        self.ledger.append(
            EarningsEntry(
                noder_principal=noder,
                event_id=event_id,
                amount_micros=amount,
                kind=EarningsKind.ACCRUAL,
                available_at=self.now,
            )
        )
        self.events.append(event_id)
        self.gross[noder] += amount

    def advance(self):
        self.now += timedelta(days=self.rng.randrange(1, 60))

    def settle(self):
        if self.rng.random() < 0.2:
            period = self.last_period  # replaying a period must be safe
        else:
            self.periods += 1
            period = f"p{self.periods}"
            self.last_period = period
        self.settlement.settle_all(period_key=period)

    def dispute(self):
        if not self.events:
            return
        event_id = self.rng.choice(self.events)
        if self.rng.random() < 0.5:
            outcome = self.disputes.refund(event_id=event_id)
            if outcome["clawed_back_micros"] > 0:
                for entry in self.ledger.entries_for_event(event_id):
                    if entry.kind is EarningsKind.CLAWBACK:
                        self.clawed_noders.add(entry.noder_principal)
        else:
            opened = self.disputes.open(event_id=event_id, reason="fuzz")
            self.disputes.reject(opened.dispute_id)

    # ------------------------------- invariants ------------------------ #
    def check(self):
        projection = BalanceProjection(self.ledger)
        for noder in NODERS:
            balance = projection.balance(noder, now=self.now)
            assert balance.reserved_micros >= 0, (
                f"{noder}: negative reserve {balance.reserved_micros}"
            )
            assert balance.lifetime_paid_micros <= self.gross[noder], (
                f"{noder}: paid {balance.lifetime_paid_micros} exceeds "
                f"gross accruals {self.gross[noder]} — money was minted"
            )
            if noder not in self.clawed_noders:
                assert balance.available_micros >= 0, (
                    f"{noder}: negative balance without any upheld clawback"
                )
            assert balance.lifetime_paid_micros == self.adapter.paid_micros.get(
                noder, 0
            ), f"{noder}: ledger PAYOUT total disagrees with the processor"


OPS = ("accrue", "accrue", "advance", "settle", "settle", "dispute")


@pytest.mark.parametrize("seed", range(12))
def test_money_invariants_hold_under_random_operation_sequences(tmp_path, seed):
    machine = _Machine(tmp_path, seed)
    for _ in range(50):
        getattr(machine, machine.rng.choice(OPS))()
        machine.check()

    # Endgame: the processor heals, the risk window passes, one clean cycle.
    machine.adapter._rate = 0.0
    machine.now += timedelta(days=120)
    machine.settlement.settle_all(period_key="final")
    machine.check()

    projection = BalanceProjection(machine.ledger)
    for noder in NODERS:
        if noder in machine.clawed_noders:
            continue  # debtors are covered by the running invariants
        balance = projection.balance(noder, now=machine.now)
        # Everything is accounted for, and everything payable was paid:
        # what remains is only the sub-threshold residue.
        assert (
            balance.lifetime_paid_micros
            + balance.available_micros
            + balance.reserved_micros
            == machine.gross[noder]
        )
        assert balance.available_micros < MIN_PAYOUT
    machine.conn.close()
