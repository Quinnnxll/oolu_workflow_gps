from __future__ import annotations

import os

import pytest

from workflow_gps.durable import DurableAuditLog, DurableConnection
from workflow_gps.durable.postgres import PostgresDurableConnection
from workflow_gps.metering import (
    AttributionStore,
    MeteringDeriver,
    MeteringLedger,
    NoderShare,
    RunBinding,
)
from workflow_gps.nodeplace import PricingModel, PricingPolicy, gross_from_policy

PG_DSN = os.environ.get("WFGPS_TEST_PG_DSN") or os.environ.get("DATABASE_URL")

_PG_TABLES = ("audit_log", "metering_events", "run_bindings", "attribution_records")


def _new_pg() -> PostgresDurableConnection:
    if not PG_DSN:
        pytest.skip("no PostgreSQL DSN configured")
    try:
        conn = PostgresDurableConnection(PG_DSN)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL unavailable: {exc}")
    with conn.transaction() as db:
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


def _executed(audit, run_id, status="succeeded", attempt=1):
    return audit.append(
        "workflow.executed",
        {"run_id": run_id, "status": status, "idempotency_key": f"{run_id}:exec:{attempt}"},
    )


def _deriver(conn):
    return MeteringDeriver(DurableAuditLog(conn), MeteringLedger(conn), AttributionStore(conn))


def test_bound_run_produces_attributed_event(conn):
    audit = DurableAuditLog(conn)
    attribution = AttributionStore(conn)
    attribution.bind(
        RunBinding(
            run_id="r1",
            version_id="v1",
            consumer_tenant="tenant-A",
            gross=0.5,
            provider_cost=0.08,
            shares=[NoderShare(noder_principal="noder-B", weight=1.0)],
        )
    )
    _executed(audit, "r1")

    events = MeteringDeriver(audit, MeteringLedger(conn), attribution).derive()

    assert len(events) == 1
    event = events[0]
    assert event.version_id == "v1"
    assert event.consumer_tenant == "tenant-A"
    assert event.gross == 0.5
    assert event.provider_cost == 0.08
    records = attribution.attributions(event.event_id)
    assert [r.noder_principal for r in records] == ["noder-B"]


def test_multi_noder_attribution(conn):
    audit = DurableAuditLog(conn)
    attribution = AttributionStore(conn)
    attribution.bind(
        RunBinding(
            run_id="r1",
            version_id="v1",
            consumer_tenant="tenant-A",
            gross=0.5,
            provider_cost=0.08,
            shares=[
                NoderShare(noder_principal="noder-A", weight=2.0),
                NoderShare(noder_principal="noder-B", weight=1.0),
            ],
        )
    )
    _executed(audit, "r1")

    events = MeteringDeriver(audit, MeteringLedger(conn), attribution).derive()
    records = attribution.attributions(events[0].event_id)
    assert {r.noder_principal: r.weight for r in records} == {"noder-A": 2.0, "noder-B": 1.0}


def test_unbound_run_records_bare_event_without_attribution(conn):
    audit = DurableAuditLog(conn)
    attribution = AttributionStore(conn)
    _executed(audit, "local-run")

    events = MeteringDeriver(audit, MeteringLedger(conn), attribution).derive()

    assert len(events) == 1
    assert events[0].version_id is None
    assert events[0].gross is None
    assert attribution.attributions(events[0].event_id) == []


def test_attribution_is_idempotent_on_replay(conn):
    audit = DurableAuditLog(conn)
    attribution = AttributionStore(conn)
    attribution.bind(
        RunBinding(
            run_id="r1",
            version_id="v1",
            consumer_tenant="tenant-A",
            shares=[NoderShare(noder_principal="noder-B", weight=1.0)],
        )
    )
    _executed(audit, "r1")

    deriver = MeteringDeriver(audit, MeteringLedger(conn), attribution)
    first = deriver.derive()
    second = deriver.derive()

    assert len(first) == 1 and second == []
    assert len(attribution.attributions(first[0].event_id)) == 1


def test_failure_produces_no_event_and_no_attribution(conn):
    audit = DurableAuditLog(conn)
    attribution = AttributionStore(conn)
    attribution.bind(
        RunBinding(
            run_id="rf",
            version_id="v1",
            consumer_tenant="tenant-A",
            shares=[NoderShare(noder_principal="noder-B", weight=1.0)],
        )
    )
    _executed(audit, "rf", status="failed")

    events = MeteringDeriver(audit, MeteringLedger(conn), attribution).derive()
    assert events == []


def test_gross_from_policy():
    assert gross_from_policy(
        PricingPolicy(version_id="v", model=PricingModel.PER_SUCCESS, unit_price=0.5)
    ) == 0.5
    assert gross_from_policy(PricingPolicy(version_id="v", model=PricingModel.FREE)) == 0.0
    assert gross_from_policy(
        PricingPolicy(version_id="v", model=PricingModel.SUBSCRIPTION, unit_price=9.99)
    ) == 0.0
