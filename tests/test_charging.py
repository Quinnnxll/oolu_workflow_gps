from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from workflow_gps.billing import (
    BalanceProjection,
    ChargingService,
    EarningsLedger,
    FakePayoutAdapter,
    MoneyModeError,
)
from workflow_gps.durable import DurableConnection
from workflow_gps.durable.idempotency import IdempotencyLedger
from workflow_gps.durable.postgres import PostgresDurableConnection
from workflow_gps.identity import ProviderConfig
from workflow_gps.metering import AttributionRecord, MeteringEvent

PG_DSN = os.environ.get("WFGPS_TEST_PG_DSN") or os.environ.get("DATABASE_URL")

EVENT_TIME = datetime(2030, 1, 1, tzinfo=UTC)


class _AsymmetricVerifier:
    algorithm = "RS256"

    def verify(self, signing_input, signature, header) -> bool:
        return True


_ASYMMETRIC = [
    ProviderConfig(
        issuer="https://idp", audiences=frozenset({"wfgps"}), verifier=_AsymmetricVerifier()
    )
]


def _event():
    return MeteringEvent(
        idempotency_key="r1:exec:1",
        run_id="r1",
        version_id="v1",
        consumer_tenant="tenant-A",
        consumer_principal="consumer-1",
        outcome="succeeded",
        gross=0.50,
        provider_cost=0.08,
        audit_seq=1,
        occurred_at=EVENT_TIME,
    )


def _attrs(event):
    return [AttributionRecord(event_id=event.event_id, noder_principal="noder-B", weight=1.0)]


def test_charging_is_refused_on_local_infra():
    conn = DurableConnection(":memory:")
    try:
        payout = FakePayoutAdapter()
        service = ChargingService(
            ledger=EarningsLedger(conn),
            payout=payout,
            durable=conn,
            providers=_ASYMMETRIC,
            idempotency=IdempotencyLedger(conn),
        )
        event = _event()
        with pytest.raises(MoneyModeError):
            service.charge_and_accrue(event, _attrs(event), consumer_ref="cus_1")
        assert payout._charges == {}  # nothing charged
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
        db.execute("DELETE FROM idempotency")
    return conn


@pytest.mark.needs_postgres
def test_charge_accrues_with_holdback_on_production():
    conn = _pg()
    try:
        ledger = EarningsLedger(conn)
        payout = FakePayoutAdapter()
        service = ChargingService(
            ledger=ledger,
            payout=payout,
            durable=conn,
            providers=_ASYMMETRIC,
            idempotency=IdempotencyLedger(conn),
        )
        event = _event()
        out = service.charge_and_accrue(event, _attrs(event), consumer_ref="cus_1")
        assert out["charged"] is True
        assert out["charged_micros"] == 500000  # G = $0.50
        assert out["noder_accruals"] == {"noder-B": 294000}  # N=0.42, rho=0.30

        projection = BalanceProjection(ledger)
        before = projection.balance("noder-B", now=EVENT_TIME)
        assert before.available_micros == 0
        assert before.pending_micros == 294000

        after = projection.balance("noder-B", now=EVENT_TIME + timedelta(days=14, seconds=1))
        assert after.available_micros == 294000
        assert after.pending_micros == 0
    finally:
        conn.close()


@pytest.mark.needs_postgres
def test_charge_is_exactly_once_on_retry():
    conn = _pg()
    try:
        ledger = EarningsLedger(conn)
        payout = FakePayoutAdapter()
        service = ChargingService(
            ledger=ledger,
            payout=payout,
            durable=conn,
            providers=_ASYMMETRIC,
            idempotency=IdempotencyLedger(conn),
        )
        event = _event()
        first = service.charge_and_accrue(event, _attrs(event), consumer_ref="cus_1")
        second = service.charge_and_accrue(event, _attrs(event), consumer_ref="cus_1")
        assert first == second
        assert len(payout._charges) == 1  # charged once
        assert len(ledger.entries_for_event(event.event_id)) == 1  # accrued once
        after = BalanceProjection(ledger).balance(
            "noder-B", now=EVENT_TIME + timedelta(days=15)
        )
        assert after.available_micros == 294000
    finally:
        conn.close()


@pytest.mark.needs_postgres
def test_unbilled_event_does_not_charge():
    conn = _pg()
    try:
        payout = FakePayoutAdapter()
        service = ChargingService(
            ledger=EarningsLedger(conn),
            payout=payout,
            durable=conn,
            providers=_ASYMMETRIC,
            idempotency=IdempotencyLedger(conn),
        )
        event = _event().model_copy(update={"gross": None})
        out = service.charge_and_accrue(event, _attrs(event), consumer_ref="cus_1")
        assert out["charged"] is False
        assert payout._charges == {}
    finally:
        conn.close()
