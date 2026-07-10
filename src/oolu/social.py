"""People talking to people — and one assistant thread across devices.

Two durable stores, both over the host's one durable connection:

- :class:`DirectMessageStore` — person-to-person messages between accounts
  on the same host, with delivery order and read state. A conversation is
  not a table row; it is the set of messages between two principals,
  folded into a peer list with unread counts on read.
- :class:`AssistantHistoryStore` — the OoLu conversation itself, per
  account. The chat used to live only in one browser's localStorage;
  moving it here is what lets a desktop, a browser, and a phone see the
  SAME thread. Ephemeral nudges (idle reminders) stay client-side by
  design — this store holds the conversation, not the presence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Callable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

# A message is a message, not a document: the file drawer handles those
# (a forwarded file rides as a file_id reference next to a short note).
MAX_MESSAGE_CHARS = 20_000
# The assistant thread is capped per account — old turns fall off the
# back, exactly like a messenger. Enough to scroll, never unbounded.
ASSISTANT_HISTORY_KEEP = 500


class DirectMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    message_id: str
    tenant_id: str
    sender: str
    recipient: str
    body: str
    file_id: str | None = None
    sent_at: datetime
    read_at: datetime | None = None


_DM_SCHEMA = """CREATE TABLE IF NOT EXISTS dm_messages (
    message_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    sender TEXT NOT NULL,
    recipient TEXT NOT NULL,
    body TEXT NOT NULL,
    file_id TEXT,
    sent_at TEXT NOT NULL,
    read_at TEXT
)"""


class DirectMessageStore:
    """Messages between two accounts on the same host, tenant-scoped."""

    def __init__(self, conn, *, clock: Callable[[], datetime] | None = None) -> None:
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_DM_SCHEMA)

    def send(
        self,
        *,
        tenant: str,
        sender: str,
        recipient: str,
        body: str,
        file_id: str | None = None,
    ) -> DirectMessage:
        body = str(body or "").strip()
        if not body:
            raise ValueError("a message needs words")
        if len(body) > MAX_MESSAGE_CHARS:
            raise ValueError("that message is too long — send it as a file")
        if sender == recipient:
            raise ValueError("that's you — notes to self live in Files")
        message = DirectMessage(
            message_id=uuid4().hex,
            tenant_id=tenant,
            sender=sender,
            recipient=recipient,
            body=body,
            file_id=file_id,
            sent_at=self._clock(),
        )
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO dm_messages
                     (message_id, tenant_id, sender, recipient, body,
                      file_id, sent_at, read_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, NULL)""",
                (
                    message.message_id,
                    tenant,
                    sender,
                    recipient,
                    body,
                    file_id,
                    message.sent_at.isoformat(),
                ),
            )
        return message

    def between(
        self, *, tenant: str, me: str, peer: str, limit: int = 200
    ) -> list[DirectMessage]:
        """The conversation, oldest first — the last `limit` messages."""
        with self._conn.lock:
            rows = self._conn.db.execute(
                """SELECT * FROM dm_messages
                   WHERE tenant_id = ?
                     AND ((sender = ? AND recipient = ?)
                       OR (sender = ? AND recipient = ?))
                   ORDER BY sent_at DESC, message_id DESC LIMIT ?""",
                (tenant, me, peer, peer, me, int(limit)),
            ).fetchall()
        return [self._row(row) for row in reversed(rows)]

    def mark_read(self, *, tenant: str, reader: str, peer: str) -> int:
        """Opening the thread reads it: every unread message FROM the peer
        gets its read_at. Returns how many were marked."""
        with self._conn.transaction() as db:
            cursor = db.execute(
                """UPDATE dm_messages SET read_at = ?
                   WHERE tenant_id = ? AND recipient = ? AND sender = ?
                     AND read_at IS NULL""",
                (self._clock().isoformat(), tenant, reader, peer),
            )
        return int(getattr(cursor, "rowcount", 0) or 0)

    def conversations(self, *, tenant: str, principal: str) -> list[dict]:
        """The peer list: everyone this account has messages with, newest
        conversation first, with the last message and the unread count.
        Folded in Python — a person's peer list is human-sized."""
        with self._conn.lock:
            rows = self._conn.db.execute(
                """SELECT * FROM dm_messages
                   WHERE tenant_id = ? AND (sender = ? OR recipient = ?)
                   ORDER BY sent_at ASC, message_id ASC""",
                (tenant, principal, principal),
            ).fetchall()
        peers: dict[str, dict] = {}
        for row in rows:
            message = self._row(row)
            peer = (
                message.recipient
                if message.sender == principal
                else message.sender
            )
            entry = peers.setdefault(
                peer, {"peer": peer, "unread": 0}
            )
            entry["last_text"] = message.body
            entry["last_from"] = message.sender
            entry["last_at"] = message.sent_at.isoformat()
            if message.recipient == principal and message.read_at is None:
                entry["unread"] += 1
        return sorted(peers.values(), key=lambda e: e["last_at"], reverse=True)

    def unread_total(self, *, tenant: str, principal: str) -> int:
        with self._conn.lock:
            row = self._conn.db.execute(
                """SELECT COUNT(*) AS n FROM dm_messages
                   WHERE tenant_id = ? AND recipient = ? AND read_at IS NULL""",
                (tenant, principal),
            ).fetchone()
        return int(row["n"] if row else 0)

    @staticmethod
    def _row(row) -> DirectMessage:
        return DirectMessage(
            message_id=row["message_id"],
            tenant_id=row["tenant_id"],
            sender=row["sender"],
            recipient=row["recipient"],
            body=row["body"],
            file_id=row["file_id"],
            sent_at=datetime.fromisoformat(row["sent_at"]),
            read_at=(
                datetime.fromisoformat(row["read_at"]) if row["read_at"] else None
            ),
        )


