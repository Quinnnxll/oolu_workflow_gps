from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from oolu.durable import (
    DurableAuditLog,
    DurableConnection,
    DurableRecordStore,
    DurableTaskQueue,
    IdempotencyLedger,
    TaskStatus,
)
from oolu.durable.postgres import PostgresDurableConnection, _translate

T0 = datetime(2099, 1, 1, tzinfo=UTC)

PG_DSN = os.environ.get("OOLU_TEST_PG_DSN") or os.environ.get("DATABASE_URL")

_PG_TABLES = (
    "workflow_runs",
    "tasks",
    "outbox",
    "idempotency",
    "audit_log",
    "routes",
    "accounts",
    "approvals",
    "incidents",
    "semantic_evidence",
    "execution_outcomes",
)


def _new_pg() -> PostgresDurableConnection:
    if not PG_DSN:
        pytest.skip("no PostgreSQL DSN configured")
    try:
        conn = PostgresDurableConnection(PG_DSN)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL unavailable: {exc}")
    with conn.transaction() as db:
        db.execute("TRUNCATE " + ", ".join(_PG_TABLES) + " RESTART IDENTITY")
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


def test_translate_insert_or_ignore():
    out = _translate("INSERT OR IGNORE INTO idempotency (key) VALUES (?)")
    assert out == "INSERT INTO idempotency (key) VALUES (%s) ON CONFLICT DO NOTHING"


def test_translate_insert_or_replace():
    out = _translate(
        "INSERT OR REPLACE INTO routes (route_id, run_id, payload_json) VALUES (?, ?, ?)"
    )
    assert out.startswith(
        "INSERT INTO routes (route_id, run_id, payload_json) VALUES (%s, %s, %s)"
    )
    assert "ON CONFLICT (route_id) DO UPDATE SET" in out
    assert "run_id = EXCLUDED.run_id" in out
    assert "payload_json = EXCLUDED.payload_json" in out
    assert "route_id = EXCLUDED.route_id" not in out


def test_translate_normalizes_on_conflict_spacing():
    out = _translate(
        "INSERT INTO t (a) VALUES (?) ON CONFLICT(a) DO UPDATE SET a = excluded.a"
    )
    assert "ON CONFLICT (a) DO UPDATE" in out


def test_idempotency_runs_effect_once(conn):
    ledger = IdempotencyLedger(conn)
    calls = {"n": 0}

    def effect():
        calls["n"] += 1
        return {"value": calls["n"]}

    first = ledger.run("k1", effect)
    second = ledger.run("k1", effect)
    assert first == second == {"value": 1}
    assert calls["n"] == 1
    assert ledger.seen("k1") is True


def test_audit_chain_appends_and_verifies(conn):
    audit = DurableAuditLog(conn)
    a = audit.append("started", {"run_id": "r1"})
    b = audit.append("stepped", {"run_id": "r1"})
    c = audit.append("finished", {"run_id": "r2"})
    assert a.seq < b.seq < c.seq
    assert b.prev_hash == a.hash
    assert audit.verify() is True
    assert [r.event_type for r in audit.records(run_id="r1")] == ["started", "stepped"]


def test_queue_lease_complete_and_exhaust(conn):
    queue = DurableTaskQueue(conn)
    task = queue.enqueue("send", {"to": "x"}, available_at=T0)
    leased = queue.lease("worker-a", now=T0)
    assert leased is not None and leased.task_id == task.task_id
    assert queue.lease("worker-b", now=T0) is None
    assert queue.complete(task.task_id, "worker-a", result={"ok": True}) is True
    assert queue.get(task.task_id).status == TaskStatus.DONE


def test_queue_enqueue_is_idempotent(conn):
    queue = DurableTaskQueue(conn)
    first = queue.enqueue(
        "send", {"to": "x"}, idempotency_key="dedupe", available_at=T0
    )
    second = queue.enqueue(
        "send", {"to": "x"}, idempotency_key="dedupe", available_at=T0
    )
    assert first.task_id == second.task_id


def test_queue_reclaims_expired_lease(conn):
    queue = DurableTaskQueue(conn)
    task = queue.enqueue("send", {"to": "x"}, available_at=T0)
    queue.lease("worker-a", lease_seconds=1.0, now=T0)
    later = T0 + timedelta(seconds=5)
    assert queue.reclaim_expired(now=later) == [task.task_id]
    reclaimed = queue.lease("worker-b", now=later)
    assert reclaimed is not None and reclaimed.attempts == 2


def test_record_account_upsert(conn):
    records = DurableRecordStore(conn)
    records.save_account("acct-1", {"name": "before"})
    records.save_account("acct-1", {"name": "after"})
    assert records.get_account("acct-1") == {"name": "after"}


@pytest.mark.needs_postgres
def test_pg_idempotency_exactly_once_across_connections():
    if not PG_DSN:
        pytest.skip("no PostgreSQL DSN configured")
    primary = _new_pg()
    secondary = PostgresDurableConnection(PG_DSN)
    try:
        calls = {"n": 0}

        def effect():
            calls["n"] += 1
            return {"value": calls["n"]}

        first = IdempotencyLedger(primary).run("shared", effect)
        second = IdempotencyLedger(secondary).run("shared", lambda: {"value": 999})
        assert first == second == {"value": 1}
        assert calls["n"] == 1
    finally:
        primary.close()
        secondary.close()


@pytest.mark.needs_postgres
def test_pg_single_winner_lease_across_connections():
    if not PG_DSN:
        pytest.skip("no PostgreSQL DSN configured")
    primary = _new_pg()
    secondary = PostgresDurableConnection(PG_DSN)
    try:
        task = DurableTaskQueue(primary).enqueue("send", {"to": "x"}, available_at=T0)
        first = DurableTaskQueue(primary).lease("worker-a", now=T0)
        second = DurableTaskQueue(secondary).lease("worker-b", now=T0)
        winners = [leased for leased in (first, second) if leased is not None]
        assert len(winners) == 1
        assert winners[0].task_id == task.task_id
    finally:
        primary.close()
        secondary.close()
