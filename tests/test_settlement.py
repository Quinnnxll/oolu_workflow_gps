from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from oolu.billing import (
    BalanceProjection,
    EarningsEntry,
    EarningsKind,
    EarningsLedger,
    FakePayoutAdapter,
    KycStatus,
    MoneyModeError,
    PayoutAccount,
    PayoutStatus,
    PayoutStore,
    SettlementService,
)
from oolu.durable import DurableConnection
from oolu.durable.idempotency import IdempotencyLedger
from oolu.durable.postgres import PostgresDurableConnection
from oolu.identity import ProviderConfig

PG_DSN = os.environ.get("OOLU_TEST_PG_DSN") or os.environ.get("DATABASE_URL")

CLEARED_AT = datetime(2020, 1, 1, tzinfo=UTC)  # long past -> already cleared holdback


class _AsymmetricVerifier:
    algorithm = "RS256"

    def verify(self, signing_input, signature, header) -> bool:
        return True


_ASYMMETRIC = [
    ProviderConfig(
        issuer="https://idp",
        audiences=frozenset({"oolu"}),
        verifier=_AsymmetricVerifier(),
    )
]


def _seed_accrual(ledger, *, noder="noder-B", micros=294000):
    ledger.append(
        EarningsEntry(
            noder_principal=noder,
            event_id="evt-1",
            amount_micros=micros,
            kind=EarningsKind.ACCRUAL,
            available_at=CLEARED_AT,
        )
    )


def _verified_account(store, *, noder="noder-B"):
    store.save_account(
        PayoutAccount(
            noder_principal=noder,
            provider_account_id="acct_1",
            kyc_status=KycStatus.VERIFIED,
        )
    )


def test_settlement_is_refused_on_local_infra():
    conn = DurableConnection(":memory:")
    try:
        service = SettlementService(
            ledger=EarningsLedger(conn),
            payout_store=PayoutStore(conn),
            payout=FakePayoutAdapter(),
            durable=conn,
            providers=_ASYMMETRIC,
            idempotency=IdempotencyLedger(conn),
        )
        with pytest.raises(MoneyModeError):
            service.settle("noder-B", period_key="2030-01")
    finally:
        conn.close()


def _pg():
    if not PG_DSN:
        pytest.skip("no PostgreSQL DSN configured")
    try:
        conn = PostgresDurableConnection(PG_DSN)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL unavailable: {exc}")
    with conn.transaction() as db:
        db.execute("DROP TABLE IF EXISTS earnings_entries")
        db.execute("DROP TABLE IF EXISTS payout_accounts")
        db.execute("DROP TABLE IF EXISTS payout_batches")
        db.execute("DELETE FROM idempotency")
    return conn


def _service(conn, payout, **overrides):
    kwargs = dict(
        ledger=EarningsLedger(conn),
        payout_store=PayoutStore(conn),
        payout=payout,
        durable=conn,
        providers=_ASYMMETRIC,
        idempotency=IdempotencyLedger(conn),
    )
    kwargs.update(overrides)
    return kwargs["ledger"], kwargs["payout_store"], SettlementService(**kwargs)


@pytest.mark.needs_postgres
def test_below_threshold_rolls_forward_and_holds_reserve():
    conn = _pg()
    try:
        payout = FakePayoutAdapter()
        ledger, store, service = _service(conn, payout, min_payout_micros=1_000_000)
        _seed_accrual(ledger)
        _verified_account(store)
        result = service.settle("noder-B", period_key="2030-01")
        assert result["paid"] is False
        assert result["reason"] == "below_threshold"
        # reserve held = 10% of 294000 = 29400; available = 294000 - 29400
        assert result["available_micros"] == 264600
        assert payout._payouts == {}
        assert store.batches_by_status(PayoutStatus.PAID) == []
        assert BalanceProjection(ledger).balance("noder-B").reserved_micros == 29400
    finally:
        conn.close()


@pytest.mark.needs_postgres
def test_above_threshold_pays_out_to_verified_account():
    conn = _pg()
    try:
        payout = FakePayoutAdapter()
        ledger, store, service = _service(conn, payout, min_payout_micros=100_000)
        _seed_accrual(ledger)
        _verified_account(store)
        result = service.settle("noder-B", period_key="2030-01")
        assert result["paid"] is True
        assert result["amount_micros"] == 264600
        assert len(payout._payouts) == 1
        batch = store.get_batch(result["batch_id"])
        assert batch.status == PayoutStatus.PAID
        balance = BalanceProjection(ledger).balance("noder-B")
        assert balance.available_micros == 0
        assert balance.lifetime_paid_micros == 264600
        assert balance.reserved_micros == 29400
    finally:
        conn.close()


@pytest.mark.needs_postgres
def test_unverified_account_is_not_paid():
    conn = _pg()
    try:
        payout = FakePayoutAdapter()
        ledger, store, service = _service(conn, payout, min_payout_micros=100_000)
        _seed_accrual(ledger)
        store.save_account(
            PayoutAccount(
                noder_principal="noder-B",
                provider_account_id="acct_1",
                kyc_status=KycStatus.PENDING,
            )
        )
        result = service.settle("noder-B", period_key="2030-01")
        assert result["paid"] is False
        assert result["reason"] == "kyc_pending"
        assert payout._payouts == {}
    finally:
        conn.close()


@pytest.mark.needs_postgres
def test_settlement_is_idempotent_per_period():
    conn = _pg()
    try:
        payout = FakePayoutAdapter()
        ledger, store, service = _service(conn, payout, min_payout_micros=100_000)
        _seed_accrual(ledger)
        _verified_account(store)
        first = service.settle("noder-B", period_key="2030-01")
        second = service.settle("noder-B", period_key="2030-01")
        assert first == second
        assert len(payout._payouts) == 1  # paid once
        balance = BalanceProjection(ledger).balance("noder-B")
        assert balance.available_micros == 0
        assert balance.lifetime_paid_micros == 264600  # not double-paid
    finally:
        conn.close()
