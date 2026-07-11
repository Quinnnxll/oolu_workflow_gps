"""A durable, hash-linked, append-only audit log.

Each entry stores the hash of the previous entry, and its own hash covers that
link plus the entry's content. The chain is therefore tamper-evident: changing or
removing any entry breaks every hash after it, which ``verify()`` detects. The log
implements the skill core's ``EventSink`` protocol, so it is a drop-in durable
replacement for the in-memory event sink the orchestrator uses.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from .connection import DurableConnection

_GENESIS = "0" * 64


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _entry_hash(
    *, entry_id: str, prev_hash: str, event_type: str, payload: dict[str, Any], at: str
) -> str:
    material = "|".join([entry_id, prev_hash, event_type, _canonical(payload), at])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class AuditRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    seq: int
    entry_id: str
    run_id: str | None
    event_type: str
    payload: dict[str, Any]
    prev_hash: str
    hash: str
    at: datetime


class DurableAuditLog:
    """Append-only hash chain. ``append`` matches the ``EventSink`` signature."""

    def __init__(self, conn: DurableConnection):
        self._conn = conn

    def append(self, event_type: str, payload: dict[str, Any]) -> AuditRecord:
        entry_id = uuid4().hex
        at = datetime.now(UTC)
        run_id = payload.get("run_id")
        with self._conn.transaction() as db:
            row = db.execute(
                "SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            prev_hash = row["hash"] if row else _GENESIS
            digest = _entry_hash(
                entry_id=entry_id,
                prev_hash=prev_hash,
                event_type=event_type,
                payload=payload,
                at=at.isoformat(),
            )
            cursor = db.execute(
                """INSERT INTO audit_log
                   (entry_id, run_id, event_type, payload_json, prev_hash, hash, at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry_id,
                    run_id,
                    event_type,
                    json.dumps(payload),
                    prev_hash,
                    digest,
                    at.isoformat(),
                ),
            )
            assert cursor.lastrowid is not None
            seq = int(cursor.lastrowid)
        return AuditRecord(
            seq=seq,
            entry_id=entry_id,
            run_id=run_id,
            event_type=event_type,
            payload=payload,
            prev_hash=prev_hash,
            hash=digest,
            at=at,
        )

    def records(self, *, run_id: str | None = None) -> list[AuditRecord]:
        with self._conn.lock:
            if run_id is None:
                rows = self._conn.db.execute(
                    "SELECT * FROM audit_log ORDER BY seq ASC"
                ).fetchall()
            else:
                rows = self._conn.db.execute(
                    "SELECT * FROM audit_log WHERE run_id = ? ORDER BY seq ASC",
                    (run_id,),
                ).fetchall()
        return [self._row(row) for row in rows]

    def executed_statuses(
        self,
        run_ids: Sequence[str],
        *,
        event_type: str = "workflow.executed",
        chunk: int = 500,
    ) -> list[str]:
        """The ``status`` payload field of every matching record for these
        runs — the stats path's targeted, indexed read. The full-log scan
        this replaces grew with the machine's whole history; this grows
        with the runs actually asked about."""
        ids = list(dict.fromkeys(run_ids))
        statuses: list[str] = []
        with self._conn.lock:
            for start in range(0, len(ids), chunk):
                piece = ids[start : start + chunk]
                marks = ",".join("?" for _ in piece)
                rows = self._conn.db.execute(
                    "SELECT payload_json FROM audit_log"
                    f" WHERE event_type = ? AND run_id IN ({marks})",
                    (event_type, *piece),
                ).fetchall()
                statuses += [
                    json.loads(row["payload_json"]).get("status")
                    for row in rows
                ]
        return statuses

    def verify(self) -> bool:
        """Recompute the chain end to end; ``False`` if any link is broken."""
        prev_hash = _GENESIS
        for record in self.records():
            if record.prev_hash != prev_hash:
                return False
            expected = _entry_hash(
                entry_id=record.entry_id,
                prev_hash=record.prev_hash,
                event_type=record.event_type,
                payload=record.payload,
                at=record.at.isoformat(),
            )
            if expected != record.hash:
                return False
            prev_hash = record.hash
        return True

    @staticmethod
    def _row(row) -> AuditRecord:
        return AuditRecord(
            seq=row["seq"],
            entry_id=row["entry_id"],
            run_id=row["run_id"],
            event_type=row["event_type"],
            payload=json.loads(row["payload_json"]),
            prev_hash=row["prev_hash"],
            hash=row["hash"],
            at=datetime.fromisoformat(row["at"]),
        )
