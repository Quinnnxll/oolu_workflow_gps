"""A transactional outbox for events and notifications.

A state change and the messages it emits are written in the *same* transaction
(``stage`` is called inside the caller's ``DurableConnection.transaction()``), so a
message is never published for a change that rolled back, and a change never
commits without its message queued. A separate relay later delivers pending
messages to a sink and marks them sent — at-least-once delivery, deduplicated by
message id, surviving restarts.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from .connection import DurableConnection


class OutboxStatus(str, Enum):
    PENDING = "pending"
    SENT = "sent"


class OutboxMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    msg_id: str
    topic: str
    payload: dict[str, Any]
    status: OutboxStatus
    attempts: int
    created_at: datetime
    sent_at: datetime | None = None


class TransactionalOutbox:
    def __init__(self, conn: DurableConnection):
        self._conn = conn

    def stage(
        self,
        db: sqlite3.Connection,
        topic: str,
        payload: dict[str, Any],
        *,
        msg_id: str | None = None,
    ) -> str:
        """Stage a message on an *already open* transaction connection."""
        msg_id = msg_id or uuid4().hex
        db.execute(
            "INSERT INTO outbox (msg_id, topic, payload_json, status, attempts, created_at)"
            " VALUES (?, ?, ?, ?, 0, ?)",
            (
                msg_id,
                topic,
                json.dumps(payload),
                OutboxStatus.PENDING.value,
                datetime.now(UTC).isoformat(),
            ),
        )
        return msg_id

    def publish(self, topic: str, payload: dict[str, Any]) -> str:
        """Stage a message in its own transaction (no surrounding state change)."""
        with self._conn.transaction() as db:
            return self.stage(db, topic, payload)

    def pending(self, *, limit: int = 100) -> list[OutboxMessage]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT * FROM outbox WHERE status = ? ORDER BY created_at ASC LIMIT ?",
                (OutboxStatus.PENDING.value, limit),
            ).fetchall()
        return [self._row(row) for row in rows]

    def relay(self, sink: Callable[[OutboxMessage], None], *, limit: int = 100) -> int:
        """Deliver pending messages to ``sink`` and mark them sent.

        A delivery failure leaves the message pending (attempts incremented) so a
        later relay retries it; a successful sink marks it sent exactly once.
        """
        delivered = 0
        for message in self.pending(limit=limit):
            try:
                sink(message)
            except Exception:
                with self._conn.transaction() as db:
                    db.execute(
                        "UPDATE outbox SET attempts = attempts + 1 WHERE msg_id = ?",
                        (message.msg_id,),
                    )
                continue
            with self._conn.transaction() as db:
                db.execute(
                    "UPDATE outbox SET status = ?, sent_at = ?, attempts = attempts + 1"
                    " WHERE msg_id = ? AND status = ?",
                    (
                        OutboxStatus.SENT.value,
                        datetime.now(UTC).isoformat(),
                        message.msg_id,
                        OutboxStatus.PENDING.value,
                    ),
                )
            delivered += 1
        return delivered

    @staticmethod
    def _row(row) -> OutboxMessage:
        return OutboxMessage(
            msg_id=row["msg_id"],
            topic=row["topic"],
            payload=json.loads(row["payload_json"]),
            status=OutboxStatus(row["status"]),
            attempts=row["attempts"],
            created_at=datetime.fromisoformat(row["created_at"]),
            sent_at=(
                datetime.fromisoformat(row["sent_at"]) if row["sent_at"] else None
            ),
        )
