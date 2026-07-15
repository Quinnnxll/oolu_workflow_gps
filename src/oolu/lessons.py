"""Imitation lessons: guided demonstrations that become nodes.

The original vision was a record button that watches the user drive OTHER
software — global mouse/keyboard capture and screen recording. The honest
audit says no: the desktop shell is deliberately capability-minimal (no
input hooks, no screen capture), and mobile platforms will not allow it
at all. What the platform DOES own, completely, is everything that runs
THROUGH a node: the hash-chained audit log of every execution, each
node's daily execution log file, every file that passes the drawer.

So a lesson is taught the way the platform can actually see: the user
NAMES the goal, DESCRIBES each step in order, and RUNS the real work
through the node while recording — the execution logs pair with the
demonstrated steps automatically. The finished lesson is a structured
data log (goal + ordered steps + paired runs) that is stored verbatim:
once as rows here, once as a JSON file in the built node's own drawer —
the training corpus the user asked node creation to become.

Deterministic-first, as everywhere: the recorded steps ARE the plan. The
model is consulted once, at build time, to write the execution function
FROM the demonstration — never again per run; running the node replays
its verified function, not a fresh guess.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Callable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

# A step is one action in the user's words, not an essay.
MAX_STEP_CHARS = 500

# A demonstration is a procedure, not a lifetime: enough for any real
# workflow, small enough to stay a readable training record.
MAX_LESSON_STEPS = 100

# What a recorded step can be: the user's own words, a run of real work
# paired from the execution logs, or a file that passed the drawer.
STEP_KINDS = frozenset({"say", "run", "file"})


class LessonStep(BaseModel):
    model_config = ConfigDict(frozen=True)

    seq: int
    kind: str  # say | run | file
    text: str
    at: datetime


class Lesson(BaseModel):
    model_config = ConfigDict(frozen=True)

    lesson_id: str
    tenant: str
    node_id: str  # the node whose window taught it
    owner: str
    goal: str
    status: str  # recording | built | discarded
    created_at: datetime
    ended_at: datetime | None = None
    built_node_id: str = ""
    steps: list[LessonStep] = Field(default_factory=list)


_SCHEMA = """CREATE TABLE IF NOT EXISTS lessons (
    lesson_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    goal TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    ended_at TEXT,
    built_node_id TEXT NOT NULL DEFAULT ''
)"""

_STEPS_SCHEMA = """CREATE TABLE IF NOT EXISTS lesson_steps (
    lesson_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    at TEXT NOT NULL,
    PRIMARY KEY (lesson_id, seq)
)"""


class LessonStore:
    """SQLite-backed demonstrations over the shell's durable connection."""

    def __init__(self, conn, *, clock: Callable[[], datetime] | None = None):
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)
            db.execute(_STEPS_SCHEMA)

    def start(
        self, *, tenant: str, node_id: str, owner: str, goal: str
    ) -> Lesson:
        """Open a recording — one at a time per node and owner: a lesson
        is a single procedure, and two recordings would interleave."""
        goal = " ".join(str(goal or "").split())
        if not goal:
            raise ValueError("a lesson needs a goal — what should it teach?")
        if len(goal) > MAX_STEP_CHARS:
            raise ValueError("a goal is one sentence, not a document")
        if self.active(tenant=tenant, node_id=node_id, owner=owner) is not None:
            raise ValueError(
                "a lesson is already recording here — finish or discard it"
            )
        lesson = Lesson(
            lesson_id=uuid4().hex,
            tenant=tenant,
            node_id=node_id,
            owner=owner,
            goal=goal,
            status="recording",
            created_at=self._clock(),
        )
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO lessons
                       (lesson_id, tenant_id, node_id, owner, goal, status,
                        created_at, ended_at, built_node_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, NULL, '')""",
                (
                    lesson.lesson_id,
                    tenant,
                    node_id,
                    owner,
                    goal,
                    lesson.status,
                    lesson.created_at.isoformat(),
                ),
            )
        return lesson

    def active(
        self, *, tenant: str, node_id: str, owner: str
    ) -> Lesson | None:
        """The recording lesson in this node's window, steps included."""
        with self._conn.lock:
            row = self._conn.db.execute(
                """SELECT * FROM lessons WHERE tenant_id = ? AND node_id = ?
                     AND owner = ? AND status = 'recording'""",
                (tenant, node_id, owner),
            ).fetchone()
        return self._hydrate(row) if row is not None else None

    def add_step(
        self,
        lesson_id: str,
        *,
        tenant: str,
        owner: str,
        kind: str,
        text: str,
    ) -> LessonStep:
        """Append one step to a RECORDING lesson — order is the lesson."""
        if kind not in STEP_KINDS:
            raise ValueError("a step is 'say', 'run', or 'file'")
        text = " ".join(str(text or "").split())
        if not text:
            raise ValueError("a step needs words")
        if len(text) > MAX_STEP_CHARS:
            raise ValueError("a step is one action, not an essay")
        lesson = self.get(lesson_id, tenant=tenant, owner=owner)
        if lesson is None or lesson.status != "recording":
            raise ValueError("no lesson is recording here")
        if len(lesson.steps) >= MAX_LESSON_STEPS:
            raise ValueError(
                "the lesson is full — a demonstration is a procedure, "
                "not a lifetime; stop and build, then teach the rest "
                "as a second lesson"
            )
        step = LessonStep(
            seq=len(lesson.steps) + 1, kind=kind, text=text, at=self._clock()
        )
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO lesson_steps (lesson_id, seq, kind, text, at)
                   VALUES (?, ?, ?, ?, ?)""",
                (lesson_id, step.seq, kind, text, step.at.isoformat()),
            )
        return step

    def finish(
        self,
        lesson_id: str,
        *,
        tenant: str,
        owner: str,
        status: str,
        built_node_id: str = "",
    ) -> Lesson | None:
        """Close the recording as 'built' or 'discarded' — exactly once."""
        if status not in {"built", "discarded"}:
            raise ValueError("a lesson ends built or discarded")
        with self._conn.transaction() as db:
            cursor = db.execute(
                """UPDATE lessons SET status = ?, ended_at = ?,
                       built_node_id = ?
                   WHERE lesson_id = ? AND tenant_id = ? AND owner = ?
                     AND status = 'recording'""",
                (
                    status,
                    self._clock().isoformat(),
                    built_node_id,
                    lesson_id,
                    tenant,
                    owner,
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) == 0:
                return None
        return self.get(lesson_id, tenant=tenant, owner=owner)

    def get(
        self, lesson_id: str, *, tenant: str, owner: str
    ) -> Lesson | None:
        """The caller's own lesson — a stranger's is indistinguishable
        from missing."""
        with self._conn.lock:
            row = self._conn.db.execute(
                """SELECT * FROM lessons WHERE lesson_id = ?
                     AND tenant_id = ? AND owner = ?""",
                (lesson_id, tenant, owner),
            ).fetchone()
        return self._hydrate(row) if row is not None else None

    def erase(self, *, tenant: str, owner: str) -> int:
        """Data-subject erasure: the account's lessons and their steps."""
        with self._conn.transaction() as db:
            ids = [
                r["lesson_id"]
                for r in db.execute(
                    "SELECT lesson_id FROM lessons WHERE tenant_id = ?"
                    " AND owner = ?",
                    (tenant, owner),
                ).fetchall()
            ]
            for lesson_id in ids:
                db.execute(
                    "DELETE FROM lesson_steps WHERE lesson_id = ?",
                    (lesson_id,),
                )
            db.execute(
                "DELETE FROM lessons WHERE tenant_id = ? AND owner = ?",
                (tenant, owner),
            )
        return len(ids)

    # ------------------------------------------------------------------ #
    def _hydrate(self, row) -> Lesson:
        with self._conn.lock:
            step_rows = self._conn.db.execute(
                "SELECT * FROM lesson_steps WHERE lesson_id = ?"
                " ORDER BY seq ASC",
                (row["lesson_id"],),
            ).fetchall()
        return Lesson(
            lesson_id=row["lesson_id"],
            tenant=row["tenant_id"],
            node_id=row["node_id"],
            owner=row["owner"],
            goal=row["goal"],
            status=row["status"],
            created_at=datetime.fromisoformat(row["created_at"]),
            ended_at=(
                datetime.fromisoformat(row["ended_at"])
                if row["ended_at"] is not None
                else None
            ),
            built_node_id=row["built_node_id"],
            steps=[
                LessonStep(
                    seq=s["seq"],
                    kind=s["kind"],
                    text=s["text"],
                    at=datetime.fromisoformat(s["at"]),
                )
                for s in step_rows
            ],
        )
