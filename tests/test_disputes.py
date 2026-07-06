from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from oolu.billing import (
    BalanceProjection,
    DisputeService,
    DisputeState,
    DisputeStore,
    EarningsEntry,
    EarningsKind,
    EarningsLedger,
    MoneyModeError,
)
from oolu.durable import DurableConnection
from oolu.durable.idempotency import IdempotencyLedger
from oolu.durable.postgres import PostgresDurableConnection
from oolu.identity import ProviderConfig

PG_DSN = os.environ.get("OOLU_TEST_PG_DSN") or os.environ.get("DATABASE_URL")

CLEARED_AT = datetime(2020, 1, 1, tzinfo=UTC)


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


def _accrual(ledger, *, event_id, noder="noder-B", micros=294000):
    ledger.append(
        EarningsEntry(
            noder_principal=noder,
            event_id=event_id,
            amount_micros=micros,
            kind=EarningsKind.ACCRUAL,
            available_at=CLEARED_AT,
        )
    )


def _service(conn):
    ledger = EarningsLedger(conn)
    service = DisputeService(
        ledger=ledger,
        disputes=DisputeStore(conn),
        durable=conn,
        providers=_ASYMMETRIC,
        idempotency=IdempotencyLedger(conn),
    )
    return ledger, service


def _clawback_count(ledger, event_id):
    return sum(
        1 for e in ledger.entries_for_event(event_id) if e.kind == EarningsKind.CLAWBACK
    )


def test_uphold_is_refused_on_local_infra():
    conn = DurableConnection(":memory:")
    try:
        _, service = _service(conn)
        dispute = service.open(event_id="evt-1", reason="chargeback")
        with pytest.raises(MoneyModeError):
            service.uphold(dispute.dispute_id)
    finally:
        conn.close()


def test_open_then_reject_records_no_clawback():
    conn = DurableConnection(":memory:")
    try:
        ledger, service = _service(conn)
        _accrual(ledger, event_id="evt-1")
        dispute = service.open(event_id="evt-1", reason="maybe")
        rejected = service.reject(dispute.dispute_id)
        assert rejected.state == DisputeState.REJECTED
        assert _clawback_count(ledger, "evt-1") == 0
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
        db.execute("DROP TABLE IF EXISTS disputes")
        db.execute("DELETE FROM idempotency")
    return conn


@pytest.mark.needs_postgres
def test_uphold_claws_back_the_accrual():
    conn = _pg()
    try:
        ledger, service = _service(conn)
        _accrual(ledger, event_id="evt-1")
        assert BalanceProjection(ledger).balance("noder-B").available_micros == 294000
        dispute = service.open(event_id="evt-1", reason="chargeback")
        result = service.uphold(dispute.dispute_id)
        assert result["clawed_back_micros"] == 294000
        assert service._disputes.get(dispute.dispute_id).state == DisputeState.UPHELD
        assert BalanceProjection(ledger).balance("noder-B").available_micros == 0
    finally:
        conn.close()


@pytest.mark.needs_postgres
def test_paid_out_clawback_goes_negative_and_is_recovered():
    conn = _pg()
    try:
        ledger, service = _service(conn)
        # An already-settled event: accrual cleared, reserve held, payout made.
        _accrual(ledger, event_id="evt-1")
        ledger.append(
            EarningsEntry(
                noder_principal="noder-B",
                amount_micros=29400,
                kind=EarningsKind.RESERVE,
                available_at=CLEARED_AT,
            )
        )
        ledger.append(
            EarningsEntry(
                noder_principal="noder-B",
                event_id="batch-1",
                amount_micros=-264600,
                kind=EarningsKind.PAYOUT,
                available_at=CLEARED_AT,
            )
        )
        assert BalanceProjection(ledger).balance("noder-B").available_micros == 0

        service.refund(event_id="evt-1", reason="chargeback")
        assert BalanceProjection(ledger).balance("noder-B").available_micros == -294000

        # Future earnings recover the negative balance.
        _accrual(ledger, event_id="evt-2")
        assert BalanceProjection(ledger).balance("noder-B").available_micros == 0
    finally:
        conn.close()


@pytest.mark.needs_postgres
def test_refund_is_idempotent_at_the_ledger():
    conn = _pg()
    try:
        ledger, service = _service(conn)
        _accrual(ledger, event_id="evt-1")
        service.refund(event_id="evt-1", reason="chargeback")
        service.refund(event_id="evt-1", reason="chargeback again")
        assert _clawback_count(ledger, "evt-1") == 1  # clawed once
        assert BalanceProjection(ledger).balance("noder-B").available_micros == 0
    finally:
        conn.close()


@pytest.mark.needs_postgres
def test_dispute_store_persists_and_lists_by_event():
    conn = _pg()
    try:
        _, service = _service(conn)
        service.open(event_id="evt-9", reason="a")
        service.open(event_id="evt-9", reason="b")
        assert len(service._disputes.for_event("evt-9")) == 2
    finally:
        conn.close()
