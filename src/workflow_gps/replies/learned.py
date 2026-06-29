"""Local learned replies and short-lived inbound/outbound pairing state."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..persistence import Migration, migrate
from .engine import normalize_message
from .models import MessageEnvelope


def _scope(message: MessageEnvelope) -> str:
    return str(message.metadata.get("reply_scope") or message.channel)


def _create_learned_reply_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS learned_replies (
            scope TEXT NOT NULL,
            normalized_prompt TEXT NOT NULL,
            prompt_text TEXT NOT NULL,
            reply_text TEXT NOT NULL,
            learned_count INTEGER NOT NULL DEFAULT 1,
            use_count INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (scope, normalized_prompt)
        );
        CREATE TABLE IF NOT EXISTS pending_messages (
            scope TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            normalized_prompt TEXT NOT NULL,
            prompt_text TEXT NOT NULL,
            received_at REAL NOT NULL,
            PRIMARY KEY (scope, conversation_id)
        );
        """
    )


def _drop_learned_reply_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        "DROP TABLE IF EXISTS pending_messages; DROP TABLE IF EXISTS learned_replies;"
    )


# Ordered schema history for local learned replies. Append-only.
MIGRATIONS: tuple[Migration, ...] = (
    Migration(up=_create_learned_reply_tables, down=_drop_learned_reply_tables),
)


@runtime_checkable
class LearnedReplyStore(Protocol):
    def lookup(self, message: MessageEnvelope) -> str | None: ...

    def remember_unanswered(self, message: MessageEnvelope) -> None: ...

    def learn_from_outbound(self, message: MessageEnvelope) -> bool: ...

    def close(self) -> None: ...


class NoopLearnedReplyStore:
    def lookup(self, message: MessageEnvelope) -> str | None:
        return None

    def remember_unanswered(self, message: MessageEnvelope) -> None:
        return None

    def learn_from_outbound(self, message: MessageEnvelope) -> bool:
        return False

    def close(self) -> None:
        return None


class LocalLearnedReplyStore:
    """SQLite memory scoped by account/channel, never shared remotely."""

    def __init__(self, path: str | Path, *, max_pair_age_s: int = 600):
        db_path = str(path) if str(path) == ":memory:" else str(Path(path).expanduser())
        if db_path != ":memory:":
            Path(db_path).resolve().parent.mkdir(parents=True, exist_ok=True)
        self._max_pair_age_s = max_pair_age_s
        self._lock = threading.RLock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            migrate(self._db, MIGRATIONS, label="learned_replies")

    def lookup(self, message: MessageEnvelope) -> str | None:
        prompt = normalize_message(message.text)
        if not prompt:
            return None
        with self._lock, self._db:
            row = self._db.execute(
                """SELECT reply_text FROM learned_replies
                   WHERE scope = ? AND normalized_prompt = ?""",
                (_scope(message), prompt),
            ).fetchone()
            if row is not None:
                self._db.execute(
                    """UPDATE learned_replies SET use_count = use_count + 1, updated_at = ?
                       WHERE scope = ? AND normalized_prompt = ?""",
                    (time.time(), _scope(message), prompt),
                )
        return str(row["reply_text"]) if row is not None else None

    def remember_unanswered(self, message: MessageEnvelope) -> None:
        prompt = normalize_message(message.text)
        if not prompt:
            return
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO pending_messages (
                       scope, conversation_id, normalized_prompt, prompt_text, received_at
                   ) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(scope, conversation_id) DO UPDATE SET
                       normalized_prompt = excluded.normalized_prompt,
                       prompt_text = excluded.prompt_text,
                       received_at = excluded.received_at""",
                (
                    _scope(message),
                    message.conversation_id,
                    prompt,
                    message.text,
                    time.time(),
                ),
            )

    def learn_from_outbound(self, message: MessageEnvelope) -> bool:
        scope = _scope(message)
        reply_to_text = message.metadata.get("reply_to_text")
        now = time.time()
        with self._lock, self._db:
            if reply_to_text:
                prompt_text = str(reply_to_text)
                prompt = normalize_message(prompt_text)
            else:
                row = self._db.execute(
                    """SELECT normalized_prompt, prompt_text, received_at
                       FROM pending_messages WHERE scope = ? AND conversation_id = ?""",
                    (scope, message.conversation_id),
                ).fetchone()
                if row is None or now - row["received_at"] > self._max_pair_age_s:
                    return False
                prompt = row["normalized_prompt"]
                prompt_text = row["prompt_text"]
            if not prompt or not message.text.strip():
                return False
            self._store(scope, prompt, prompt_text, message.text.strip(), now)
            self._db.execute(
                "DELETE FROM pending_messages WHERE scope = ? AND conversation_id = ?",
                (scope, message.conversation_id),
            )
        return True

    def teach(self, *, scope: str, prompt: str, reply: str) -> None:
        normalized = normalize_message(prompt)
        if not scope.strip() or not normalized or not reply.strip():
            raise ValueError("scope, prompt, and reply must not be empty")
        now = time.time()
        with self._lock, self._db:
            self._store(scope.strip(), normalized, prompt.strip(), reply.strip(), now)

    def _store(
        self,
        scope: str,
        normalized_prompt: str,
        prompt_text: str,
        reply_text: str,
        now: float,
    ) -> None:
        self._db.execute(
            """INSERT INTO learned_replies (
                   scope, normalized_prompt, prompt_text, reply_text,
                   learned_count, use_count, created_at, updated_at
               ) VALUES (?, ?, ?, ?, 1, 0, ?, ?)
               ON CONFLICT(scope, normalized_prompt) DO UPDATE SET
                   prompt_text = excluded.prompt_text,
                   reply_text = excluded.reply_text,
                   learned_count = learned_replies.learned_count + 1,
                   updated_at = excluded.updated_at""",
            (scope, normalized_prompt, prompt_text, reply_text, now, now),
        )

    def close(self) -> None:
        with self._lock:
            self._db.close()