_TURNS_SCHEMA = """CREATE TABLE IF NOT EXISTS assistant_turns (
    tenant_id TEXT NOT NULL,
    principal TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    body TEXT NOT NULL,
    at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, principal, seq)
)"""


class AssistantHistoryStore:
    """The OoLu conversation per account: user turns, assistant turns, and
    run markers (kind="run", body=run_id), in strict order. Explicit seq —
    portable across SQLite and Postgres, no autoincrement dialects."""

    def __init__(self, conn, *, clock: Callable[[], datetime] | None = None) -> None:
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_TURNS_SCHEMA)

    def append(
        self, *, tenant: str, principal: str, kind: str, body: str
    ) -> int:
        if kind not in ("user", "assistant", "run"):
            raise ValueError(f"unknown turn kind: {kind}")
        with self._conn.transaction() as db:
            row = db.execute(
                "SELECT COALESCE(MAX(seq), 0) AS top FROM assistant_turns"
                " WHERE tenant_id = ? AND principal = ?",
                (tenant, principal),
            ).fetchone()
            seq = int(row["top"]) + 1
            db.execute(
                """INSERT INTO assistant_turns
                     (tenant_id, principal, seq, kind, body, at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    tenant,
                    principal,
                    seq,
                    kind,
                    str(body),
                    self._clock().isoformat(),
                ),
            )
            # The messenger cap: old turns fall off the back.
            db.execute(
                "DELETE FROM assistant_turns"
                " WHERE tenant_id = ? AND principal = ? AND seq <= ?",
                (tenant, principal, seq - ASSISTANT_HISTORY_KEEP),
            )
        return seq

    def history(
        self, *, tenant: str, principal: str, limit: int = 200
    ) -> list[dict]:
        """The last `limit` turns, oldest first."""
        with self._conn.lock:
            rows = self._conn.db.execute(
                """SELECT seq, kind, body, at FROM assistant_turns
                   WHERE tenant_id = ? AND principal = ?
                   ORDER BY seq DESC LIMIT ?""",
                (tenant, principal, int(limit)),
            ).fetchall()
        return [
            {
                "seq": row["seq"],
                "kind": row["kind"],
                "body": row["body"],
                "at": row["at"],
            }
            for row in reversed(rows)
        ]
