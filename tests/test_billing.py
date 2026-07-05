from __future__ import annotations

import os

import pytest

from workflow_gps.billing import (
    BalanceProjection,
    BillingService,
    EarningsKind,
    EarningsLedger,
    PricingEngine,
)
from workflow_gps.durable import DurableAuditLog, DurableConnection
from workflow_gps.durable.postgres import PostgresDurableConnection
from workflow_gps.metering import (
    AttributionStore,
    MeteringDeriver,
    MeteringLedger,
    NoderShare,
    RunBinding,
)

PG_DSN = os.environ.get("WFGPS_TEST_PG_DSN") or os.environ.get("DATABASE_URL")


def _shares(*pairs) -> list[NoderShare]:
    return [NoderShare(noder_principal=p, weight=w) for p, w in pairs]


def test_worked_example_single_noder():
    result = PricingEngine(rho=0.30).price(
        gross=0.50, provider_cost=0.08, shares=_shares(("noder-A", 1.0))
    )
    assert result.net_micros == 420000
    assert result.platform_micros == 126000
    assert result.noder_micros == {"noder-A": 294000}
    assert result.conserves()


def test_worked_example_two_noders():
    result = PricingEngine(rho=0.30).price(
        gross=0.50,
        provider_cost=0.08,
        shares=_shares(("noder-A", 2.0), ("noder-B", 1.0)),
    )
    assert result.platform_micros == 126000
    assert result.noder_micros == {"noder-A": 196000, "noder-B": 98000}
    assert result.conserves()


def test_conservation_holds_across_many_cases():
    cases = [
        (0.50, 0.08, [("a", 1.0)]),
        (0.50, 0.08, [("a", 2.0), ("b", 1.0)]),
        (1.00, 0.33, [("a", 1.0), ("b", 1.0), ("c", 1.0)]),
        (0.07, 0.00, [("a", 3.0), ("b", 5.0)]),
        (0.50, 0.60, [("a", 1.0)]),
        (2.00, 0.01, [("a", 0.0), ("b", 1.0)]),
        (0.99, 0.01, [("a", 1.0), ("a", 1.0)]),
        (0.00, 0.00, [("a", 1.0)]),
    ]
    for rho in (0.0, 0.1, 0.30, 0.5, 0.7, 1.0):
        engine = PricingEngine(rho=rho)
        for gross, cost, pairs in cases:
            result = engine.price(
                gross=gross, provider_cost=cost, shares=_shares(*pairs)
            )
            assert result.conserves()
            assert result.platform_micros >= 0
            assert all(micros >= 0 for micros in result.noder_micros.values())


def test_negative_net_yields_zero_noder_earnings():
    result = PricingEngine(rho=0.30).price(
        gross=0.10, provider_cost=0.40, shares=_shares(("noder-A", 1.0))
    )
    assert result.net_micros == 0
    assert result.noder_micros == {}
    assert result.platform_micros == 0


def test_rho_bounds_are_validated():
    with pytest.raises(ValueError):
        PricingEngine(rho=1.5)
    with pytest.raises(ValueError):
        PricingEngine(rho=-0.1)


def test_billing_exposes_no_payment_movement_api():
    surface = " ".join(dir(BillingService)).lower()
    for token in ("payout", "charge", "settle", "capture", "refund", "stripe"):
        assert token not in surface


# --------------------------------------------------------------------------- #
# Ledger / projection over both durable backends.                             #
# --------------------------------------------------------------------------- #
def _new_pg() -> PostgresDurableConnection:
    if not PG_DSN:
        pytest.skip("no PostgreSQL DSN configured")
    try:
        conn = PostgresDurableConnection(PG_DSN)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL unavailable: {exc}")
    with conn.transaction() as db:
        db.execute("DROP TABLE IF EXISTS earnings_entries")
        db.execute("DROP TABLE IF EXISTS metering_events")
        db.execute("DROP TABLE IF EXISTS run_bindings")
        db.execute("DROP TABLE IF EXISTS attribution_records")
        db.execute("TRUNCATE audit_log RESTART IDENTITY")
    return conn


@pytest.fixture(params=["sqlite", "postgres"])
def conn(request):
    if request.param == "sqlite":
        connection = DurableConnection(":memory:")
    else:
        connection = _new_pg()
    try:
        yield connection
    finally:
        connection.close()


def _event_and_attributions(conn, *, gross=0.50, cost=0.08, shares=(("noder-B", 1.0),)):
    audit = DurableAuditLog(conn)
    ledger = MeteringLedger(conn)
    attribution = AttributionStore(conn)
    attribution.bind(
        RunBinding(
            run_id="r1",
            version_id="v1",
            consumer_tenant="tenant-A",
            gross=gross,
            provider_cost=cost,
            shares=[NoderShare(noder_principal=p, weight=w) for p, w in shares],
        )
    )
    audit.append(
        "workflow.executed",
        {"run_id": "r1", "status": "succeeded", "idempotency_key": "r1:exec:1"},
    )
    events = MeteringDeriver(audit, ledger, attribution).derive()
    return events[0], attribution.attributions(events[0].event_id)


def test_accrual_creates_earnings_entry(conn):
    event, attributions = _event_and_attributions(conn)
    billing = BillingService(EarningsLedger(conn), rho=0.30)
    entries = billing.accrue(event, attributions)
    assert len(entries) == 1
    assert entries[0].noder_principal == "noder-B"
    assert entries[0].amount_micros == 294000
    assert entries[0].kind == EarningsKind.ACCRUAL


def test_balance_is_a_pure_projection(conn):
    event, attributions = _event_and_attributions(conn)
    billing = BillingService(EarningsLedger(conn), rho=0.30)
    billing.accrue(event, attributions)
    assert billing.balance("noder-B").available_micros == 294000


def test_accrual_is_idempotent(conn):
    event, attributions = _event_and_attributions(conn)
    ledger = EarningsLedger(conn)
    billing = BillingService(ledger, rho=0.30)
    first = billing.accrue(event, attributions)
    second = billing.accrue(event, attributions)
    assert len(first) == 1 and second == []
    assert len(ledger.entries_for_event(event.event_id)) == 1
    assert billing.balance("noder-B").available_micros == 294000


def test_ledger_append_only_and_balance_replays(conn):
    event, attributions = _event_and_attributions(conn)
    ledger = EarningsLedger(conn)
    BillingService(ledger, rho=0.30).accrue(event, attributions)
    replayed = BalanceProjection(ledger).balance("noder-B")
    assert replayed.available_micros == sum(
        e.amount_micros for e in ledger.entries("noder-B")
    )


def test_cross_noder_isolation(conn):
    event, attributions = _event_and_attributions(
        conn, shares=(("noder-A", 2.0), ("noder-B", 1.0))
    )
    billing = BillingService(EarningsLedger(conn), rho=0.30)
    billing.accrue(event, attributions)
    assert billing.balance("noder-A").available_micros == 196000
    assert billing.balance("noder-B").available_micros == 98000
    assert billing.balance("stranger").available_micros == 0


def test_unbound_event_accrues_nothing(conn):
    audit = DurableAuditLog(conn)
    audit.append(
        "workflow.executed",
        {"run_id": "local", "status": "succeeded", "idempotency_key": "local:exec:1"},
    )
    events = MeteringDeriver(
        audit, MeteringLedger(conn), AttributionStore(conn)
    ).derive()
    billing = BillingService(EarningsLedger(conn), rho=0.30)
    assert billing.accrue(events[0], []) == []
