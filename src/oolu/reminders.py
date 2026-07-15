"""Reminders: the missing route for "remind me" — durable, deterministic.

A reminder is not a workflow to plan: it is a row with a clock. This module
is the whole capability — a durable store the chat creates rows in (the
confirmation always reads the STORED row back, never the request), and a
``due`` read the client polls so a ripe reminder surfaces as OoLu's own
message in the conversation. Nothing here runs on a timer by itself; the
client's poll is the tick, same as every other nudge in the product.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Callable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

# A reminder is one sentence, not a document.
MAX_REMINDER_CHARS = 500

# The farthest a reminder may be set: a year. Past that, it's a plan.
MAX_AHEAD_S = 366 * 24 * 3600


class Reminder(BaseModel):
    model_config = ConfigDict(frozen=True)

    reminder_id: str
    tenant: str
    principal: str
    text: str
    due_at: datetime
    created_at: datetime
    delivered_at: datetime | None = None


_SCHEMA = """CREATE TABLE IF NOT EXISTS reminders (
    reminder_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    principal TEXT NOT NULL,
    text TEXT NOT NULL,
    due_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivered_at TEXT
)"""


class ReminderStore:
    """SQLite-backed reminders over the shell's own durable connection."""

    def __init__(self, conn, *, clock: Callable[[], datetime] | None = None):
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def add(
        self, *, tenant: str, principal: str, text: str, due_at: datetime
    ) -> Reminder:
        text = " ".join(str(text or "").split())
        if not text:
            raise ValueError("a reminder needs words")
        if len(text) > MAX_REMINDER_CHARS:
            raise ValueError("a reminder is one sentence, not a document")
        now = self._clock()
        if due_at.tzinfo is None:
            raise ValueError("a reminder's time must carry a timezone")
        if due_at <= now:
            raise ValueError("that time has already passed")
        if (due_at - now).total_seconds() > MAX_AHEAD_S:
            raise ValueError("reminders reach at most a year ahead")
        reminder = Reminder(
            reminder_id=uuid4().hex,
            tenant=tenant,
            principal=principal,
            text=text,
            due_at=due_at,
            created_at=now,
        )
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO reminders
                       (reminder_id, tenant_id, principal, text, due_at,
                        created_at, delivered_at)
                   VALUES (?, ?, ?, ?, ?, ?, NULL)""",
                (
                    reminder.reminder_id,
                    tenant,
                    principal,
                    reminder.text,
                    reminder.due_at.isoformat(),
                    reminder.created_at.isoformat(),
                ),
            )
        return reminder

    def get(
        self, reminder_id: str, *, tenant: str, principal: str
    ) -> Reminder | None:
        """The caller's own reminder — a stranger's is indistinguishable
        from missing."""
        with self._conn.lock:
            row = self._conn.db.execute(
                """SELECT * FROM reminders WHERE reminder_id = ?
                     AND tenant_id = ? AND principal = ?""",
                (reminder_id, tenant, principal),
            ).fetchone()
        return self._row(row) if row is not None else None

    def due(
        self, *, tenant: str, principal: str, now: datetime | None = None
    ) -> list[Reminder]:
        """Ripe and undelivered, oldest first — what the client surfaces
        as OoLu's own message the moment it polls."""
        now = now or self._clock()
        return [
            r
            for r in self._all(tenant, principal)
            if r.delivered_at is None and r.due_at <= now
        ]

    def upcoming(
        self,
        *,
        tenant: str,
        principal: str,
        now: datetime | None = None,
        limit: int = 50,
    ) -> list[Reminder]:
        """Still ahead, soonest first — the user's forward list."""
        now = now or self._clock()
        ahead = [
            r
            for r in self._all(tenant, principal)
            if r.delivered_at is None and r.due_at > now
        ]
        return ahead[:limit]

    def mark_delivered(
        self, reminder_id: str, *, tenant: str, principal: str
    ) -> Reminder | None:
        """Delivered exactly once: the first marker wins, a second call
        returns None — a reminder never fires twice."""
        with self._conn.transaction() as db:
            cursor = db.execute(
                """UPDATE reminders SET delivered_at = ?
                   WHERE reminder_id = ? AND tenant_id = ? AND principal = ?
                     AND delivered_at IS NULL""",
                (
                    self._clock().isoformat(),
                    reminder_id,
                    tenant,
                    principal,
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) == 0:
                return None
        return self.get(reminder_id, tenant=tenant, principal=principal)

    def erase(self, *, tenant: str, principal: str) -> int:
        """Data-subject erasure: the account's reminders, gone."""
        with self._conn.transaction() as db:
            cursor = db.execute(
                "DELETE FROM reminders WHERE tenant_id = ? AND principal = ?",
                (tenant, principal),
            )
        return int(getattr(cursor, "rowcount", 0) or 0)

    # ------------------------------------------------------------------ #
    def _all(self, tenant: str, principal: str) -> list[Reminder]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                """SELECT * FROM reminders
                   WHERE tenant_id = ? AND principal = ?
                   ORDER BY due_at ASC, reminder_id ASC""",
                (tenant, principal),
            ).fetchall()
        return [self._row(row) for row in rows]

    @staticmethod
    def _row(row) -> Reminder:
        return Reminder(
            reminder_id=row["reminder_id"],
            tenant=row["tenant_id"],
            principal=row["principal"],
            text=row["text"],
            due_at=datetime.fromisoformat(row["due_at"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            delivered_at=(
                datetime.fromisoformat(row["delivered_at"])
                if row["delivered_at"] is not None
                else None
            ),
        )
