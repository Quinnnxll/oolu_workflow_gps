"""Local representative state: settings, remembered exchanges, drafts.

One SQLite database per install, scoped by account inside — the same shape
as the learned-replies store. ``exchanges`` is the Phase-0 memory (and the
Phase-1 training corpus); ``drafts`` is simultaneously the audit log, the
accept-rate metric source, and the future DPO dataset.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from ..persistence import Migration, migrate
from .models import Draft, GateVerdict


def _create_representative_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS representative_settings (
            scope TEXT PRIMARY KEY,
            mode TEXT NOT NULL DEFAULT 'off',
            persona_about TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS exchanges (
            scope TEXT NOT NULL,
            exchange_key TEXT NOT NULL,
            prompt_text TEXT NOT NULL,
            reply_text TEXT NOT NULL,
            at REAL NOT NULL,
            PRIMARY KEY (scope, exchange_key)
        );
        CREATE TABLE IF NOT EXISTS drafts (
            draft_id TEXT PRIMARY KEY,
            scope TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            inbound_text TEXT NOT NULL,
            generated_text TEXT NOT NULL,
            final_text TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            gate_score REAL NOT NULL DEFAULT 0,
            gate_commitment INTEGER NOT NULL DEFAULT 0,
            gate_auto_ok INTEGER NOT NULL DEFAULT 0,
            gate_reasons TEXT NOT NULL DEFAULT '',
            adapter_version TEXT NOT NULL DEFAULT 'base',
            created_at REAL NOT NULL,
            decided_at REAL
        );
        CREATE INDEX IF NOT EXISTS drafts_by_scope_status
            ON drafts (scope, status, created_at);
        """
    )


def _drop_representative_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        "DROP TABLE IF EXISTS drafts;"
        " DROP TABLE IF EXISTS exchanges;"
        " DROP TABLE IF EXISTS representative_settings;"
    )


def _create_adapter_versions(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS adapter_versions (
            scope TEXT NOT NULL,
            version INTEGER NOT NULL,
            base_model TEXT NOT NULL,
            artifact_ref TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            message_count INTEGER NOT NULL DEFAULT 0,
            holdout_ppl REAL,
            trained_at REAL,
            created_at REAL NOT NULL,
            PRIMARY KEY (scope, version)
        );
        """
    )


def _drop_adapter_versions(conn: sqlite3.Connection) -> None:
    conn.executescript("DROP TABLE IF EXISTS adapter_versions;")


def _add_exchange_peer(conn: sqlite3.Connection) -> None:
    # WHO the user was answering: the register signal. Old rows keep '' —
    # they still teach the average voice, just never a specific register.
    conn.executescript(
        "ALTER TABLE exchanges ADD COLUMN peer TEXT NOT NULL DEFAULT '';"
    )


def _add_info_flow_columns(conn: sqlite3.Connection) -> None:
    # notified_at: when a needs_info draft's questions were surfaced to the
    # user in their OoLu conversation — asked once, never repeated.
    # mode_on_at: the last time the representative was switched ON — a
    # discarded draft only blocks re-drafting until the next switch-on.
    conn.executescript(
        "ALTER TABLE drafts ADD COLUMN notified_at REAL;"
        " ALTER TABLE representative_settings"
        "   ADD COLUMN mode_on_at REAL NOT NULL DEFAULT 0;"
    )


def _create_peer_rules(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS peer_rules (
            scope TEXT NOT NULL,
            peer TEXT NOT NULL,
            auto_allowed INTEGER NOT NULL DEFAULT 1,
            updated_at REAL NOT NULL,
            PRIMARY KEY (scope, peer)
        );
        """
    )


def _drop_peer_rules(conn: sqlite3.Connection) -> None:
    conn.executescript("DROP TABLE IF EXISTS peer_rules;")


# Ordered schema history. Append-only.
MIGRATIONS: tuple[Migration, ...] = (
    Migration(up=_create_representative_tables, down=_drop_representative_tables),
    Migration(up=_create_adapter_versions, down=_drop_adapter_versions),
    Migration(up=_create_peer_rules, down=_drop_peer_rules),
    Migration(up=_add_exchange_peer),  # irreversible: a column, kept
    Migration(up=_add_info_flow_columns),  # irreversible: columns, kept
)

# An adapter's life: pending -> trained -> active -> retired, or failed.
# Exactly one row per scope is ever 'active' — that's the voice in use.
ADAPTER_STATUSES: tuple[str, ...] = (
    "pending",
    "trained",
    "active",
    "retired",
    "failed",
)

