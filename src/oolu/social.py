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

    def erase_principal(self, *, tenant: str, principal: str) -> int:
        """Data-subject erasure: every message the principal sent OR
        received is gone — the store holds ONE shared row per message, so
        erasing an account removes its threads from the peers' view too
        (said plainly in the account-deletion response). Returns count."""
        with self._conn.transaction() as db:
            cursor = db.execute(
                """DELETE FROM dm_messages
                   WHERE tenant_id = ? AND (sender = ? OR recipient = ?)""",
                (tenant, principal, principal),
            )
        return int(getattr(cursor, "rowcount", 0) or 0)

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

    def erase(self, *, tenant: str, principal: str) -> int:
        """Data-subject erasure: the account's whole thread, gone."""
        with self._conn.transaction() as db:
            cursor = db.execute(
                "DELETE FROM assistant_turns"
                " WHERE tenant_id = ? AND principal = ?",
                (tenant, principal),
            )
        return int(getattr(cursor, "rowcount", 0) or 0)

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


# --------------------------------------------------------------------------- #
# Friendship: requests, acceptance, blocks, and who may message whom.         #
# --------------------------------------------------------------------------- #
# A directed edge per (owner -> peer). "accepted" is written BOTH ways when a
# request is accepted, so friendship reads the same from either side; "blocked"
# and "pending" are one-directional by nature. A per-account preference decides
# whether strangers may message at all.
_FRIENDS_SCHEMA = """CREATE TABLE IF NOT EXISTS friendships (
    tenant_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    peer TEXT NOT NULL,
    status TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, owner, peer)
)"""

_MSG_PREFS_SCHEMA = """CREATE TABLE IF NOT EXISTS message_prefs (
    tenant_id TEXT NOT NULL,
    principal TEXT NOT NULL,
    allow_nonfriend INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (tenant_id, principal)
)"""


class FriendshipError(ValueError):
    """A refused social move — blocked, or nothing to accept."""


