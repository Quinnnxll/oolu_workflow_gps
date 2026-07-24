"""The build ledger — a node build remembers itself across turns.

Phase 5 of the context-harness plan. The build door is synchronous, but
the WORK is not: a build that fails birth verification today is retried
tomorrow, after ten unrelated chat turns, possibly from another process
— and until now the retry started from zero, free to repeat exactly the
mistake that sank the first attempt. This ledger is the spec's task
ledger scoped to builds: one durable row per attempt (goal, script,
problem, transaction states, outcome), persisted through the same
DurableConnection every other promise rides — never the model's
transcript.

Two reads feed the next attempt:

- ``lessons_for`` — the standing lessons for a goal, compiled into the
  build context pack's lessons section, each one born from a real
  refused attempt (provenance: the attempt row id it cites).
- ``last_failure`` — the most recent refused attempt whole (script,
  problem), for callers that want to resume the work, not just recall
  the warning.

Supersession is the write-back rule (the spec's correction policy): a
PUBLISHED build closes the book — every open lesson for that goal is
stamped superseded by the publishing attempt and stops entering context
packs. A lesson about a problem that no longer exists is stale context,
and stale context is what this whole plan exists to keep out of seats.

The focus story rides here too, deliberately shaped around a consent
invariant: the growth offer still lives for exactly one message
("consent detached from the question it answered is not consent" —
gateway), so a side question DOES withdraw the offer. What survives the
interruption is the WORK: the failed attempt's state, in this ledger,
rehydrated the moment the user comes back to the goal.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

_SCHEMA_ATTEMPTS = """CREATE TABLE IF NOT EXISTS build_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant TEXT NOT NULL,
    goal_key TEXT NOT NULL,
    goal TEXT NOT NULL,
    status TEXT NOT NULL,
    script TEXT NOT NULL DEFAULT '',
    problem TEXT NOT NULL DEFAULT '',
    states TEXT NOT NULL DEFAULT '[]',
    node_id TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
)"""

_SCHEMA_LESSONS = """CREATE TABLE IF NOT EXISTS build_lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant TEXT NOT NULL,
    goal_key TEXT NOT NULL,
    lesson TEXT NOT NULL,
    source_attempt INTEGER NOT NULL,
    superseded_by INTEGER,
    created_at TEXT NOT NULL
)"""

# Outcomes the door reports. "refused" admits a lesson; "published"
# supersedes the goal's open lessons; "declined" (conversation, twin
# guard) records nothing worth teaching.
_STATUSES = ("published", "refused", "declined")


class BuildLedger:
    """Durable build attempts and their lessons, per (tenant, goal_key)."""

    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_SCHEMA_ATTEMPTS)
            db.execute(_SCHEMA_LESSONS)

    # ------------------------------------------------------------------ #
    def record(
        self,
        tenant: str,
        goal_key: str,
        goal: str,
        *,
        status: str,
        script: str = "",
        problem: str = "",
        states: tuple[str, ...] | list[str] = (),
        node_id: str = "",
        model: str = "",
    ) -> int:
        """One attempt on the record; returns its row id (the provenance
        every lesson cites). A refusal admits a lesson; a publish
        supersedes the goal's open lessons — corrections beat stale
        warnings, per the plan's write-back policy. ``model`` names WHO
        sat in the seat for the attempt — the per-model outcome history
        performance-fed routing reads (Phase 6)."""
        if status not in _STATUSES:
            raise ValueError(f"unknown build status {status!r}")
        stamp = datetime.now(UTC).isoformat()
        with self._conn.transaction() as db:
            cursor = db.execute(
                """INSERT INTO build_attempts
                   (tenant, goal_key, goal, status, script, problem,
                    states, node_id, model, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    tenant,
                    goal_key,
                    goal,
                    status,
                    script or "",
                    problem or "",
                    json.dumps(list(states), ensure_ascii=False),
                    node_id or "",
                    model or "",
                    stamp,
                ),
            )
            attempt_id = int(cursor.lastrowid)
            if status == "refused" and problem:
                db.execute(
                    """INSERT INTO build_lessons
                       (tenant, goal_key, lesson, source_attempt,
                        superseded_by, created_at)
                       VALUES (?, ?, ?, ?, NULL, ?)""",
                    (
                        tenant,
                        goal_key,
                        (
                            "a previous attempt at this goal failed birth "
                            f"verification with: {problem[:400]}"
                        ),
                        attempt_id,
                        stamp,
                    ),
                )
            elif status == "published":
                db.execute(
                    """UPDATE build_lessons
                       SET superseded_by = ?
                       WHERE tenant = ? AND goal_key = ?
                         AND superseded_by IS NULL""",
                    (attempt_id, tenant, goal_key),
                )
        return attempt_id

    # ------------------------------------------------------------------ #
    def lessons_for(self, tenant: str, goal_key: str, *, limit: int = 3) -> list[str]:
        """The goal's standing (unsuperseded) lessons, newest first —
        what the context pack's lessons section carries into the seat."""
        with self._conn.transaction() as db:
            rows = db.execute(
                """SELECT lesson FROM build_lessons
                   WHERE tenant = ? AND goal_key = ? AND superseded_by IS NULL
                   ORDER BY id DESC LIMIT ?""",
                (tenant, goal_key, int(limit)),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def last_failure(self, tenant: str, goal_key: str) -> dict | None:
        """The most recent refused attempt, whole — None once a later
        publish closed the book on the goal."""
        with self._conn.transaction() as db:
            row = db.execute(
                """SELECT id, status, script, problem, goal, created_at
                   FROM build_attempts
                   WHERE tenant = ? AND goal_key = ?
                     AND status IN ('refused', 'published')
                   ORDER BY id DESC LIMIT 1""",
                (tenant, goal_key),
            ).fetchone()
        if row is None or str(row[1]) != "refused":
            return None
        return {
            "attempt_id": int(row[0]),
            "script": str(row[2]),
            "problem": str(row[3]),
            "goal": str(row[4]),
            "created_at": str(row[5]),
        }

    def seat_performance(self, tenant: str) -> list[dict]:
        """Per-model outcome history for the build seat, best first:
        who published, who kept getting refused, at what rate — the
        evidence performance-fed routing demotes on, and the honest
        answer to "is this model earning the node.build seat?". Rows
        without a model name (stub authors, pre-Phase-6 attempts) are
        aggregated under ``""`` rather than dropped — history is
        history."""
        with self._conn.transaction() as db:
            rows = db.execute(
                """SELECT model,
                          SUM(CASE WHEN status = 'published' THEN 1 ELSE 0 END),
                          SUM(CASE WHEN status = 'refused' THEN 1 ELSE 0 END)
                   FROM build_attempts
                   WHERE tenant = ? AND status IN ('published', 'refused')
                   GROUP BY model""",
                (tenant,),
            ).fetchall()
        board = []
        for model, published, refused in rows:
            published, refused = int(published or 0), int(refused or 0)
            total = published + refused
            board.append(
                {
                    "model": str(model or ""),
                    "published": published,
                    "refused": refused,
                    "success_rate": (published / total) if total else 0.0,
                }
            )
        board.sort(key=lambda row: (-row["success_rate"], -row["published"]))
        return board

    def attempts(self, tenant: str, goal_key: str) -> list[dict]:
        """The goal's full attempt history, oldest first — the audit
        view; the ledger never forgets, it only supersedes."""
        with self._conn.transaction() as db:
            rows = db.execute(
                """SELECT id, status, problem, states, node_id, created_at
                   FROM build_attempts
                   WHERE tenant = ? AND goal_key = ? ORDER BY id ASC""",
                (tenant, goal_key),
            ).fetchall()
        return [
            {
                "attempt_id": int(row[0]),
                "status": str(row[1]),
                "problem": str(row[2]),
                "states": json.loads(row[3] or "[]"),
                "node_id": str(row[4]),
                "created_at": str(row[5]),
            }
            for row in rows
        ]
