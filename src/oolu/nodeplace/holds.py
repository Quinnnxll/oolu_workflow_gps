"""Held reserved contracts, durably — approvals survive restarts, anywhere.

A reserved contract waiting for an authorized decision is a commitment the
submitter made ("I want this to run, pending approval"), so it lives in the
same durable database as the runs themselves, not in process memory. Both
surfaces share this store: the desktop shell (single user) and the gateway
(multi-tenant — records carry the submitting tenant/principal, and listing
filters by tenant so holds never leak across tenants). The record stores the
contract *as posted* plus the budget knobs captured at submission time; the
compiled blueprint is deliberately NOT persisted — script bodies mint fresh
action ids per compile, so whichever process decides the hold recompiles
once and executes exactly what it inspected.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

_SCHEMA = """CREATE TABLE IF NOT EXISTS pending_contracts (
    pending_id TEXT PRIMARY KEY,
    consumer_tenant TEXT,
    payload_json TEXT NOT NULL
)"""

# The conversation attached to a hold: instead of (or before) deciding,
# the human in control can type an answer back to whoever submitted it.
_REPLIES_SCHEMA = """CREATE TABLE IF NOT EXISTS pending_replies (
    pending_id TEXT NOT NULL,
    author TEXT NOT NULL,
    message TEXT NOT NULL,
    at TEXT NOT NULL
)"""


class PendingContractRecord(BaseModel):
    """One held contract: what to run, and the terms it was confirmed on."""

    model_config = ConfigDict(frozen=True)

    pending_id: str
    contract: dict[str, Any]  # the validated NodeContract, as JSON
    reserved: list[str] = Field(default_factory=list)  # why it is held
    # Who submitted it — the run binds to THEM on approval, never to the
    # approver. None on single-user surfaces (the desktop shell).
    consumer_tenant: str | None = None
    consumer_principal: str | None = None
    budget_cap: float | None = None
    review_threshold: float | None = None
    review_acknowledged: bool = False
    created_at: datetime
    # The promise made at submission: decidable until then, gone after.
    # None = the hold never expires (single-user shells may choose this).
    expires_at: datetime | None = None


class PendingContractStore:
    """SQLite-backed holds over the shell's own durable connection."""

    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)
            db.execute(_REPLIES_SCHEMA)

    def add_reply(
        self, pending_id: str, *, author: str, message: str, at: datetime
    ) -> None:
        with self._conn.transaction() as db:
            db.execute(
                "INSERT INTO pending_replies (pending_id, author, message, at)"
                " VALUES (?, ?, ?, ?)",
                (pending_id, author, message, at.isoformat()),
            )

    def replies(self, pending_id: str) -> list[dict]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT author, message, at FROM pending_replies"
                " WHERE pending_id = ? ORDER BY at",
                (pending_id,),
            ).fetchall()
        return [
            {"author": r["author"], "message": r["message"], "at": r["at"]}
            for r in rows
        ]

    def add(self, record: PendingContractRecord) -> bool:
        with self._conn.transaction() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO pending_contracts"
                " (pending_id, consumer_tenant, payload_json) VALUES (?, ?, ?)",
                (record.pending_id, record.consumer_tenant, record.model_dump_json()),
            )
            return cursor.rowcount > 0

    def get(self, pending_id: str) -> PendingContractRecord | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json FROM pending_contracts WHERE pending_id = ?",
                (pending_id,),
            ).fetchone()
        if row is None:
            return None
        return PendingContractRecord.model_validate_json(row["payload_json"])

    def list(self, *, tenant: str | None = None) -> list[PendingContractRecord]:
        """All holds, or one tenant's — surfaces must scope what they show.

        Ordered oldest-first by the record's own clock, never by SQLite's
        implicit row-order column: PostgreSQL doesn't have it, and asking
        for it there is an UndefinedColumn crash."""
        with self._conn.lock:
            if tenant is None:
                rows = self._conn.db.execute(
                    "SELECT payload_json FROM pending_contracts"
                ).fetchall()
            else:
                rows = self._conn.db.execute(
                    "SELECT payload_json FROM pending_contracts"
                    " WHERE consumer_tenant = ?",
                    (tenant,),
                ).fetchall()
        records = [
            PendingContractRecord.model_validate_json(row["payload_json"])
            for row in rows
        ]
        records.sort(key=lambda r: (r.created_at.isoformat(), r.pending_id))
        return records

    def sweep_expired(self, now: datetime) -> list[PendingContractRecord]:
        """Remove and return every hold past its ``expires_at``.

        Expiry is lazy — surfaces sweep whenever they list or decide, so a
        stale hold can never rot in the queue or be released long after the
        submitter's intent went cold. Auditing the sweep is the surface's
        job (each has its own audit log).
        """
        expired = [
            record
            for record in self.list()
            if record.expires_at is not None and record.expires_at <= now
        ]
        for record in expired:
            self.remove(record.pending_id)
        return expired

    def remove(self, pending_id: str) -> bool:
        with self._conn.transaction() as db:
            cursor = db.execute(
                "DELETE FROM pending_contracts WHERE pending_id = ?",
                (pending_id,),
            )
            return cursor.rowcount > 0