class FriendshipStore:
    """Requests, acceptance, blocks, and the stranger-message preference.

    Discovery still happens elsewhere (exact lookup by name or e-mail);
    this is what a found account becomes — a request the other person
    decides, never an unsolicited message that just appears."""

    def __init__(self, conn, *, clock: Callable[[], datetime] | None = None) -> None:
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_FRIENDS_SCHEMA)
            db.execute(_MSG_PREFS_SCHEMA)

    # -- the preference: may strangers message me? --------------------------
    def allow_nonfriend(self, *, tenant: str, principal: str) -> bool:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT allow_nonfriend FROM message_prefs"
                " WHERE tenant_id = ? AND principal = ?",
                (tenant, principal),
            ).fetchone()
        return bool(row["allow_nonfriend"]) if row is not None else True

    def set_allow_nonfriend(
        self, *, tenant: str, principal: str, allow: bool
    ) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO message_prefs (tenant_id, principal, allow_nonfriend)
                   VALUES (?, ?, ?)
                   ON CONFLICT(tenant_id, principal) DO UPDATE SET
                     allow_nonfriend = excluded.allow_nonfriend""",
                (tenant, principal, int(bool(allow))),
            )

    # -- state reads --------------------------------------------------------
    def _status(self, tenant: str, owner: str, peer: str) -> str | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT status FROM friendships"
                " WHERE tenant_id = ? AND owner = ? AND peer = ?",
                (tenant, owner, peer),
            ).fetchone()
        return row["status"] if row else None

    def are_friends(self, *, tenant: str, a: str, b: str) -> bool:
        return self._status(tenant, a, b) == "accepted"

    def is_blocked(self, *, tenant: str, by: str, whom: str) -> bool:
        return self._status(tenant, by, whom) == "blocked"

    def relationship(self, *, tenant: str, me: str, other: str) -> str:
        """How ``me`` stands to ``other``: friends | pending_out |
        pending_in | blocked | blocked_by | none."""
        mine = self._status(tenant, me, other)
        theirs = self._status(tenant, other, me)
        if mine == "accepted":
            return "friends"
        if mine == "blocked":
            return "blocked"
        if theirs == "blocked":
            return "blocked_by"
        if mine == "pending":
            return "pending_out"
        if theirs == "pending":
            return "pending_in"
        return "none"

    def may_message(self, *, tenant: str, sender: str, recipient: str) -> bool:
        """Whether ``sender`` may message ``recipient``: never if blocked;
        otherwise friends always may, strangers only if the recipient
        allows it."""
        if self.is_blocked(tenant=tenant, by=recipient, whom=sender):
            return False
        if self.are_friends(tenant=tenant, a=recipient, b=sender):
            return True
        return self.allow_nonfriend(tenant=tenant, principal=recipient)

    # -- moves --------------------------------------------------------------
    def _set(self, tenant: str, owner: str, peer: str, status: str) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO friendships (tenant_id, owner, peer, status, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(tenant_id, owner, peer) DO UPDATE SET
                     status = excluded.status, updated_at = excluded.updated_at""",
                (tenant, owner, peer, status, self._clock().isoformat()),
            )

    def _clear(self, tenant: str, owner: str, peer: str) -> None:
        with self._conn.transaction() as db:
            db.execute(
                "DELETE FROM friendships"
                " WHERE tenant_id = ? AND owner = ? AND peer = ?",
                (tenant, owner, peer),
            )

    def request(self, *, tenant: str, requester: str, target: str) -> str:
        """Ask to be friends. Returns the resulting relationship. Refuses
        if the target blocked the requester; a no-op if already friends or
        already requested; auto-accepts if the target had already asked
        (the two requests meet)."""
        if requester == target:
            raise FriendshipError("that's you")
        if self.is_blocked(tenant=tenant, by=target, whom=requester):
            raise FriendshipError("you can't send a request to this account")
        if self.is_blocked(tenant=tenant, by=requester, whom=target):
            raise FriendshipError("unblock this account before adding them")
        if self.are_friends(tenant=tenant, a=requester, b=target):
            return "friends"
        if self._status(tenant, target, requester) == "pending":
            # They already asked us: meeting in the middle IS acceptance.
            self._accept_pair(tenant, requester, target)
            return "friends"
        self._set(tenant, requester, target, "pending")
        return "pending_out"

    def _accept_pair(self, tenant: str, a: str, b: str) -> None:
        self._set(tenant, a, b, "accepted")
        self._set(tenant, b, a, "accepted")

    def accept(self, *, tenant: str, me: str, requester: str) -> None:
        """Accept a pending request from ``requester``."""
        if self._status(tenant, requester, me) != "pending":
            raise FriendshipError("there is no request from this account")
        self._accept_pair(tenant, me, requester)

    def decline(self, *, tenant: str, me: str, requester: str) -> None:
        """Decline a pending request — it simply goes away, no block."""
        if self._status(tenant, requester, me) != "pending":
            raise FriendshipError("there is no request from this account")
        self._clear(tenant, requester, me)

    def block(self, *, tenant: str, me: str, other: str) -> None:
        """Block an account: they can't message or request me, and any
        pending request or friendship between us is cleared."""
        self._clear(tenant, other, me)  # their pending/accepted edge to me
        self._clear(tenant, me, other)  # my accepted edge to them
        self._set(tenant, me, other, "blocked")

    def unblock(self, *, tenant: str, me: str, other: str) -> None:
        if self._status(tenant, me, other) == "blocked":
            self._clear(tenant, me, other)

    def incoming(self, *, tenant: str, me: str) -> list[str]:
        """Accounts awaiting my decision — the friend-request inbox."""
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT owner FROM friendships"
                " WHERE tenant_id = ? AND peer = ? AND status = 'pending'"
                " ORDER BY updated_at DESC",
                (tenant, me),
            ).fetchall()
        return [r["owner"] for r in rows]

    def erase_principal(self, *, tenant: str, principal: str) -> int:
        """Data-subject erasure: every edge and preference touching them."""
        with self._conn.transaction() as db:
            c1 = db.execute(
                "DELETE FROM friendships WHERE tenant_id = ?"
                " AND (owner = ? OR peer = ?)",
                (tenant, principal, principal),
            )
            c2 = db.execute(
                "DELETE FROM message_prefs WHERE tenant_id = ? AND principal = ?",
                (tenant, principal),
            )
        return int(getattr(c1, "rowcount", 0) or 0) + int(
            getattr(c2, "rowcount", 0) or 0
        )
