from __future__ import annotations

import os
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from .connection import DURABLE_SCHEMA_VERSION

try:
    import psycopg
    from psycopg.rows import dict_row
except ModuleNotFoundError as exc:  # pragma: no cover
    raise ModuleNotFoundError(
        "PostgresDurableConnection requires the 'postgres' extra: "
        "pip install 'oolu[postgres]'"
    ) from exc

# Every table any store upserts with SQLite's INSERT OR REPLACE, with its
# primary key (tuples for composite keys). The translation needs the
# conflict target PostgreSQL demands; a table missing here fails LOUDLY
# below with directions, never with a bare KeyError in production.
_REPLACE_PK: dict[str, tuple[str, ...]] = {
    "routes": ("route_id",),
    "semantic_evidence": ("evidence_id",),
    "skill_registry": ("skill_id", "semver"),
    "trace_node_stats": ("node_key", "context"),
    "graph_proposals": ("proposal_id",),
    "market_reference_prices": ("class_key",),
    "market_policies": ("tenant", "principal"),
    "market_sales_policies": ("tenant", "principal"),
}


def _translate(sql: str) -> str:
    s = sql
    replace = re.match(
        r"\s*INSERT\s+OR\s+REPLACE\s+INTO\s+(\w+)\s*\(([^)]+)\)", s, re.I
    )
    if replace:
        table = replace.group(1)
        columns = [c.strip() for c in replace.group(2).split(",")]
        pk = _REPLACE_PK.get(table)
        if pk is None:
            raise NotImplementedError(
                f"INSERT OR REPLACE INTO {table} has no PostgreSQL "
                "translation: add the table's primary key to _REPLACE_PK, "
                "or write the statement as a portable ON CONFLICT upsert"
            )
        assignments = ", ".join(
            f"{c} = EXCLUDED.{c}" for c in columns if c not in pk
        )
        s = re.sub(
            r"\s*INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", s, count=1, flags=re.I
        )
        s = (
            f"{s.rstrip().rstrip(';')} ON CONFLICT ({', '.join(pk)}) "
            f"DO UPDATE SET {assignments}"
        )
    elif re.match(r"\s*INSERT\s+OR\s+IGNORE\s+INTO", s, re.I):
        s = re.sub(
            r"\s*INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", s, count=1, flags=re.I
        )
        s = f"{s.rstrip().rstrip(';')} ON CONFLICT DO NOTHING"
    # SQLite's rowid-alias autoincrement is a syntax error on PostgreSQL;
    # BIGSERIAL is the same contract (monotonic per-table sequence). This
    # runs at CREATE TABLE time — exactly where a marketplace store on the
    # production connection would otherwise crash the gateway at boot.
    s = re.sub(
        r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT",
        "BIGSERIAL PRIMARY KEY",
        s,
        flags=re.I,
    )
    s = s.replace("?", "%s")
    s = re.sub(r"ON CONFLICT\(", "ON CONFLICT (", s, flags=re.I)
    return s


# Tables whose stores read cursor.lastrowid after an INSERT (SQLite's
# rowid). PostgreSQL has no lastrowid, so the wrapper appends
# RETURNING <serial column> for exactly these inserts and carries the
# value — same contract, both engines.
_SERIAL_COLUMN = {
    "audit_log": "seq",
    "build_attempts": "id",
    "build_lessons": "id",
    "graph_edges": "id",
    "memory_spine": "id",
}


def _serial_return(sql: str) -> str | None:
    insert = re.match(r"\s*INSERT\s+INTO\s+(\w+)", sql, re.I)
    if insert is None:
        return None
    return _SERIAL_COLUMN.get(insert.group(1))


class _CursorWrapper:
    def __init__(self, cursor: Any, lastrowid: int | None = None) -> None:
        self._cursor = cursor
        self.lastrowid = lastrowid

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    def fetchone(self) -> Any:
        return self._cursor.fetchone()

    def fetchall(self) -> list[Any]:
        return self._cursor.fetchall()


class _DialectConnection:
    def __init__(self, pg: Any) -> None:
        self._pg = pg

    def execute(self, sql: str, params: tuple = ()) -> _CursorWrapper:
        translated = _translate(sql)
        serial = _serial_return(translated)
        if serial is not None:
            translated = f"{translated} RETURNING {serial}"
            cursor = self._pg.execute(translated, params)
            row = cursor.fetchone()
            return _CursorWrapper(
                cursor, int(row[serial]) if row else None
            )
        return _CursorWrapper(self._pg.execute(translated, params))