_REASON_SEP = ""  # unit separator: reasons are prose, never structured


class RepresentativeStore:
    """SQLite memory scoped by account, never shared remotely."""

    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time):
        db_path = str(path) if str(path) == ":memory:" else str(Path(path).expanduser())
        if db_path != ":memory:":
            Path(db_path).resolve().parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.RLock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            migrate(self._db, MIGRATIONS, label="representative")

    # -------------------------------------------------------------- #
    # Settings.                                                       #
    # -------------------------------------------------------------- #
    def mode(self, scope: str) -> str:
        with self._lock:
            row = self._db.execute(
                "SELECT mode FROM representative_settings WHERE scope = ?", (scope,)
            ).fetchone()
        return str(row["mode"]) if row is not None else "off"

    def about(self, scope: str) -> str:
        with self._lock:
            row = self._db.execute(
                "SELECT persona_about FROM representative_settings WHERE scope = ?",
                (scope,),
            ).fetchone()
        return str(row["persona_about"]) if row is not None else ""

    def configure(
        self, scope: str, *, mode: str | None = None, about: str | None = None
    ) -> None:
        now = self._clock()
        # Switching ON (off -> draft/auto, or between on-modes) stamps
        # mode_on_at: the moment discarded drafts stop blocking a redraft.
        turned_on = mode is not None and mode != "off"
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO representative_settings
                       (scope, mode, persona_about, updated_at, mode_on_at)
                   VALUES (?, COALESCE(?, 'off'), COALESCE(?, ''), ?, ?)
                   ON CONFLICT(scope) DO UPDATE SET
                       mode = COALESCE(?, representative_settings.mode),
                       persona_about = COALESCE(?, representative_settings.persona_about),
                       updated_at = excluded.updated_at,
                       mode_on_at = CASE WHEN ? THEN excluded.updated_at
                                         ELSE representative_settings.mode_on_at END""",
                (
                    scope,
                    mode,
                    about,
                    now,
                    now if turned_on else 0,
                    mode,
                    about,
                    int(turned_on),
                ),
            )

    def mode_on_at(self, scope: str) -> float:
        with self._lock:
            row = self._db.execute(
                "SELECT mode_on_at FROM representative_settings WHERE scope = ?",
                (scope,),
            ).fetchone()
        return float(row["mode_on_at"]) if row is not None else 0.0

    # -------------------------------------------------------------- #
    # Exchanges (the memory, and later the training corpus).          #
    # -------------------------------------------------------------- #
    def remember_exchange(
        self, scope: str, *, key: str, prompt: str, reply: str, peer: str = ""
    ) -> None:
        prompt, reply = prompt.strip(), reply.strip()
        if not prompt or not reply:
            return
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO exchanges
                       (scope, exchange_key, prompt_text, reply_text, peer, at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(scope, exchange_key) DO UPDATE SET
                       prompt_text = excluded.prompt_text,
                       reply_text = excluded.reply_text,
                       peer = excluded.peer""",
                (scope, key, prompt, reply, peer.strip(), self._clock()),
            )

    def exchanges(self, scope: str, *, limit: int = 2000) -> list[sqlite3.Row]:
        """The most recent exchanges, newest first — recall's candidates."""
        with self._lock:
            return self._db.execute(
                """SELECT exchange_key, prompt_text, reply_text, peer, at
                   FROM exchanges
                   WHERE scope = ? ORDER BY at DESC, exchange_key DESC LIMIT ?""",
                (scope, int(limit)),
            ).fetchall()

    def exchange_count(self, scope: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS n FROM exchanges WHERE scope = ?", (scope,)
            ).fetchone()
        return int(row["n"])

    # -------------------------------------------------------------- #
    # Drafts.                                                         #
    # -------------------------------------------------------------- #
    def add_draft(self, draft: Draft) -> None:
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO drafts (
                       draft_id, scope, conversation_id, inbound_text,
                       generated_text, final_text, status, gate_score,
                       gate_commitment, gate_auto_ok, gate_reasons,
                       adapter_version, created_at, decided_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    draft.draft_id,
                    draft.scope,
                    draft.conversation_id,
                    draft.inbound_text,
                    draft.generated_text,
                    draft.final_text,
                    draft.status,
                    draft.gate.score,
                    int(draft.gate.commitment),
                    int(draft.gate.auto_ok),
                    _REASON_SEP.join(draft.gate.reasons),
                    draft.adapter_version,
                    draft.created_at,
                    draft.decided_at,
                ),
            )

    def get_draft(self, draft_id: str) -> Draft | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM drafts WHERE draft_id = ?", (draft_id,)
            ).fetchone()
        return self._draft(row) if row is not None else None

    def pending_drafts(self, scope: str, *, limit: int = 100) -> list[Draft]:
        with self._lock:
            rows = self._db.execute(
                """SELECT * FROM drafts WHERE scope = ? AND status = 'pending'
                   ORDER BY created_at DESC, draft_id DESC LIMIT ?""",
                (scope, int(limit)),
            ).fetchall()
        return [self._draft(row) for row in rows]

    def decide_draft(
        self,
        draft_id: str,
        *,
        status: str,
        final_text: str | None,
        from_statuses: tuple[str, ...] = ("pending",),
    ) -> Draft | None:
        """Record the user's decision; returns the updated draft, or None
        when the draft is missing or already decided (no double-spends).
        ``from_statuses`` widens the door for verdicts that also settle a
        waiting question (ignore covers needs_info too)."""
        marks = ", ".join("?" for _ in from_statuses)
        with self._lock, self._db:
            cursor = self._db.execute(
                f"""UPDATE drafts SET status = ?, final_text = ?, decided_at = ?
                   WHERE draft_id = ? AND status IN ({marks})""",
                (status, final_text, self._clock(), draft_id, *from_statuses),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) == 0:
                return None
        return self.get_draft(draft_id)

    def waiting_drafts(self, scope: str, *, limit: int = 100) -> list[Draft]:
        """Drafts waiting on information only the user can give, OLDEST
        first — the conversation works through them one at a time."""
        with self._lock:
            rows = self._db.execute(
                """SELECT * FROM drafts WHERE scope = ? AND status = 'needs_info'
                   ORDER BY created_at ASC, draft_id ASC LIMIT ?""",
                (scope, int(limit)),
            ).fetchall()
        return [self._draft(row) for row in rows]

    def next_unnotified(self, scope: str) -> Draft | None:
        """The oldest waiting draft whose questions have NOT yet been
        surfaced in the user's OoLu conversation — one ask per sweep."""
        with self._lock:
            row = self._db.execute(
                """SELECT * FROM drafts
                   WHERE scope = ? AND status = 'needs_info'
                     AND notified_at IS NULL
                   ORDER BY created_at ASC, draft_id ASC LIMIT 1""",
                (scope,),
            ).fetchone()
        return self._draft(row) if row is not None else None

    def mark_notified(self, draft_id: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE drafts SET notified_at = ? WHERE draft_id = ?",
                (self._clock(), draft_id),
            )

    def supersede_needs_info(
        self, scope: str, conversation_id: str, inbound_text: str, *, keep: str
    ) -> int:
        """Mark older waiting drafts for this exact message as ANSWERED —
        the user supplied the information and a fresh draft (``keep``)
        stands in their place. Answered drafts never block a redraft."""
        with self._lock, self._db:
            cursor = self._db.execute(
                """UPDATE drafts SET status = 'answered', decided_at = ?
                   WHERE scope = ? AND conversation_id = ? AND inbound_text = ?
                     AND status = 'needs_info' AND draft_id != ?""",
                (self._clock(), scope, conversation_id, inbound_text.strip(), keep),
            )
        return int(getattr(cursor, "rowcount", 0) or 0)

    def ignore_conversation(self, scope: str, conversation_id: str) -> int:
        """The user's word to let a peer's message rest: every standing
        draft (pending or waiting on info) for that conversation is
        settled as IGNORED. Returns how many were settled."""
        with self._lock, self._db:
            cursor = self._db.execute(
                """UPDATE drafts SET status = 'ignored', decided_at = ?
                   WHERE scope = ? AND conversation_id = ?
                     AND status IN ('pending', 'needs_info')""",
                (self._clock(), scope, conversation_id),
            )
        return int(getattr(cursor, "rowcount", 0) or 0)

    def recent_decisions(self, scope: str, *, limit: int = 50) -> list[str]:
        """The user's latest verdicts (sent/edited/discarded), newest first.
        Auto-sends are excluded — the representative grading itself would
        make autonomy self-reinforcing."""
        with self._lock:
            rows = self._db.execute(
                """SELECT status FROM drafts
                   WHERE scope = ? AND status IN ('sent', 'edited', 'discarded')
                   ORDER BY decided_at DESC, draft_id DESC LIMIT ?""",
                (scope, int(limit)),
            ).fetchall()
        return [str(row["status"]) for row in rows]

    def edited_pairs(self, scope: str, *, limit: int = 20_000) -> list[sqlite3.Row]:
        """Every draft the user rewrote before sending: (context, what the
        model said, what the user actually said) — the DPO dataset."""
        with self._lock:
            return self._db.execute(
                """SELECT inbound_text, generated_text, final_text,
                          conversation_id
                   FROM drafts
                   WHERE scope = ? AND status = 'edited' AND final_text IS NOT NULL
                   ORDER BY decided_at ASC LIMIT ?""",
                (scope, int(limit)),
            ).fetchall()

    def has_draft_for(
        self, scope: str, conversation_id: str, inbound_text: str
    ) -> bool:
        """Whether this exact message already has a STANDING draft — the
        sweep's idempotency, with a memory that forgives:

        - pending / needs_info / sent / edited / auto_sent / ignored
          block forever: the message is being handled, was answered, or
          was deliberately laid to rest;
        - answered never blocks — it was superseded by a real draft
          (which blocks on its own);
        - discarded blocks only while FRESH: a discard postpones, it
          never buries. It stops blocking after ``REDRAFT_AFTER_S`` (a
          day) or the moment the representative is switched on again —
          and a NEW message from the peer never matched here anyway.
        """
        from .models import REDRAFT_AFTER_S

        with self._lock:
            rows = self._db.execute(
                """SELECT status, decided_at FROM drafts
                   WHERE scope = ? AND conversation_id = ? AND inbound_text = ?""",
                (scope, conversation_id, inbound_text.strip()),
            ).fetchall()
        if not rows:
            return False
        mode_on = self.mode_on_at(scope)
        now = self._clock()
        for row in rows:
            status = str(row["status"])
            if status == "answered":
                continue
            if status == "discarded":
                decided = (
                    float(row["decided_at"])
                    if row["decided_at"] is not None
                    else 0.0
                )
                aged = now - decided >= REDRAFT_AFTER_S
                # Forgiven only by a switch-on strictly AFTER the discard.
                retoggled = decided < mode_on
                if aged or retoggled:
                    continue
            return True
        return False

    def outcome_counts(self, scope: str) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute(
                "SELECT status, COUNT(*) AS n FROM drafts WHERE scope = ? GROUP BY status",
                (scope,),
            ).fetchall()
        return {str(row["status"]): int(row["n"]) for row in rows}

    # -------------------------------------------------------------- #
    # The adapter registry (Phase 1): which trained voice is live.    #
    # -------------------------------------------------------------- #
    def begin_adapter(self, scope: str, *, base_model: str, message_count: int) -> int:
        """Open the next adapter version as pending; returns its number."""
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT COALESCE(MAX(version), 0) AS top FROM adapter_versions"
                " WHERE scope = ?",
                (scope,),
            ).fetchone()
            version = int(row["top"]) + 1
            self._db.execute(
                """INSERT INTO adapter_versions
                       (scope, version, base_model, status, message_count, created_at)
                   VALUES (?, ?, ?, 'pending', ?, ?)""",
                (scope, version, base_model, int(message_count), self._clock()),
            )
        return version

    def finish_adapter(
        self,
        scope: str,
        version: int,
        *,
        artifact_ref: str,
        holdout_ppl: float | None = None,
    ) -> None:
        with self._lock, self._db:
            self._db.execute(
                """UPDATE adapter_versions
                   SET status = 'trained', artifact_ref = ?, holdout_ppl = ?,
                       trained_at = ?
                   WHERE scope = ? AND version = ? AND status = 'pending'""",
                (artifact_ref, holdout_ppl, self._clock(), scope, version),
            )

    def activate_adapter(self, scope: str, version: int) -> bool:
        """Make one trained version the live voice; the previous active
        version retires in the same transaction — never two voices, and
        never zero because a bad candidate was offered. Returns whether
        the switch happened (only trained versions activate)."""
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT status FROM adapter_versions WHERE scope = ? AND version = ?",
                (scope, version),
            ).fetchone()
            if row is None or str(row["status"]) != "trained":
                return False
            self._db.execute(
                """UPDATE adapter_versions SET status = 'retired'
                   WHERE scope = ? AND status = 'active'""",
                (scope,),
            )
            self._db.execute(
                """UPDATE adapter_versions SET status = 'active'
                   WHERE scope = ? AND version = ?""",
                (scope, version),
            )
        return True

    def fail_adapter(self, scope: str, version: int) -> None:
        with self._lock, self._db:
            self._db.execute(
                """UPDATE adapter_versions SET status = 'failed'
                   WHERE scope = ? AND version = ? AND status = 'pending'""",
                (scope, version),
            )

    def active_adapter(self, scope: str) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM adapter_versions WHERE scope = ? AND status = 'active'",
                (scope,),
            ).fetchone()

    def adapter_history(self, scope: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(
                """SELECT * FROM adapter_versions WHERE scope = ?
                   ORDER BY version DESC""",
                (scope,),
            ).fetchall()

    # -------------------------------------------------------------- #
    # Per-peer rules: "never auto-reply to my boss."                  #
    # -------------------------------------------------------------- #
    def set_peer_auto(self, scope: str, peer: str, *, allowed: bool) -> None:
        peer = peer.strip()
        if not peer:
            raise ValueError("a peer rule needs a peer")
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO peer_rules (scope, peer, auto_allowed, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(scope, peer) DO UPDATE SET
                       auto_allowed = excluded.auto_allowed,
                       updated_at = excluded.updated_at""",
                (scope, peer, int(allowed), self._clock()),
            )

    def peer_auto_allowed(self, scope: str, peer: str) -> bool:
        """Whether auto-send may address this peer. Absent rule = allowed —
        the earned-autonomy and gate checks still stand either way."""
        with self._lock:
            row = self._db.execute(
                "SELECT auto_allowed FROM peer_rules WHERE scope = ? AND peer = ?",
                (scope, peer),
            ).fetchone()
        return bool(row["auto_allowed"]) if row is not None else True

    def muted_peers(self, scope: str) -> list[str]:
        with self._lock:
            rows = self._db.execute(
                """SELECT peer FROM peer_rules
                   WHERE scope = ? AND auto_allowed = 0 ORDER BY peer""",
                (scope,),
            ).fetchall()
        return [str(row["peer"]) for row in rows]

    def scopes(self) -> list[str]:
        """Every scope with the representative turned on — the sweep list."""
        with self._lock:
            rows = self._db.execute(
                "SELECT scope FROM representative_settings WHERE mode != 'off'"
            ).fetchall()
        return [str(row["scope"]) for row in rows]

    # -------------------------------------------------------------- #
    # Lifecycle.                                                      #
    # -------------------------------------------------------------- #
    def erase(self, scope: str) -> int:
        """Data-subject erasure: the account's whole representative, gone.
        Returns how many rows (settings + exchanges + drafts) were removed."""
        with self._lock, self._db:
            erased = 0
            for statement in (
                "DELETE FROM peer_rules WHERE scope = ?",
                "DELETE FROM adapter_versions WHERE scope = ?",
                "DELETE FROM drafts WHERE scope = ?",
                "DELETE FROM exchanges WHERE scope = ?",
                "DELETE FROM representative_settings WHERE scope = ?",
            ):
                cursor = self._db.execute(statement, (scope,))
                erased += int(getattr(cursor, "rowcount", 0) or 0)
        return erased

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @staticmethod
    def _draft(row: sqlite3.Row) -> Draft:
        reasons = str(row["gate_reasons"])
        return Draft(
            draft_id=row["draft_id"],
            scope=row["scope"],
            conversation_id=row["conversation_id"],
            inbound_text=row["inbound_text"],
            generated_text=row["generated_text"],
            final_text=row["final_text"],
            status=row["status"],
            gate=GateVerdict(
                score=float(row["gate_score"]),
                commitment=bool(row["gate_commitment"]),
                auto_ok=bool(row["gate_auto_ok"]),
                reasons=reasons.split(_REASON_SEP) if reasons else [],
            ),
            adapter_version=row["adapter_version"],
            created_at=float(row["created_at"]),
            decided_at=(
                float(row["decided_at"]) if row["decided_at"] is not None else None
            ),
        )


def ingest_exchanges(
    store: RepresentativeStore,
    scope: str,
    exchanges: Iterable[tuple[str, str, str]],
) -> int:
    """Upsert (key, prompt, reply) triples; idempotent by key. Returns how
    many triples were offered (upserts don't distinguish new from refreshed)."""
    count = 0
    for key, prompt, reply in exchanges:
        store.remember_exchange(scope, key=key, prompt=prompt, reply=reply)
        count += 1
    return count
