"""The benchmark's measuring stick (N0) — reading, honestly measured.

A news post succeeds or fails on three signals from its readers
(docs/news-agent-benchmark-roadmap.md): ATTENTION TIME (how long the
opened story held them), COMPLETE-READING RATE (of those who opened,
how many reached the end), and LIKES (the explicit tap). This module is
the durable substrate for all three, under the reporter's fourth law:

- **Per-member rows only under consent.** The doors record a receipt
  or a like only while ``press.personalize`` is on — the caller checks
  the switch; nothing here is written for a member who did not opt in.
- **One receipt per (story, member).** Re-reading updates the row —
  the longest dwell stands, and completion, once true, stays true. A
  reader is counted once, never inflated by re-opens.
- **One like, once.** The insert is idempotent; there is no unlike —
  the first tap is the tap (the vote-idempotence law, reborn).
- **Aggregates over a floor.** The editorial read renders only above
  ``METRICS_K_FLOOR`` readers — below it the honest answer is "not
  enough readers yet", with no counts to reverse-engineer a neighbor's
  reading from.
- **Erasure outranks the aggregate.** A member's rows delete with the
  account; the aggregate honestly shrinks.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Callable

# Below this many distinct readers, no aggregate renders. A working
# default, deliberately the poll floor's old K.
METRICS_K_FLOOR = 5

_RECEIPTS_SCHEMA = """CREATE TABLE IF NOT EXISTS press_read_receipts (
    tenant_id TEXT NOT NULL,
    story_id TEXT NOT NULL,
    principal TEXT NOT NULL,
    dwell_ms INTEGER NOT NULL,
    completed INTEGER NOT NULL,
    at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, story_id, principal)
)"""

_LIKES_SCHEMA = """CREATE TABLE IF NOT EXISTS press_story_likes (
    tenant_id TEXT NOT NULL,
    story_id TEXT NOT NULL,
    principal TEXT NOT NULL,
    at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, story_id, principal)
)"""


class StoryMetricsStore:
    """Read receipts and likes, per story — durable, tenant-scoped,
    consent-gated at the doors, erasable per member."""

    def __init__(self, conn, *, clock: Callable[[], datetime] | None = None) -> None:
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_RECEIPTS_SCHEMA)
            db.execute(_LIKES_SCHEMA)

    # -- writing (the caller has already checked consent) --------------- #
    def read_receipt(
        self,
        *,
        tenant: str,
        story_id: str,
        principal: str,
        dwell_ms: int,
        completed: bool,
    ) -> None:
        """One reader, one row: the longest dwell stands; completion,
        once true, stays true — a re-open never un-finishes a story."""
        dwell = max(0, int(dwell_ms))
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO press_read_receipts
                     (tenant_id, story_id, principal, dwell_ms, completed, at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (tenant_id, story_id, principal) DO UPDATE SET
                     dwell_ms = MAX(press_read_receipts.dwell_ms,
                                    excluded.dwell_ms),
                     completed = MAX(press_read_receipts.completed,
                                     excluded.completed),
                     at = excluded.at""",
                (
                    tenant,
                    story_id,
                    principal,
                    dwell,
                    1 if completed else 0,
                    self._clock().isoformat(),
                ),
            )

    def like(self, *, tenant: str, story_id: str, principal: str) -> bool:
        """Idempotent: True when this tap was the member's first."""
        with self._conn.transaction() as db:
            cursor = db.execute(
                """INSERT INTO press_story_likes
                     (tenant_id, story_id, principal, at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT (tenant_id, story_id, principal) DO NOTHING""",
                (tenant, story_id, principal, self._clock().isoformat()),
            )
        return bool(getattr(cursor, "rowcount", 0) or 0)

    # -- the editorial read --------------------------------------------- #
    def aggregate(
        self, *, tenant: str, story_id: str, k_floor: int = METRICS_K_FLOOR
    ) -> dict:
        """The three benchmark numbers — or an honest refusal below the
        floor (distinct READERS gate the reveal; a pile of likes alone
        never opens the door)."""
        with self._conn.lock:
            row = self._conn.db.execute(
                """SELECT COUNT(*) AS opens,
                          COALESCE(AVG(dwell_ms), 0) AS mean_dwell,
                          COALESCE(SUM(completed), 0) AS completions
                     FROM press_read_receipts
                    WHERE tenant_id = ? AND story_id = ?""",
                (tenant, story_id),
            ).fetchone()
            likes = self._conn.db.execute(
                """SELECT COUNT(*) AS n FROM press_story_likes
                    WHERE tenant_id = ? AND story_id = ?""",
                (tenant, story_id),
            ).fetchone()
        opens = int(row["opens"])
        if opens < int(k_floor):
            return {
                "story_id": story_id,
                "revealed": False,
                "reason": "not enough readers yet",
            }
        return {
            "story_id": story_id,
            "revealed": True,
            "opens": opens,
            "likes": int(likes["n"]),
            "mean_attention_ms": int(round(float(row["mean_dwell"]))),
            "completion_rate": round(int(row["completions"]) / opens, 4),
        }

    def genre_evidence(
        self,
        *,
        tenant: str,
        genres_of: Callable[[str], tuple[str, ...]],
    ) -> dict:
        """The N0 signals rolled up PER GENRE for the demand desk (N1):
        distinct readers, opens, completions, dwell, likes — aggregates
        only; the per-member rows never leave this store. ``genres_of``
        resolves a story to its genres (a story can carry several; its
        evidence counts toward each)."""
        from .demand import GenreEvidence

        with self._conn.lock:
            receipts = self._conn.db.execute(
                """SELECT story_id, principal, dwell_ms, completed
                     FROM press_read_receipts WHERE tenant_id = ?""",
                (tenant,),
            ).fetchall()
            likes = self._conn.db.execute(
                """SELECT story_id FROM press_story_likes
                    WHERE tenant_id = ?""",
                (tenant,),
            ).fetchall()
        readers: dict[str, set[str]] = {}
        tally: dict[str, dict[str, int]] = {}
        for row in receipts:
            for genre in genres_of(str(row["story_id"])):
                readers.setdefault(genre, set()).add(str(row["principal"]))
                book = tally.setdefault(
                    genre,
                    {"opens": 0, "completions": 0, "dwell_ms": 0, "likes": 0},
                )
                book["opens"] += 1
                book["completions"] += int(row["completed"])
                book["dwell_ms"] += int(row["dwell_ms"])
        for row in likes:
            for genre in genres_of(str(row["story_id"])):
                book = tally.setdefault(
                    genre,
                    {"opens": 0, "completions": 0, "dwell_ms": 0, "likes": 0},
                )
                book["likes"] += 1
        return {
            genre: GenreEvidence(
                readers=len(readers.get(genre, ())),
                opens=book["opens"],
                completions=book["completions"],
                dwell_ms=book["dwell_ms"],
                likes=book["likes"],
            )
            for genre, book in tally.items()
        }

    # -- the data subject's right --------------------------------------- #
    def erase(self, *, tenant: str, principal: str) -> int:
        """The member's reading and likes, gone — the aggregates
        honestly shrink."""
        erased = 0
        with self._conn.transaction() as db:
            for table in ("press_read_receipts", "press_story_likes"):
                cursor = db.execute(
                    f"DELETE FROM {table}"
                    " WHERE tenant_id = ? AND principal = ?",
                    (tenant, principal),
                )
                erased += int(getattr(cursor, "rowcount", 0) or 0)
        return erased
