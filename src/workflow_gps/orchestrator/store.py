"""Run-state persistence: in-memory and versioned local SQLite.

Both stores keep only the serialized ``RunState`` JSON, so pause/resume and
durability reduce to saving and reloading one object (ADR-0002). The SQLite store
versions its schema through the shared ``persistence`` migration runner.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..persistence import Migration, migrate
from .state import ORCHESTRATOR_SCHEMA_VERSION, RunState


def _create_run_state(conn: sqlite3.Connection) -> None:
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


def _drop_run_state(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS workflow_runs")


# Ordered schema history for the orchestrator run-state store. Append-only.
RUN_STATE_MIGRATIONS: tuple[Migration, ...] = (
    Migration(up=_create_run_state, down=_drop_run_state),
)


@runtime_checkable
class RunStateStore(Protocol):
    def save(self, state: RunState) -> None: ...

    def get(self, run_id: str) -> RunState | None: ...

    def list(self, *, limit: int = 100) -> list[RunState]: ...

    def delete(self, run_id: str) -> bool: ...

    def close(self) -> None: ...


class InMemoryRunStateStore:
    def __init__(self):
        self._payloads: dict[str, str] = {}

    def save(self, state: RunState) -> None:
        # Store serialized JSON so in-memory behaves exactly like a durable store.
        self._payloads[state.run_id] = state.model_dump_json()

    def get(self, run_id: str) -> RunState | None:
        payload = self._payloads.get(run_id)
        return RunState.model_validate_json(payload) if payload is not None else None

    def list(self, *, limit: int = 100) -> list[RunState]:
        states = [
            RunState.model_validate_json(payload) for payload in self._payloads.values()
        ]
        return sorted(states, key=lambda item: item.updated_at, reverse=True)[:limit]

    def delete(self, run_id: str) -> bool:
        return self._payloads.pop(run_id, None) is not None

    def close(self) -> None:
        return None


class LocalRunStateStore:
    """Durable run-state persistence versioned via the shared migration runner."""

    def __init__(self, path: str | Path):
        db_path = str(path) if str(path) == ":memory:" else str(Path(path).expanduser())
        if db_path != ":memory:":
            Path(db_path).resolve().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            migrate(self._db, RUN_STATE_MIGRATIONS, label="workflow_runs")

    def save(self, state: RunState) -> None:
        if state.schema_version != ORCHESTRATOR_SCHEMA_VERSION:
            raise ValueError(
                f"cannot store run schema {state.schema_version}; "
                f"expected {ORCHESTRATOR_SCHEMA_VERSION}"
            )
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO workflow_runs (
                       run_id, schema_version, phase, paused, payload_json, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(run_id) DO UPDATE SET
                       schema_version = excluded.schema_version,
                       phase = excluded.phase,
                       paused = excluded.paused,
                       payload_json = excluded.payload_json,
                       updated_at = excluded.updated_at""",
                (
                    state.run_id,
                    state.schema_version,
                    state.phase.value,
                    1 if state.is_paused else 0,
                    state.model_dump_json(),
                    state.updated_at.isoformat(),
                ),
            )

    def get(self, run_id: str) -> RunState | None:
        with self._lock:
            row = self._db.execute(
                "SELECT payload_json FROM workflow_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return RunState.model_validate_json(row["payload_json"]) if row else None

    def list(self, *, limit: int = 100) -> list[RunState]:
        with self._lock:
            rows = self._db.execute(
                "SELECT payload_json FROM workflow_runs ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [RunState.model_validate_json(row["payload_json"]) for row in rows]

    def delete(self, run_id: str) -> bool:
        with self._lock, self._db:
            cursor = self._db.execute(
                "DELETE FROM workflow_runs WHERE run_id = ?", (run_id,)
            )
        return cursor.rowcount > 0

    def close(self) -> None:
        with self._lock:
            self._db.close()
