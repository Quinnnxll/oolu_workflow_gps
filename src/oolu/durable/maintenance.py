"""Backup, restore, retention, and deletion workflows for the durable runtime.

Backups use SQLite's online backup API (consistent without stopping writers).
Restore reopens a backup file as a live durable connection. Retention prunes
terminal queue tasks and delivered outbox messages past a cutoff. Deletion removes
a single workflow's mutable records for data-subject erasure while leaving the
append-only, tamper-evident audit chain intact — the record of *what happened*
survives and stays verifiable, which is the point of an audit log.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .connection import DurableConnection


def backup(conn: DurableConnection, destination: str | Path) -> Path:
    """Write a consistent copy of the durable database to ``destination``."""
    dest = Path(destination).expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)
    with conn.lock:
        target = sqlite3.connect(str(dest))
        try:
            conn.db.backup(target)
        finally:
            target.close()
    return dest


def restore(source: str | Path) -> DurableConnection:
    """Open a backup file as a live durable connection (migrations are a no-op)."""
    return DurableConnection(source)


def prune_retention(
    conn: DurableConnection,
    *,
    older_than_days: float,
    now: datetime | None = None,
) -> dict[str, int]:
    """Delete terminal tasks and delivered outbox messages older than the cutoff."""
    moment = now or datetime.now(UTC)
    cutoff = (moment - timedelta(days=older_than_days)).isoformat()
    with conn.transaction() as db:
        tasks = db.execute(
            "DELETE FROM tasks WHERE status IN ('done', 'cancelled', 'dead')"
            " AND updated_at < ?",
            (cutoff,),
        ).rowcount
        messages = db.execute(
            "DELETE FROM outbox WHERE status = 'sent' AND sent_at < ?",
            (cutoff,),
        ).rowcount
    return {"tasks": tasks, "outbox": messages}


def delete_workflow(conn: DurableConnection, run_id: str) -> dict[str, int]:
    """Erase one workflow's mutable records; the immutable audit chain is retained."""
    counts: dict[str, int] = {}
    with conn.transaction() as db:
        for table in (
            "workflow_runs",
            "routes",
            "approvals",
            "incidents",
            "semantic_evidence",
            "execution_outcomes",
        ):
            counts[table] = db.execute(
                f"DELETE FROM {table} WHERE run_id = ?", (run_id,)
            ).rowcount
    return counts
