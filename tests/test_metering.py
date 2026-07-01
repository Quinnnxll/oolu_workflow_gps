from __future__ import annotations

import importlib
import os

import pytest

from workflow_gps.durable import DurableAuditLog, DurableConnection
from workflow_gps.durable.postgres import PostgresDurableConnection
from workflow_gps.metering import MeteringDeriver, MeteringEvent, MeteringLedger

PG_DSN = os.environ.get("WFGPS_TEST_PG_DSN") or os.environ.get("DATABASE_URL")

_PG_TABLES = ("audit_log", "metering_events")


def _new_pg() -> PostgresDurableConnection:
    if not PG_DSN:
        pytest.skip("no PostgreSQL DSN configured")
    try:
        conn = PostgresDurableConnection(PG_DSN)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL unavailable: {exc}")
    with conn.transaction() as db:
        db.execute("DROP TABLE IF EXISTS metering_events")
        db.execute("TRUNCATE audit_log RESTART IDENTITY")
    return conn


@pytest.fixture(params=["sqlite", "postgres"])
def conn(request):
    if request.param == "sqlite":
        connection = DurableConnection(":memory:")
        yield connection
        connection.close()
    else:
        connection = _new_pg()
        yield connection
        connection.close()


def _executed(audit: DurableAuditLog, run_id: str, status: str, attempt: int = 1):
    return audit.append(
        "workflow.executed",
        {
            "run_id": run_id,
            "status": status,
            "idempotency_key": f"{run_id}:exec:{attempt}",
        },
    )


def test_verified_success_produces_one_event(conn):
    audit = DurableAuditLog(conn)
    ledger = MeteringLedger(conn)
    record = _executed(audit, "r1", "succeeded")

    events = MeteringDeriver(audit, ledger).derive()

    assert len(events) == 1
    event = events[0]
    assert event.idempotency_key == "r1:exec:1"
    assert event.run_id == "r1"
    assert event.outcome == "succeeded"
    assert event.audit_seq == record.seq
    assert event.version_id is None
    assert event.consumer_tenant is None


def test_derivation_is_idempotent_on_replay(conn):
    audit = DurableAuditLog(conn)
    ledger = MeteringLedger(conn)
    _executed(audit, "r1", "succeeded")

    deriver = MeteringDeriver(audit, ledger)
    first = deriver.derive()
    second = deriver.derive()

    assert len(first) == 1
    assert second == []
    assert len(ledger.events()) == 1


def test_failure_block_cancel_are_not_metered(conn):
    audit = DurableAuditLog(conn)
    ledger = MeteringLedger(conn)
    _executed(audit, "rf", "failed")
    _executed(audit, "rb", "blocked")
    _executed(audit, "rc", "cancelled")

    events = MeteringDeriver(audit, ledger).derive()

    assert events == []
    assert ledger.events() == []


def test_only_successful_attempt_of_a_retried_run_is_metered(conn):
    audit = DurableAuditLog(conn)
    ledger = MeteringLedger(conn)
    _executed(audit, "r1", "failed", attempt=1)
    _executed(audit, "r1", "succeeded", attempt=2)

    events = MeteringDeriver(audit, ledger).derive()

    assert len(events) == 1
    assert events[0].idempotency_key == "r1:exec:2"


def test_multiple_runs_each_produce_one_event(conn):
    audit = DurableAuditLog(conn)
    ledger = MeteringLedger(conn)
    _executed(audit, "r1", "succeeded")
    _executed(audit, "r2", "succeeded")

    events = MeteringDeriver(audit, ledger).derive()

    assert {e.run_id for e in events} == {"r1", "r2"}
    assert len(ledger.events()) == 2


def test_metering_event_has_no_earnings_fields():
    # P1: gross G and provider_cost C_p are recorded facts on the event; derived
    # earnings/payout must never live here (that is billing/, display-only in P1).
    forbidden = {
        "net",
        "commission",
        "earning",
        "platform_earning",
        "noder_earning",
        "payout",
        "balance",
    }
    assert not (set(MeteringEvent.model_fields) & forbidden)


def test_no_real_payment_path_exists():
    for module in ("workflow_gps.payout", "workflow_gps.settlement"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(module)


def test_metering_exposes_no_money_symbols():
    surface = " ".join(dir(importlib.import_module("workflow_gps.metering"))).lower()
    for token in ("price", "pricing", "charge", "payout", "billing", "earning", "commission"):
        assert token not in surface
