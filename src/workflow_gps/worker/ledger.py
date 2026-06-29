"""Lease ledgers: single-use consumption and revocation.

The ledger is what makes a lease single-use and revocable. ``consume`` records a
lease id atomically and returns ``False`` if it was already recorded (a replay);
``revoke``/``is_revoked`` back cancellation. A durable SQLite ledger is provided so
the single-use guarantee survives worker restarts; an in-memory one supports tests.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..persistence import Migration, migrate


@runtime_checkable
class LeaseLedger(Protocol):
    def consume(self, lease_id: str) -> bool:
        """Record ``lease_id``; return True if newly consumed, False if a replay."""
        ...

    def revoke(self, lease_id: str) -> None: ...

    def is_revoked(self, lease_id: str) -> bool: ...


class InMemoryLeaseLedger:
    def __init__(self) -> None:
        self._consumed: set[str] = set()
        self._revoked: set[str] = set()
        self._lock = threading.RLock()

    def consume(self, lease_id: str) -> bool:
        with self._lock:
            if lease_id in self._consumed:
                return False
            self._consumed.add(lease_id)
            return True

    def revoke(self, lease_id: str) -> None:
        with self._lock:
            self._revoked.add(lease_id)

    def is_revoked(self, lease_id: str) -> bool:
        with self._lock:
            return lease_id in self._revoked


def _create_lease_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS consumed_leases ("
        "lease_id TEXT PRIMARY KEY, consumed_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS revoked_leases ("
        "lease_id TEXT PRIMARY KEY, revoked_at TEXT NOT NULL)"
    )


def _drop_lease_schema(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS revoked_leases")
    conn.execute("DROP TABLE IF EXISTS consumed_leases")


WORKER_MIGRATIONS: tuple[Migration, ...] = (
    Migration(up=_create_lease_schema, down=_drop_lease_schema),
)


class LocalLeaseLedger:
    """Durable single-use + revocation ledger; the guarantee survives restarts."""

    def __init__(self, path: str | Path):
        db_path = str(path) if str(path) == ":memory:" else str(Path(path).expanduser())
        if db_path != ":memory:":
            Path(db_path).resolve().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            migrate(self._db, WORKER_MIGRATIONS, label="worker")

    def consume(self, lease_id: str) -> bool:
        with self._lock, self._db:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO consumed_leases (lease_id, consumed_at)"
                " VALUES (?, ?)",
                (lease_id, datetime.now(UTC).isoformat()),
            )
        return cursor.rowcount > 0

    def revoke(self, lease_id: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO revoked_leases (lease_id, revoked_at)"
                " VALUES (?, ?)",
                (lease_id, datetime.now(UTC).isoformat()),
            )

    def is_revoked(self, lease_id: str) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM revoked_leases WHERE lease_id = ?", (lease_id,)
            ).fetchone()
        return row is not None

    def close(self) -> None:
        with self._lock:
            self._db.close()
