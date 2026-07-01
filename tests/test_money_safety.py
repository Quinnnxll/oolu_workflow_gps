from __future__ import annotations

import os
import threading
from datetime import UTC, datetime, timedelta

import pytest

from workflow_gps.billing import (
    BalanceProjection,
    ChargingService,
    EarningsEntry,
    EarningsKind,
    EarningsLedger,
    FakePayoutAdapter,
    KycStatus,
    PayoutAccount,
    PayoutStore,
    SettlementService,
)
from workflow_gps.durable.idempotency import IdempotencyLedger
from workflow_gps.durable.postgres import PostgresDurableConnection
from workflow_gps.identity import ProviderConfig
from workflow_gps.metering import AttributionRecord, MeteringEvent

PG_DSN = os.environ.get("WFGPS_TEST_PG_DSN") or os.environ.get("DATABASE_URL")

CLEARED_AT = datetime(2020, 1, 1, tzinfo=UTC)
pytestmark = pytest.mark.needs_postgres


class _AsymmetricVerifier:
    algorithm = "RS256"

    def verify(self, signing_input, signature, header) -> bool:
        return True


_ASYMMETRIC = [
    ProviderConfig(
        issuer="https://idp", audiences=frozenset({"wfgps"}), verifier=_AsymmetricVerifier()
    )
]


def _pg() -> PostgresDurableConnection:
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
    # Pre-create the billing tables on one connection so worker connections do not
    # race on CREATE TABLE IF NOT EXISTS.
    EarningsLedger(conn)
    PayoutStore(conn)
    return conn


def _run_threads(target, count):
    threads = [threading.Thread(target=target) for _ in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def _event():
    return MeteringEvent(
        idempotency_key="r1:exec:1",
        run_id="r1",
        version_id="v1",
        consumer_principal="consumer",
        outcome="succeeded",
        gross=0.50,
        provider_cost=0.08,
        audit_seq=1,
        occurred_at=CLEARED_AT,
    )


def test_concurrent_charges_are_exactly_once():
    main = _pg()
    payout = FakePayoutAdapter()
    event = _event()
    attributions = [AttributionRecord(event_id=event.event_id, noder_principal="noder-B", weight=1.0)]

    def worker():
        conn = PostgresDurableConnection(PG_DSN)
        try:
            ChargingService(
                ledger=EarningsLedger(conn),
                payout=payout,
                durable=conn,
                providers=_ASYMMETRIC,
                idempotency=IdempotencyLedger(conn),
            ).charge_and_accrue(event, attributions, consumer_ref="cus_1")
        finally:
            conn.close()

    try:
        _run_threads(worker, 8)
        assert len(payout._charges) == 1  # charged exactly once
        entries = EarningsLedger(main).entries_for_event(event.event_id)
        assert len(entries) == 1  # accrued exactly once
        assert entries[0].amount_micros == 294000
    finally:
        main.close()


def test_concurrent_settlement_pays_once():
    main = _pg()
    payout = FakePayoutAdapter()
    EarningsLedger(main).append(
        EarningsEntry(
            noder_principal="noder-B",
            event_id="evt-1",
            amount_micros=294000,
            kind=EarningsKind.ACCRUAL,
            available_at=CLEARED_AT,
        )
    )
    PayoutStore(main).save_account(
        PayoutAccount(noder_principal="noder-B", provider_account_id="acct_1", kyc_status=KycStatus.VERIFIED)
    )

    def worker():
        conn = PostgresDurableConnection(PG_DSN)
        try:
            SettlementService(
                ledger=EarningsLedger(conn),
                payout_store=PayoutStore(conn),
                payout=payout,
                durable=conn,
                providers=_ASYMMETRIC,
                idempotency=IdempotencyLedger(conn),
                min_payout_micros=100_000,
            ).settle("noder-B", period_key="2030-01")
        finally:
            conn.close()

    try:
        _run_threads(worker, 6)
        assert len(payout._payouts) == 1  # paid exactly once
        balance = BalanceProjection(EarningsLedger(main)).balance("noder-B")
        assert balance.lifetime_paid_micros == 264600  # not double-paid
        assert balance.available_micros == 0
    finally:
        main.close()


def test_restart_mid_settlement_does_not_double_pay():
    conn = _pg()
    payout = FakePayoutAdapter()
    EarningsLedger(conn).append(
        EarningsEntry(
            noder_principal="noder-B",
            event_id="evt-1",
            amount_micros=294000,
            kind=EarningsKind.ACCRUAL,
            available_at=CLEARED_AT,
        )
    )
    PayoutStore(conn).save_account(
        PayoutAccount(noder_principal="noder-B", provider_account_id="acct_1", kyc_status=KycStatus.VERIFIED)
    )
    try:
        SettlementService(
            ledger=EarningsLedger(conn),
            payout_store=PayoutStore(conn),
            payout=payout,
            durable=conn,
            providers=_ASYMMETRIC,
            idempotency=IdempotencyLedger(conn),
            min_payout_micros=100_000,
        ).settle("noder-B", period_key="2030-01")
    finally:
        conn.close()

    # A restarted process: a brand-new connection re-runs the same period.
    restarted = PostgresDurableConnection(PG_DSN)
    try:
        SettlementService(
            ledger=EarningsLedger(restarted),
            payout_store=PayoutStore(restarted),
            payout=payout,
            durable=restarted,
            providers=_ASYMMETRIC,
            idempotency=IdempotencyLedger(restarted),
            min_payout_micros=100_000,
        ).settle("noder-B", period_key="2030-01")
        assert len(payout._payouts) == 1  # still paid once across the restart
        assert BalanceProjection(EarningsLedger(restarted)).balance("noder-B").lifetime_paid_micros == 264600
    finally:
        restarted.close()


def test_cross_noder_isolation_under_concurrent_accrual():
    main = _pg()
    payout = FakePayoutAdapter()

    def worker(noder, key):
        conn = PostgresDurableConnection(PG_DSN)
        event = MeteringEvent(
            idempotency_key=key,
            run_id=key,
            version_id="v1",
            consumer_principal="consumer",
            outcome="succeeded",
            gross=0.50,
            provider_cost=0.08,
            audit_seq=1,
            occurred_at=CLEARED_AT,
        )
        attributions = [AttributionRecord(event_id=event.event_id, noder_principal=noder, weight=1.0)]
        try:
            ChargingService(
                ledger=EarningsLedger(conn),
                payout=payout,
                durable=conn,
                providers=_ASYMMETRIC,
                idempotency=IdempotencyLedger(conn),
            ).charge_and_accrue(event, attributions, consumer_ref="cus")
        finally:
            conn.close()

    threads = [
        threading.Thread(target=worker, args=("noder-A", "a:exec:1")),
        threading.Thread(target=worker, args=("noder-B", "b:exec:1")),
    ]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        ledger = EarningsLedger(main)
        after = CLEARED_AT + timedelta(days=15)
        assert BalanceProjection(ledger).balance("noder-A", now=after).available_micros == 294000
        assert BalanceProjection(ledger).balance("noder-B", now=after).available_micros == 294000
        assert BalanceProjection(ledger).balance("stranger", now=after).available_micros == 0
    finally:
        main.close()
