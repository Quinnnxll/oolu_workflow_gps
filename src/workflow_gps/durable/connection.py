"""The durable-runtime database: one versioned SQLite file shared by every store.

The durable runtime keeps all of its tables — run-state checkpoints, the task
queue, the transactional outbox, the idempotency ledger, the hash-linked audit
log, and the domain record stores — in one physical database so a state change and
the events/tasks it spawns commit in a single transaction (the basis for
exactly-once processing across restarts). The schema is versioned through the
shared ``persistence`` migration runner, and a single ``DurableConnection`` owns
the connection and a re-entrant lock so cross-store writes can be made atomic.

This is the *local* durable adapter. The same table contract and idempotency /
lease semantics are what a PostgreSQL adapter implements for the multi-process
production deployment (see ``docs/ADAPTER_MATURITY.md``); the contract tests are
written against the port, not this implementation.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..persistence import Migration, migrate

DURABLE_SCHEMA_VERSION = 1


def _create_durable_schema(conn: sqlite3.Connection) -> None:
    # Durable workflow-state checkpoints.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS workflow_runs (
               run_id TEXT PRIMARY KEY,
               schema_version INTEGER NOT NULL,
               phase TEXT NOT NULL,
               paused INTEGER NOT NULL DEFAULT 0,
               payload_json TEXT NOT NULL,
               updated_at TEXT NOT NULL
           )"""
    )
    # Durable task queue with leases, heartbeats, attempts, and retry scheduling.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tasks (
               task_id TEXT PRIMARY KEY,
               kind TEXT NOT NULL,
               payload_json TEXT NOT NULL,
               status TEXT NOT NULL,
               available_at TEXT NOT NULL,
               lease_owner TEXT,
               lease_expires_at TEXT,
               heartbeat_at TEXT,
               attempts INTEGER NOT NULL DEFAULT 0,
               max_attempts INTEGER NOT NULL DEFAULT 5,
               idempotency_key TEXT UNIQUE,
               result_json TEXT,
               error TEXT,
               created_at TEXT NOT NULL,
               updated_at TEXT NOT NULL
           )"""
    )
    # Transactional outbox for events and notifications.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS outbox (
               msg_id TEXT PRIMARY KEY,
               topic TEXT NOT NULL,
               payload_json TEXT NOT NULL,
               status TEXT NOT NULL DEFAULT 'pending',
               attempts INTEGER NOT NULL DEFAULT 0,
               created_at TEXT NOT NULL,
               sent_at TEXT
           )"""
    )
    # Idempotency ledger for externally visible mutations.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS idempotency (
               key TEXT PRIMARY KEY,
               scope TEXT NOT NULL,
               result_json TEXT,
               created_at TEXT NOT NULL
           )"""
    )
    # Hash-linked, append-only audit log.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS audit_log (
               seq INTEGER PRIMARY KEY AUTOINCREMENT,
               entry_id TEXT NOT NULL,
               run_id TEXT,
               event_type TEXT NOT NULL,
               payload_json TEXT NOT NULL,
               prev_hash TEXT NOT NULL,
               hash TEXT NOT NULL,
               at TEXT NOT NULL
           )"""
    )
    # Domain record stores (routes, accounts, approvals, incidents, semantic
    # evidence, execution outcomes).
    conn.execute(
        """CREATE TABLE IF NOT EXISTS routes (
               route_id TEXT PRIMARY KEY,
               run_id TEXT NOT NULL,
               payload_json TEXT NOT NULL,
               created_at TEXT NOT NULL
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS accounts (
               account_id TEXT PRIMARY KEY,
               payload_json TEXT NOT NULL,
               created_at TEXT NOT NULL,
               updated_at TEXT NOT NULL
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS approvals (
               approval_id TEXT PRIMARY KEY,
               run_id TEXT NOT NULL,
               principal TEXT NOT NULL,
               policy TEXT NOT NULL,
               decision TEXT NOT NULL,
               payload_json TEXT NOT NULL,
               created_at TEXT NOT NULL
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS incidents (
               incident_id TEXT PRIMARY KEY,
               run_id TEXT NOT NULL,
               reason TEXT NOT NULL,
               severity TEXT NOT NULL,
               resolution TEXT,
               payload_json TEXT NOT NULL,
               created_at TEXT NOT NULL
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS semantic_evidence (
               evidence_id TEXT PRIMARY KEY,
               run_id TEXT NOT NULL,
               payload_json TEXT NOT NULL,
               created_at TEXT NOT NULL
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS execution_outcomes (
               run_id TEXT NOT NULL,
               idempotency_key TEXT NOT NULL,
               status TEXT NOT NULL,
               payload_json TEXT NOT NULL,
               completed_at TEXT,
               PRIMARY KEY (run_id, idempotency_key)
           )"""
    )


def _drop_durable_schema(conn: sqlite3.Connection) -> None:
    for table in (
        "execution_outcomes",
        "semantic_evidence",
        "incidents",
        "approvals",
        "accounts",
        "routes",
        "audit_log",
        "idempotency",
        "outbox",
        "tasks",
        "workflow_runs",
    ):
        conn.execute(f"DROP TABLE IF EXISTS {table}")


# Ordered schema history for the durable runtime. Append-only: add new steps,
# never edit a released one.
DURABLE_MIGRATIONS: tuple[Migration, ...] = (
    Migration(up=_create_durable_schema, down=_drop_durable_schema),
)


class DurableConnection:
    """Owns one SQLite connection and a re-entrant lock for the durable runtime."""

    def __init__(self, path: str | Path):
        db_path = str(path) if str(path) == ":memory:" else str(Path(path).expanduser())
        if db_path != ":memory:":
            Path(db_path).resolve().parent.mkdir(parents=True, exist_ok=True)
        self.path = db_path
        self.lock = threading.RLock()
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        with self.lock:
            migrate(self.db, DURABLE_MIGRATIONS, label="durable")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block under the lock inside a single committed transaction."""
        with self.lock, self.db:
            yield self.db

    def close(self) -> None:
        with self.lock:
            self.db.close()
