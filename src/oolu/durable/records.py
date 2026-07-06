"""Durable stores for run-state checkpoints and the domain records.

These persist the records a workflow produces — its run-state checkpoint, the
chosen route, accounts, approvals, incidents, semantic evidence, and execution
outcomes — so the complete execution history can be reconstructed from storage
alone after a restart. Records reuse the existing domain models (no parallel
vocabulary). The same port is what a PostgreSQL adapter implements in production.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from ..orchestrator.state import (
    ExecutionRecord,
    Incident,
    RoutePlan,
    RunState,
    SemanticGrounding,
)
from ..skills.models import ApprovalRecord
from .connection import DurableConnection


class DurableRunStateStore:
    """Durable, versioned run-state checkpoints (one row per run)."""

    def __init__(self, conn: DurableConnection):
        self._conn = conn

    def save(self, state: RunState) -> None:
        with self._conn.transaction() as db:
            self.stage(db, state)

    def stage(self, db, state: RunState) -> None:
        """Checkpoint the run on an already-open transaction connection."""
        db.execute(
            """INSERT INTO workflow_runs
                   (run_id, schema_version, phase, paused, payload_json, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
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
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json FROM workflow_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return RunState.model_validate_json(row["payload_json"]) if row else None

    def list(self, *, limit: int = 100) -> list[RunState]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT payload_json FROM workflow_runs ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [RunState.model_validate_json(row["payload_json"]) for row in rows]

    def delete(self, run_id: str) -> bool:
        with self._conn.transaction() as db:
            cursor = db.execute("DELETE FROM workflow_runs WHERE run_id = ?", (run_id,))
        return cursor.rowcount > 0


class DurableRecordStore:
    """Durable stores for routes, accounts, approvals, incidents, evidence, outcomes."""

    def __init__(self, conn: DurableConnection):
        self._conn = conn

    # --- routes ----------------------------------------------------------- #
    def save_route(self, run_id: str, route: RoutePlan) -> None:
        with self._conn.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO routes (route_id, run_id, payload_json, created_at)"
                " VALUES (?, ?, ?, ?)",
                (
                    route.chosen.id,
                    run_id,
                    route.model_dump_json(),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def list_routes(self, run_id: str) -> list[RoutePlan]:
        rows = self._select("routes", run_id)
        return [RoutePlan.model_validate_json(row["payload_json"]) for row in rows]

    # --- accounts --------------------------------------------------------- #
    def save_account(self, account_id: str, payload: dict[str, Any]) -> None:
        now = datetime.now(UTC).isoformat()
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO accounts (account_id, payload_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(account_id) DO UPDATE SET
                       payload_json = excluded.payload_json,
                       updated_at = excluded.updated_at""",
                (account_id, json.dumps(payload), now, now),
            )

    def get_account(self, account_id: str) -> dict[str, Any] | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json FROM accounts WHERE account_id = ?", (account_id,)
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    # --- approvals -------------------------------------------------------- #
    def save_approval(self, run_id: str, approval: ApprovalRecord) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT OR IGNORE INTO approvals
                   (approval_id, run_id, principal, policy, decision, payload_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    approval.id,
                    run_id,
                    approval.principal,
                    approval.policy,
                    approval.decision,
                    approval.model_dump_json(),
                    approval.created_at.isoformat(),
                ),
            )

    def list_approvals(self, run_id: str) -> list[ApprovalRecord]:
        rows = self._select("approvals", run_id)
        return [ApprovalRecord.model_validate_json(row["payload_json"]) for row in rows]

    # --- incidents -------------------------------------------------------- #
    def save_incident(self, run_id: str, incident: Incident) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO incidents
                   (incident_id, run_id, reason, severity, resolution, payload_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(incident_id) DO UPDATE SET
                       resolution = excluded.resolution,
                       payload_json = excluded.payload_json""",
                (
                    incident.id,
                    run_id,
                    incident.reason,
                    incident.severity,
                    incident.resolution,
                    incident.model_dump_json(),
                    incident.at.isoformat(),
                ),
            )

    def list_incidents(self, run_id: str) -> list[Incident]:
        rows = self._select("incidents", run_id)
        return [Incident.model_validate_json(row["payload_json"]) for row in rows]

    # --- semantic evidence ----------------------------------------------- #
    def save_semantic_evidence(self, run_id: str, grounding: SemanticGrounding) -> None:
        with self._conn.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO semantic_evidence"
                " (evidence_id, run_id, payload_json, created_at)"
                " VALUES (?, ?, ?, ?)",
                (
                    f"{run_id}:grounding",
                    run_id,
                    grounding.model_dump_json(),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def list_semantic_evidence(self, run_id: str) -> list[SemanticGrounding]:
        rows = self._select("semantic_evidence", run_id)
        return [
            SemanticGrounding.model_validate_json(row["payload_json"]) for row in rows
        ]

    # --- execution outcomes ---------------------------------------------- #
    def save_execution_outcome(self, run_id: str, execution: ExecutionRecord) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT OR IGNORE INTO execution_outcomes
                   (run_id, idempotency_key, status, payload_json, completed_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    run_id,
                    execution.idempotency_key,
                    execution.status.value,
                    execution.model_dump_json(),
                    execution.completed_at.isoformat()
                    if execution.completed_at
                    else None,
                ),
            )

    def list_execution_outcomes(self, run_id: str) -> list[ExecutionRecord]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT payload_json FROM execution_outcomes WHERE run_id = ?"
                " ORDER BY completed_at ASC",
                (run_id,),
            ).fetchall()
        return [
            ExecutionRecord.model_validate_json(row["payload_json"]) for row in rows
        ]

    # --------------------------------------------------------------------- #
    def _select(self, table: str, run_id: str):
        with self._conn.lock:
            return self._conn.db.execute(
                f"SELECT payload_json FROM {table} WHERE run_id = ? ORDER BY created_at ASC",
                (run_id,),
            ).fetchall()
