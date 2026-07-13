"""Self-referencing upserts must speak BOTH dialects.

The production incident this pins down: ``ON CONFLICT ... DO UPDATE SET
calls = calls + 1`` is legal SQLite but ambiguous PostgreSQL (the target
row and ``excluded`` are both in scope), so the usage booking that runs
after EVERY successful hosted-model reply crashed the whole chat turn —
on Postgres only, which the SQLite-backed suite could never see. The
right-hand side must qualify the table (``model_usage.calls + 1``).

The dialect test needs a real PostgreSQL (OOLU_TEST_PG_DSN or
DATABASE_URL, same switch as test_durable_postgres); the SQLite half of
the promise always runs.
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