def _create_pg_schema(pg: Any) -> None:
    statements = (
        """CREATE TABLE IF NOT EXISTS workflow_runs (
               run_id TEXT PRIMARY KEY,
               schema_version INTEGER NOT NULL,
               phase TEXT NOT NULL,
               paused INTEGER NOT NULL DEFAULT 0,
               payload_json TEXT NOT NULL,
               updated_at TEXT NOT NULL
           )""",
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
           )""",
        """CREATE TABLE IF NOT EXISTS outbox (
               msg_id TEXT PRIMARY KEY,
               topic TEXT NOT NULL,
               payload_json TEXT NOT NULL,
               status TEXT NOT NULL DEFAULT 'pending',
               attempts INTEGER NOT NULL DEFAULT 0,
               created_at TEXT NOT NULL,
               sent_at TEXT
           )""",
        """CREATE TABLE IF NOT EXISTS idempotency (
               key TEXT PRIMARY KEY,
               scope TEXT NOT NULL,
               result_json TEXT,
               created_at TEXT NOT NULL
           )""",
        """CREATE TABLE IF NOT EXISTS audit_log (
               seq BIGSERIAL PRIMARY KEY,
               entry_id TEXT NOT NULL,
               run_id TEXT,
               event_type TEXT NOT NULL,
               payload_json TEXT NOT NULL,
               prev_hash TEXT NOT NULL,
               hash TEXT NOT NULL,
               at TEXT NOT NULL
           )""",
        # Schema v2: the stats and activity paths ask the audit log by run
        # and by event type — indexed on both backends alike.
        """CREATE INDEX IF NOT EXISTS idx_audit_log_run
           ON audit_log(run_id)""",
        """CREATE INDEX IF NOT EXISTS idx_audit_log_type_run
           ON audit_log(event_type, run_id)""",
        """CREATE TABLE IF NOT EXISTS routes (
               route_id TEXT PRIMARY KEY,
               run_id TEXT NOT NULL,
               payload_json TEXT NOT NULL,
               created_at TEXT NOT NULL
           )""",
        """CREATE TABLE IF NOT EXISTS accounts (
               account_id TEXT PRIMARY KEY,
               payload_json TEXT NOT NULL,
               created_at TEXT NOT NULL,
               updated_at TEXT NOT NULL
           )""",
        """CREATE TABLE IF NOT EXISTS approvals (
               approval_id TEXT PRIMARY KEY,
               run_id TEXT NOT NULL,
               principal TEXT NOT NULL,
               policy TEXT NOT NULL,
               decision TEXT NOT NULL,
               payload_json TEXT NOT NULL,
               created_at TEXT NOT NULL
           )""",
        """CREATE TABLE IF NOT EXISTS incidents (
               incident_id TEXT PRIMARY KEY,
               run_id TEXT NOT NULL,
               reason TEXT NOT NULL,
               severity TEXT NOT NULL,
               resolution TEXT,
               payload_json TEXT NOT NULL,
               created_at TEXT NOT NULL
           )""",
        """CREATE TABLE IF NOT EXISTS semantic_evidence (
               evidence_id TEXT PRIMARY KEY,
               run_id TEXT NOT NULL,
               payload_json TEXT NOT NULL,
               created_at TEXT NOT NULL
           )""",
        """CREATE TABLE IF NOT EXISTS execution_outcomes (
               run_id TEXT NOT NULL,
               idempotency_key TEXT NOT NULL,
               status TEXT NOT NULL,
               payload_json TEXT NOT NULL,
               completed_at TEXT,
               PRIMARY KEY (run_id, idempotency_key)
           )""",
    )
    for statement in statements:
        pg.execute(statement)


def _migrate_pg(pg: Any) -> None:
    pg.execute(
        "CREATE TABLE IF NOT EXISTS durable_schema_version ("
        " label TEXT PRIMARY KEY, version INTEGER NOT NULL)"
    )
    row = pg.execute(
        "SELECT version FROM durable_schema_version WHERE label = %s", ("durable",)
    ).fetchone()
    current = int(row["version"]) if row else 0
    if current < DURABLE_SCHEMA_VERSION:
        _create_pg_schema(pg)
        if current < 3:
            # Version 3: the poll floor is closed (the Poll agent was
            # removed) — its tables leave on the production adapter
            # exactly as on the local one, so no member's vote lingers
            # unreachable. The pairwise preference book stays.
            for table in (
                "poll_pairs",
                "poll_votes",
                "poll_genre_stats",
                "poll_findings",
            ):
                pg.execute(f"DROP TABLE IF EXISTS {table}")
        pg.execute(
            "INSERT INTO durable_schema_version (label, version) VALUES (%s, %s)"
            " ON CONFLICT (label) DO UPDATE SET version = EXCLUDED.version",
            ("durable", DURABLE_SCHEMA_VERSION),
        )


class PostgresDurableConnection:
    # The multi-process production backend: the only durable adapter on which
    # real money movement is permitted (invariant #8).
    is_production_durable = True

    def __init__(self, dsn: str | None = None) -> None:
        resolved = dsn or os.environ.get("DATABASE_URL")
        if not resolved:
            raise ValueError("a PostgreSQL DSN or DATABASE_URL is required")
        self.path = resolved
        self.lock = threading.RLock()
        self._pg = psycopg.connect(resolved, autocommit=True, row_factory=dict_row)
        self.db = _DialectConnection(self._pg)
        with self.lock:
            _migrate_pg(self._pg)

    @contextmanager
    def transaction(self) -> Iterator[_DialectConnection]:
        with self.lock, self._pg.transaction():
            yield self.db

    def close(self) -> None:
        with self.lock:
            self._pg.close()
