"""Knowledge clients — read abstract hints, record outcomes, persist across sessions.

Same swappable-seam pattern as ``ExecutionBackend`` and ``Gateway``: a ``Protocol``
with implementations that drop in by config.

  * ``NoopKnowledgeClient`` — the offline default. Returns nothing, records nothing.
    The engine MUST navigate fully without a knowledge layer; this guarantees it.
  * ``LocalKnowledgeClient`` — SQLite-backed, single-machine memory. Turns the
    import->package resolution problem from a per-run cost into a derived-once fact
    that survives restarts. Useful even single-user.
  * RemoteKnowledgeClient (future) — the same Protocol over HTTP to the central
    server, with auth/rate-limiting/trust-scoring. Built later; nothing here changes.

SCOPE (locked): abstract knowledge only — ``DependencyHint`` (#1) and ``ErrorPattern``
(#2). No raw scripts / route templates. Every value is gated through ``scrubbing``
before storage, and identifiers must pass ``is_safe_identifier``, so secrets and PII
cannot reach the store even by accident.

CROWD-TRUST PRINCIPLE: locally learned hints are recorded with ``source=LOCAL``, never
``CROWD``. The resolver's trust floor already rejects unproven crowd hints; the store
reinforces that boundary by refusing to mint crowd-trust from local observations.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..models import (
    DependencyHint,
    ErrorClass,
    ErrorPattern,
    KnowledgeSource,
    RecalcStrategy,
)
from ..persistence import Migration, migrate
from .scrubbing import is_safe_identifier, is_safe_to_store, scrub
from .signature import error_pattern_key


def _create_knowledge_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS dependency_hints (
            import_name    TEXT NOT NULL,
            package_name   TEXT NOT NULL,
            pinned_version TEXT,
            success_count  INTEGER NOT NULL DEFAULT 0,
            failure_count  INTEGER NOT NULL DEFAULT 0,
            source         TEXT NOT NULL DEFAULT 'local',
            last_seen      TEXT NOT NULL,
            PRIMARY KEY (import_name, package_name)
        );
        CREATE TABLE IF NOT EXISTS error_patterns (
            key            TEXT PRIMARY KEY,
            error_class    TEXT NOT NULL,
            error_signature TEXT NOT NULL,
            strategy       TEXT NOT NULL,
            success_count  INTEGER NOT NULL DEFAULT 0,
            failure_count  INTEGER NOT NULL DEFAULT 0,
            source         TEXT NOT NULL DEFAULT 'local',
            last_seen      TEXT NOT NULL
        );
        """
    )


def _drop_knowledge_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        "DROP TABLE IF EXISTS error_patterns; DROP TABLE IF EXISTS dependency_hints;"
    )


# Ordered schema history for the local knowledge store. Append-only.
KNOWLEDGE_MIGRATIONS: tuple[Migration, ...] = (
    Migration(up=_create_knowledge_tables, down=_drop_knowledge_tables),
)


@runtime_checkable
class KnowledgeClient(Protocol):
    """Abstract-knowledge memory: read hints/patterns, record outcomes."""

    def all_dependency_hints(self) -> list[DependencyHint]: ...
    def get_dependency_hints(self, import_name: str) -> list[DependencyHint]: ...
    def record_dependency_success(
        self, import_name: str, package_name: str, *, version: str | None = None
    ) -> None: ...
    def record_dependency_failure(
        self, import_name: str, package_name: str
    ) -> None: ...
    def get_error_patterns(self, error_class: ErrorClass) -> list[ErrorPattern]: ...
    def record_error_pattern(
        self,
        error_class: ErrorClass,
        error_signature: str,
        strategy: RecalcStrategy,
        *,
        success: bool = True,
    ) -> None: ...
    def close(self) -> None: ...


class NoopKnowledgeClient:
    """Offline default — the engine works identically with no knowledge layer."""

    def all_dependency_hints(self) -> list[DependencyHint]:
        return []

    def get_dependency_hints(self, import_name: str) -> list[DependencyHint]:
        return []

    def record_dependency_success(
        self, import_name: str, package_name: str, *, version: str | None = None
    ) -> None:
        return None

    def record_dependency_failure(self, import_name: str, package_name: str) -> None:
        return None

    def get_error_patterns(self, error_class: ErrorClass) -> list[ErrorPattern]:
        return []

    def record_error_pattern(
        self,
        error_class: ErrorClass,
        error_signature: str,
        strategy: RecalcStrategy,
        *,
        success: bool = True,
    ) -> None:
        return None

    def close(self) -> None:
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.now(timezone.utc)


