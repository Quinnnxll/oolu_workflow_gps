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
    """Retention across the durable runtime, one cutoff: terminal tasks,
    delivered outbox messages, TERMINAL workflow runs (completed, failed,
    cancelled — the dead Noder threads nobody revives), and the audit
    chain's oldest prefix (through :meth:`DurableAuditLog.prune`, which
    attests the cut so the surviving chain still verifies). Live and
    paused runs are never touched — retention trims history, not work."""
    from .audit import DurableAuditLog

    moment = now or datetime.now(UTC)
    cutoff_at = moment - timedelta(days=older_than_days)
    cutoff = cutoff_at.isoformat()
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
        runs = db.execute(
            "DELETE FROM workflow_runs WHERE updated_at < ?"
            " AND phase IN ('completed', 'failed', 'cancelled')",
            (cutoff,),
        ).rowcount
    audit = DurableAuditLog(conn).prune(older_than=cutoff_at)
    return {"tasks": tasks, "outbox": messages, "runs": runs, "audit": audit}


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
