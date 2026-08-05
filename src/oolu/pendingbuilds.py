"""Builds paused on their key (V2): written, verified, waiting to publish.

A function that targets an authenticated API is held HERE after the whole
birth gate passed — the script, its declared interface, and the walked
transaction states — instead of publishing a node whose first run can
only fail. The secret-form door completes the build: the key lands in
the vault, the node publishes, the credential binds. A pending build is
consumed exactly once; abandoning it costs nothing (no node stood).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Callable
from uuid import uuid4

_SCHEMA = """CREATE TABLE IF NOT EXISTS pending_builds (
    build_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    principal TEXT NOT NULL,
    agent TEXT NOT NULL DEFAULT 'oolu',
    goal TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    script TEXT NOT NULL,
    io_json TEXT NOT NULL,
    states_json TEXT NOT NULL DEFAULT '[]',
    model TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
)"""


class PendingBuildStore:
    def __init__(self, conn, *, clock: Callable[[], datetime] | None = None):
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def put(
        self,
        *,
        tenant: str,
        principal: str,
        agent: str,
        goal: str,
        skill_id: str,
        script: str,
        io: dict,
        states: list[str],
        model: str = "",
    ) -> str:
        build_id = uuid4().hex
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO pending_builds
                     (build_id, tenant_id, principal, agent, goal, skill_id,
                      script, io_json, states_json, model, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    build_id,
                    tenant,
                    principal,
                    str(agent or "oolu"),
                    goal,
                    skill_id,
                    script,
                    json.dumps(io or {}),
                    json.dumps(list(states or [])),
                    str(model or ""),
                    self._clock().isoformat(),
                ),
            )
        return build_id

    def get(self, build_id: str, *, tenant: str) -> dict | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT * FROM pending_builds"
                " WHERE build_id = ? AND tenant_id = ?",
                (str(build_id), tenant),
            ).fetchone()
        return self._row(row) if row else None

    def pop(self, build_id: str, *, tenant: str) -> dict | None:
        """Consume the pending build — exactly once; the completion door
        publishes from what it takes."""
        with self._conn.transaction() as db:
            row = db.execute(
                "SELECT * FROM pending_builds"
                " WHERE build_id = ? AND tenant_id = ?",
                (str(build_id), tenant),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "DELETE FROM pending_builds WHERE build_id = ?",
                (str(build_id),),
            )
        return self._row(row)

    @staticmethod
    def _row(row) -> dict:
        def _loads(text, fallback):
            try:
                parsed = json.loads(text or "")
            except Exception:  # noqa: BLE001 - a torn row reads as empty
                return fallback
            return parsed if isinstance(parsed, type(fallback)) else fallback

        return {
            "build_id": row["build_id"],
            "tenant_id": row["tenant_id"],
            "principal": row["principal"],
            "agent": row["agent"],
            "goal": row["goal"],
            "skill_id": row["skill_id"],
            "script": row["script"],
            "io": _loads(row["io_json"], {}),
            "states": _loads(row["states_json"], []),
            "model": row["model"],
            "created_at": row["created_at"],
        }
