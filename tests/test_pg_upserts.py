"""Durable-store SQL must speak BOTH dialects.

Two production incidents pinned down here, one class: SQL that SQLite
accepts and PostgreSQL refuses, crashing prod while the SQLite-backed
suite stays green.

* ``ON CONFLICT ... DO UPDATE SET calls = calls + 1`` — legal SQLite,
  ambiguous PostgreSQL (the target row and ``excluded`` are both in
  scope). The usage booking after EVERY hosted-model reply crashed the
  chat turn. The right-hand side must qualify the table.
* ``ORDER BY rowid`` — SQLite's implicit column; PostgreSQL has no such
  thing (UndefinedColumn). Listing holds inside a node's interact chat
  crashed the turn. Durable stores must order by their own columns or
  sort the parsed records.

The dialect tests need a real PostgreSQL (OOLU_TEST_PG_DSN or
DATABASE_URL, same switch as test_durable_postgres); the SQLite halves
and the static tripwires always run.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from oolu.billing import ModelUsageStore
from oolu.durable.connection import DurableConnection

PG_DSN = os.environ.get("OOLU_TEST_PG_DSN") or os.environ.get("DATABASE_URL")


def _exercise(conn) -> dict:
    usage = ModelUsageStore(conn)
    usage.record("t1", source="subscription", cost=0.25, prompt_tokens=10)
    # The second write takes the ON CONFLICT path — where the ambiguity was.
    usage.record("t1", source="subscription", cost=0.75, prompt_tokens=20)
    [row] = usage.view("t1")
    return row


def test_the_usage_upsert_accumulates_on_sqlite(tmp_path):
    conn = DurableConnection(tmp_path / "usage.db")
    row = _exercise(conn)
    assert row["calls"] == 2 and row["cost_usd"] == pytest.approx(1.0)
    assert row["prompt_tokens"] == 30
    conn.close()


def test_the_usage_upsert_accumulates_on_postgres():
    if not PG_DSN:
        pytest.skip("no PostgreSQL DSN configured")
    from oolu.durable.postgres import PostgresDurableConnection

    conn = PostgresDurableConnection(PG_DSN)
    ModelUsageStore(conn)  # ensure the table exists on a fresh database
    with conn.transaction() as db:
        db.execute("DELETE FROM model_usage WHERE tenant_id = 't1'")
    row = _exercise(conn)
    assert row["calls"] == 2 and row["cost_usd"] == pytest.approx(1.0)
    conn.close()


def test_no_durable_upsert_self_references_without_qualifying():
    """The tripwire for the whole class: any DO UPDATE SET arithmetic on a
    durable store (the ones that can ride Postgres) must qualify its
    columns. Local-only sqlite3 stores are exempt — they never translate.
    """
    src = Path(__file__).resolve().parent.parent / "src" / "oolu"
    offenders: list[str] = []
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        durable = "._conn.transaction()" in text or "conn.transaction()" in text
        if not durable or "DO UPDATE" not in text:
            continue
        for match in re.finditer(
            r"DO UPDATE SET(.{0,600}?)(?:\"\"\"|''')", text, re.S
        ):
            block = match.group(1)
            # A bare column on the RIGHT of an assignment (arithmetic or
            # COALESCE fallback) is the PostgreSQL ambiguity.
            for assign in re.finditer(
                r"=\s*([A-Za-z_.]+)\s*[+\-]|COALESCE\([^)]*?,\s*([a-z_]+)\)",
                block,
            ):
                name = assign.group(1) or assign.group(2)
                if name and "." not in name and name != "excluded":
                    offenders.append(f"{path.relative_to(src)}: {name}")
    assert not offenders, "unqualified upsert self-references:\n" + "\n".join(
        offenders
    )


def test_no_source_sql_orders_by_rowid():
    """The tripwire for the second class: ``rowid`` is SQLite's implicit
    column, so any SQL naming it is an UndefinedColumn on the PostgreSQL
    backend. (``cursor.lastrowid`` is a Python DB-API attribute, not SQL,
    and the PG shim provides it — that one is fine.)"""
    src = Path(__file__).resolve().parent.parent / "src" / "oolu"
    offenders = [
        f"{path.relative_to(src)}:{n}"
        for path in src.rglob("*.py")
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"(?<!last)rowid", line.split("#", 1)[0], re.I)
    ]
    assert not offenders, "SQLite-only rowid in SQL:\n" + "\n".join(offenders)


def test_holds_list_and_sweep_speak_postgres(tmp_path):
    """The exact prod crash: 'pending' inside a node's interact chat lists
    holds, which swept expired ones with ORDER BY rowid — UndefinedColumn
    on PostgreSQL. The store must list, in insertion order, on BOTH
    backends."""
    from datetime import UTC, datetime, timedelta

    from oolu.nodeplace.holds import PendingContractRecord, PendingContractStore

    def exercise(conn):
        store = PendingContractStore(conn)
        with conn.transaction() as db:
            db.execute("DELETE FROM pending_contracts")
        base = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
        for i in range(3):
            store.add(
                PendingContractRecord(
                    pending_id=f"p{i}",
                    contract={"name": f"job-{i}"},
                    consumer_tenant="t1",
                    created_at=base + timedelta(minutes=i),
                    expires_at=None if i else base,  # p0 already expired
                )
            )
        assert [r.pending_id for r in store.list(tenant="t1")] == ["p0", "p1", "p2"]
        swept = store.sweep_expired(base + timedelta(hours=1))
        assert [r.pending_id for r in swept] == ["p0"]
        assert [r.pending_id for r in store.list()] == ["p1", "p2"]

    exercise(DurableConnection(tmp_path / "holds.db"))
    if PG_DSN:
        from oolu.durable.postgres import PostgresDurableConnection

        conn = PostgresDurableConnection(PG_DSN)
        exercise(conn)
        conn.close()
