"""Held reserved contracts, durably — approvals survive a shell restart.

A reserved contract waiting for an authorized decision is a commitment the
user made ("I want this to run, pending approval"), so it lives in the same
durable database as the runs themselves, not in process memory. The record
stores the contract *as posted* plus the budget knobs captured at confirm
time; the compiled blueprint is deliberately NOT persisted — script bodies
mint fresh action ids per compile, so whichever process decides the hold
recompiles once and executes exactly what it inspected.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

_SCHEMA = """CREATE TABLE IF NOT EXISTS desktop_pending_contracts (
    pending_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL
)"""


class PendingContractRecord(BaseModel):
    """One held contract: what to run, and the terms it was confirmed on."""

    model_config = ConfigDict(frozen=True)

    pending_id: str
    contract: dict[str, Any]  # the validated NodeContract, as JSON
    reserved: list[str] = Field(default_factory=list)  # why it is held
    budget_cap: float | None = None
    review_threshold: float | None = None
    review_acknowledged: bool = False
    created_at: datetime


class PendingContractStore:
    """SQLite-backed holds over the shell's own durable connection."""

    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def add(self, record: PendingContractRecord) -> bool:
        with self._conn.transaction() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO desktop_pending_contracts"
                " (pending_id, payload_json) VALUES (?, ?)",
                (record.pending_id, record.model_dump_json()),
            )
            return cursor.rowcount > 0

    def get(self, pending_id: str) -> PendingContractRecord | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json FROM desktop_pending_contracts"
                " WHERE pending_id = ?",
                (pending_id,),
            ).fetchone()
        if row is None:
            return None
        return PendingContractRecord.model_validate_json(row["payload_json"])

    def list(self) -> list[PendingContractRecord]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT payload_json FROM desktop_pending_contracts ORDER BY rowid"
            ).fetchall()
        return [
            PendingContractRecord.model_validate_json(row["payload_json"])
            for row in rows
        ]

    def remove(self, pending_id: str) -> bool:
        with self._conn.transaction() as db:
            cursor = db.execute(
                "DELETE FROM desktop_pending_contracts WHERE pending_id = ?",
                (pending_id,),
            )
            return cursor.rowcount > 0