class LocalKnowledgeClient:
    """SQLite-backed local memory. Persists across sessions; safe even single-user."""

    def __init__(self, db_path: str | Path = ":memory:"):
        self._lock = threading.Lock()
        location = str(Path(db_path).expanduser())
        if location != ":memory:":
            # First run on a fresh machine: sqlite does not create dirs.
            Path(location).resolve().parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(location, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            migrate(self._conn, KNOWLEDGE_MIGRATIONS, label="knowledge")

    # --- dependency hints --------------------------------------------- #
    def all_dependency_hints(self) -> list[DependencyHint]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM dependency_hints").fetchall()
        return [self._row_to_hint(r) for r in rows]

    def get_dependency_hints(self, import_name: str) -> list[DependencyHint]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM dependency_hints WHERE import_name = ?", (import_name,)
            ).fetchall()
        return [self._row_to_hint(r) for r in rows]

    def record_dependency_success(
        self, import_name: str, package_name: str, *, version: str | None = None
    ) -> None:
        self._record_dependency(
            import_name, package_name, version=version, success=True
        )

    def record_dependency_failure(self, import_name: str, package_name: str) -> None:
        self._record_dependency(import_name, package_name, version=None, success=False)

    def _record_dependency(
        self, import_name: str, package_name: str, *, version: str | None, success: bool
    ) -> None:
        # Strict identifier gate: a mapping field can never carry a path or secret.
        if not (is_safe_identifier(import_name) and is_safe_identifier(package_name)):
            return
        if version is not None and not is_safe_to_store(version):
            version = None
        s_inc, f_inc = (1, 0) if success else (0, 1)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO dependency_hints
                    (import_name, package_name, pinned_version, success_count, failure_count, source, last_seen)
                VALUES (?, ?, ?, ?, ?, 'local', ?)
                ON CONFLICT(import_name, package_name) DO UPDATE SET
                    success_count = success_count + ?,
                    failure_count = failure_count + ?,
                    pinned_version = COALESCE(excluded.pinned_version, pinned_version),
                    last_seen = excluded.last_seen
                """,
                (
                    import_name,
                    package_name,
                    version,
                    s_inc,
                    f_inc,
                    _now(),
                    s_inc,
                    f_inc,
                ),
            )
            self._conn.commit()

    @staticmethod
    def _row_to_hint(row: sqlite3.Row) -> DependencyHint:
        return DependencyHint(
            import_name=row["import_name"],
            package_name=row["package_name"],
            pinned_version=row["pinned_version"],
            source=KnowledgeSource(row["source"]),
            success_count=row["success_count"],
            failure_count=row["failure_count"],
            last_seen=_parse_ts(row["last_seen"]),
        )

    # --- error patterns ----------------------------------------------- #
    def get_error_patterns(self, error_class: ErrorClass) -> list[ErrorPattern]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM error_patterns WHERE error_class = ?",
                (error_class.value,),
            ).fetchall()
        return [self._row_to_pattern(r) for r in rows]

    def record_error_pattern(
        self,
        error_class: ErrorClass,
        error_signature: str,
        strategy: RecalcStrategy,
        *,
        success: bool = True,
    ) -> None:
        # Error signatures are already normalized, but scrub again as a hard gate and
        # refuse to store anything that still looks secret-bearing.
        signature = scrub(error_signature)
        if not is_safe_to_store(signature):
            return
        key = error_pattern_key(error_class, signature)
        s_inc, f_inc = (1, 0) if success else (0, 1)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO error_patterns
                    (key, error_class, error_signature, strategy, success_count, failure_count, source, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, 'local', ?)
                ON CONFLICT(key) DO UPDATE SET
                    success_count = success_count + ?,
                    failure_count = failure_count + ?,
                    strategy = excluded.strategy,
                    last_seen = excluded.last_seen
                """,
                (
                    key,
                    error_class.value,
                    signature,
                    strategy.value,
                    s_inc,
                    f_inc,
                    _now(),
                    s_inc,
                    f_inc,
                ),
            )
            self._conn.commit()

    @staticmethod
    def _row_to_pattern(row: sqlite3.Row) -> ErrorPattern:
        return ErrorPattern(
            error_class=ErrorClass(row["error_class"]),
            error_signature=row["error_signature"],
            strategy=RecalcStrategy(row["strategy"]),
            source=KnowledgeSource(row["source"]),
            success_count=row["success_count"],
            failure_count=row["failure_count"],
            last_seen=_parse_ts(row["last_seen"]),
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # Context-manager sugar for tests / scripts.
    def __enter__(self) -> "LocalKnowledgeClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def as_hint_provider(self):
        """Adapter for the graph's plan node: a callable (intent) -> all hints.
        Dependency hints are keyed by import, not intent, so we seed the full (small)
        set and let the resolver match by import name."""
        return lambda _intent: self.all_dependency_hints()
