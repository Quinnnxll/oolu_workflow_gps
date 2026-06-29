"""Script cache protocol plus no-op and local SQLite implementations."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..persistence import Migration, migrate
from .models import ScriptCacheEntry


def _create_script_cache(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS script_cache (
            cache_key TEXT PRIMARY KEY,
            script TEXT NOT NULL,
            dependencies TEXT NOT NULL,
            tier TEXT NOT NULL,
            model TEXT NOT NULL,
            success_count INTEGER NOT NULL DEFAULT 0,
            failure_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )


# Ordered schema history for the local script cache. Append new steps; never edit
# a released one. Version N == len(MIGRATIONS[:N]) applied.
MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        up=_create_script_cache,
        down=lambda conn: conn.execute("DROP TABLE IF EXISTS script_cache"),
    ),
)


@runtime_checkable
class ScriptCache(Protocol):
    def get(self, key: str) -> ScriptCacheEntry | None: ...

    def store_success(
        self, key: str, *, script: str, dependencies: list[str], tier: str, model: str
    ) -> None: ...

    def record_failure(self, key: str) -> None: ...

    def close(self) -> None: ...


class NoopScriptCache:
    def get(self, key: str) -> ScriptCacheEntry | None:
        return None

    def store_success(
        self, key: str, *, script: str, dependencies: list[str], tier: str, model: str
    ) -> None:
        return None

    def record_failure(self, key: str) -> None:
        return None

    def close(self) -> None:
        return None


class LocalScriptCache:
    """Small local cache. Entries with two failures are ignored until replaced."""

    FAILURE_LIMIT = 2

    def __init__(self, path: str | Path):
        db_path = str(path) if str(path) == ":memory:" else str(Path(path).expanduser())
        if db_path != ":memory:":
            Path(db_path).resolve().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            migrate(self._db, MIGRATIONS, label="script_cache")

    def get(self, key: str) -> ScriptCacheEntry | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM script_cache WHERE cache_key = ? AND failure_count < ?",
                (key, self.FAILURE_LIMIT),
            ).fetchone()
        if row is None:
            return None
        return ScriptCacheEntry(
            key=row["cache_key"],
            script=row["script"],
            dependencies=tuple(json.loads(row["dependencies"])),
            tier=row["tier"],
            model=row["model"],
            success_count=row["success_count"],
            failure_count=row["failure_count"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def store_success(
        self, key: str, *, script: str, dependencies: list[str], tier: str, model: str
    ) -> None:
        now = datetime.now(UTC).isoformat()
        deps = json.dumps(sorted(set(dependencies)), separators=(",", ":"))
        with self._lock, self._db:
            self._db.execute(
                """
                INSERT INTO script_cache (
                    cache_key, script, dependencies, tier, model,
                    success_count, failure_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, 0, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    script = excluded.script,
                    dependencies = excluded.dependencies,
                    tier = excluded.tier,
                    model = excluded.model,
                    success_count = script_cache.success_count + 1,
                    failure_count = 0,
                    updated_at = excluded.updated_at
                """,
                (key, script, deps, tier, model, now, now),
            )

    def record_failure(self, key: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._lock, self._db:
            self._db.execute(
                """UPDATE script_cache
                   SET failure_count = failure_count + 1, updated_at = ?
                   WHERE cache_key = ?""",
                (now, key),
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()
