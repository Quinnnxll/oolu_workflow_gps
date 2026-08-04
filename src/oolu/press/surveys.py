"""The survey desk (N3) — ask the members what the records cannot tell.

The pipeline's one opinion instrument, rebuilt on the News desk without
the poll floor's social-feed shape: a survey is OPENED FOR A TOPIC (the
slate's brief is the reason it exists), asks ONE question with typed
options, and collects answers in conversation two ways — a bounded
RANDOM SAMPLE of consented members gets the question block landed in
their News thread (with an honest "why am I seeing this"), and any
member can volunteer an answer from the open list ("surveys").

The laws:

- **Consent gates participation.** Only `press.personalize` members are
  sampled or recorded; consent off answers "not recorded" and writes
  NOTHING per-member.
- **One answer, once.** An answer is durable and idempotent: replaying
  it changes nothing; changing your mind is refused — the first answer
  is the answer (the vote law, reborn where it belongs).
- **The floor holds.** Aggregates render only at or above
  ``SURVEY_K_FLOOR`` answers — below it the honest reply is "not
  enough answers yet"; no individual answer is ever rendered or
  exported, anywhere.
- **The draw is recorded.** The random sample is drawn with a seed the
  survey stores (the desk doctrine: sampled decisions replay exactly).
- **Erasure outranks everything.** A member's answers and sample
  memberships delete with the account; aggregates honestly shrink.
- **Traceable by construction.** A closed survey's result row —
  survey id, topic key, question, sample size, the aggregate — is the
  typed source a composed post cites (N4), and the respondent set is
  retained pseudonymously for the revenue split (N6).

Question kinds, composed deterministically from the topic brief:

- ``telling`` — a corroborated member cluster asks "which telling
  serves the reader better?" with two contributions as options; a
  consented answer ALSO writes the member's own pairwise preference
  row (`press/pairwise.py` — the book's first writer since the poll
  floor closed).
- ``editorial`` — every other brief asks whether the subject is worth
  a full story, with fixed typed options.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Callable, Mapping, Sequence

from .contributions import PressError

SURVEY_VERSION = 1
# Below this many answers, no aggregate renders — the metrics floor's
# number, applied to opinion.
SURVEY_K_FLOOR = 5
# The bounded random sample: how many consented members one survey may
# pull into its question. A survey, not a broadcast.
SURVEY_SAMPLE_SIZE = 12
# An open survey older than this closes on the next desk tick, whatever
# its count — a question that lingers is a nag, not research.
SURVEY_TTL_HOURS = 48

# The editorial kind's fixed options — typed, versioned by the module.
EDITORIAL_OPTIONS: tuple[tuple[str, str], ...] = (
    ("worth", "Worth a full story"),
    ("not_worth", "Not worth telling"),
    ("more_evidence", "Need more evidence first"),
)


@dataclass(frozen=True)
class SurveyOption:
    key: str
    label: str
    contribution_id: str = ""  # telling-kind: the piece this option IS


@dataclass(frozen=True)
class Survey:
    survey_id: str
    tenant_id: str
    topic_key: str  # the brief this survey researches
    kind: str  # "telling" | "editorial"
    question: str
    options: tuple[SurveyOption, ...]
    reason: str  # the honest "why am I seeing this"
    status: str  # "open" | "closed"
    opened_at: datetime
    closed_at: datetime | None = None
    draw_seed: int | None = None
    # The BRIEF SNAPSHOT: the topic brief exactly as it stood when the
    # survey opened — the slate replaces whole, so composition (N4)
    # reads the survey's own copy, never a vanished row.
    brief: dict | None = None


def compose_survey(
    brief: Mapping,
    *,
    survey_id: str,
    opened_at: datetime,
    draw_seed: int | None = None,
) -> Survey:
    """One question from one brief, deterministically. A cluster brief
    with two tellings asks WHICH SERVES THE READER; everything else
    asks whether the subject is worth a full story."""
    subject = str(brief.get("subject") or "")
    topic_key = str(brief.get("topic_key") or brief.get("key") or "")
    contribution_facts = [
        f
        for f in (brief.get("facts") or [])
        if isinstance(f, Mapping) and f.get("kind") == "contribution"
    ]
    if len(contribution_facts) >= 2:
        left, right = contribution_facts[0], contribution_facts[1]
        options = (
            SurveyOption(
                key="left",
                label=str(left.get("summary") or ""),
                contribution_id=str(left.get("ref") or ""),
            ),
            SurveyOption(
                key="right",
                label=str(right.get("summary") or ""),
                contribution_id=str(right.get("ref") or ""),
            ),
        )
        return Survey(
            survey_id=survey_id,
            tenant_id=str(brief.get("tenant_id") or ""),
            topic_key=topic_key,
            kind="telling",
            question=(
                f"The desk is researching: {subject}. Which telling "
                "serves the reader better?"
            ),
            options=options,
            reason=(
                "The News desk is researching this subject. One answer "
                "per member, counted anonymously — nothing renders "
                f"below {SURVEY_K_FLOOR} answers."
            ),
            status="open",
            opened_at=opened_at,
            draw_seed=draw_seed,
            brief=dict(brief),
        )
    return Survey(
        survey_id=survey_id,
        tenant_id=str(brief.get("tenant_id") or ""),
        topic_key=topic_key,
        kind="editorial",
        question=(
            f"The desk is researching: {subject}. Is this worth a "
            "full story?"
        ),
        options=tuple(
            SurveyOption(key=key, label=label)
            for key, label in EDITORIAL_OPTIONS
        ),
        reason=(
            "The News desk is researching this subject. One answer per "
            "member, counted anonymously — nothing renders below "
            f"{SURVEY_K_FLOOR} answers."
        ),
        status="open",
        opened_at=opened_at,
        draw_seed=draw_seed,
        brief=dict(brief),
    )


def draw_sample(
    members: Sequence[str], *, size: int = SURVEY_SAMPLE_SIZE,
    rng: random.Random,
) -> list[str]:
    """The bounded random sample — order-independent of the input
    (sorted first), replayable from the survey's recorded seed."""
    frame = sorted(set(members))
    if len(frame) <= size:
        return frame
    return sorted(rng.sample(frame, size))


_SURVEYS_SCHEMA = """CREATE TABLE IF NOT EXISTS press_surveys (
    survey_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    topic_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    question TEXT NOT NULL,
    options TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    draw_seed INTEGER,
    survey_version INTEGER NOT NULL,
    brief TEXT NOT NULL DEFAULT '{}'
)"""

# The respondent frame actually drawn — pseudonymous rows, erasable,
# retained for the N6 revenue split.
_SAMPLE_SCHEMA = """CREATE TABLE IF NOT EXISTS press_survey_sample (
    survey_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    principal TEXT NOT NULL,
    PRIMARY KEY (survey_id, principal)
)"""

_ANSWERS_SCHEMA = """CREATE TABLE IF NOT EXISTS press_survey_answers (
    survey_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    principal TEXT NOT NULL,
    choice TEXT NOT NULL,
    at TEXT NOT NULL,
    PRIMARY KEY (survey_id, principal)
)"""


class SurveyStore:
    """Surveys, the drawn samples, and the answers — durable,
    tenant-scoped. The laws (one answer once, the floor, erasure) live
    in :class:`SurveyDesk` and the doors; the rows live here."""

    def __init__(self, conn, *, clock: Callable[[], datetime] | None = None) -> None:
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_SURVEYS_SCHEMA)
            db.execute(_SAMPLE_SCHEMA)
            db.execute(_ANSWERS_SCHEMA)
        self._migrate_brief_column()

    def _migrate_brief_column(self) -> None:
        # Tables born before the snapshot column: old surveys read an
        # empty brief — composition honestly skips them.
        try:
            with self._conn.transaction() as db:
                db.execute("SELECT brief FROM press_surveys LIMIT 1")
            return
        except Exception:
            pass
        with self._conn.transaction() as db:
            db.execute(
                "ALTER TABLE press_surveys"
                " ADD COLUMN brief TEXT NOT NULL DEFAULT '{}'"
            )

    def now(self) -> datetime:
        return self._clock()

    # -- surveys -------------------------------------------------------- #
    def insert(self, survey: Survey) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO press_surveys
                     (survey_id, tenant_id, topic_key, kind, question,
                      options, reason, status, opened_at, closed_at,
                      draw_seed, survey_version, brief)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    survey.survey_id,
                    survey.tenant_id,
                    survey.topic_key,
                    survey.kind,
                    survey.question,
                    json.dumps(
                        [
                            {
                                "key": o.key,
                                "label": o.label,
                                "contribution_id": o.contribution_id,
                            }
                            for o in survey.options
                        ]
                    ),
                    survey.reason,
                    survey.status,
                    survey.opened_at.isoformat(),
                    None,
                    survey.draw_seed,
                    SURVEY_VERSION,
                    json.dumps(survey.brief or {}),
                ),
            )

    def get(self, survey_id: str, *, tenant: str) -> Survey | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT * FROM press_surveys"
                " WHERE survey_id = ? AND tenant_id = ?",
                (survey_id, tenant),
            ).fetchone()
        return self._survey(row) if row is not None else None

    def list(
        self, *, tenant: str, status: str | None = None, limit: int = 20
    ) -> list[Survey]:
        where = "tenant_id = ?"
        args: list = [tenant]
        if status is not None:
            where += " AND status = ?"
            args.append(status)
        with self._conn.lock:
            rows = self._conn.db.execute(
                f"""SELECT * FROM press_surveys WHERE {where}
                    ORDER BY opened_at DESC, survey_id DESC LIMIT ?""",
                (*args, int(limit)),
            ).fetchall()
        return [self._survey(row) for row in rows]

    def close(self, survey_id: str, *, tenant: str) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """UPDATE press_surveys SET status = 'closed', closed_at = ?
                    WHERE survey_id = ? AND tenant_id = ?
                      AND status = 'open'""",
                (self._clock().isoformat(), survey_id, tenant),
            )

    # -- the sample ----------------------------------------------------- #
    def record_sample(
        self, survey_id: str, *, tenant: str, principals: Sequence[str]
    ) -> None:
        with self._conn.transaction() as db:
            for principal in principals:
                db.execute(
                    """INSERT INTO press_survey_sample
                         (survey_id, tenant_id, principal)
                       VALUES (?, ?, ?)
                       ON CONFLICT (survey_id, principal) DO NOTHING""",
                    (survey_id, tenant, principal),
                )

    def sample_of(self, survey_id: str, *, tenant: str) -> list[str]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                """SELECT principal FROM press_survey_sample
                   WHERE survey_id = ? AND tenant_id = ?
                   ORDER BY principal""",
                (survey_id, tenant),
            ).fetchall()
        return [str(row["principal"]) for row in rows]

    # -- answers -------------------------------------------------------- #
    def answer_of(
        self, survey_id: str, *, tenant: str, principal: str
    ) -> str | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                """SELECT choice FROM press_survey_answers
                   WHERE survey_id = ? AND tenant_id = ? AND principal = ?""",
                (survey_id, tenant, principal),
            ).fetchone()
        return str(row["choice"]) if row is not None else None

    def record_answer(
        self, survey_id: str, *, tenant: str, principal: str, choice: str
    ) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO press_survey_answers
                     (survey_id, tenant_id, principal, choice, at)
                   VALUES (?, ?, ?, ?, ?)""",
                (survey_id, tenant, principal, choice,
                 self._clock().isoformat()),
            )

    def counts(self, survey_id: str, *, tenant: str) -> dict[str, int]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                """SELECT choice, COUNT(*) AS n FROM press_survey_answers
                   WHERE survey_id = ? AND tenant_id = ?
                   GROUP BY choice""",
                (survey_id, tenant),
            ).fetchall()
        return {str(row["choice"]): int(row["n"]) for row in rows}

    def respondents(self, survey_id: str, *, tenant: str) -> list[str]:
        """The pseudonymous respondent set — N6's split reads exactly
        this. Never rendered with choices attached."""
        with self._conn.lock:
            rows = self._conn.db.execute(
                """SELECT principal FROM press_survey_answers
                   WHERE survey_id = ? AND tenant_id = ?
                   ORDER BY principal""",
                (survey_id, tenant),
            ).fetchall()
        return [str(row["principal"]) for row in rows]

    # -- the data subject's right --------------------------------------- #
    def erase(self, *, tenant: str, principal: str) -> int:
        erased = 0
        with self._conn.transaction() as db:
            for table in ("press_survey_answers", "press_survey_sample"):
                cursor = db.execute(
                    f"DELETE FROM {table}"
                    " WHERE tenant_id = ? AND principal = ?",
                    (tenant, principal),
                )
                erased += int(getattr(cursor, "rowcount", 0) or 0)
        return erased

    @staticmethod
    def _survey(row) -> Survey:
        return Survey(
            survey_id=row["survey_id"],
            tenant_id=row["tenant_id"],
            topic_key=row["topic_key"],
            kind=row["kind"],
            question=row["question"],
            options=tuple(
                SurveyOption(
                    key=o["key"],
                    label=o["label"],
                    contribution_id=o.get("contribution_id", ""),
                )
                for o in json.loads(row["options"])
            ),
            reason=row["reason"],
            status=row["status"],
            opened_at=datetime.fromisoformat(row["opened_at"]),
            closed_at=(
                datetime.fromisoformat(row["closed_at"])
                if row["closed_at"]
                else None
            ),
            draw_seed=(
                int(row["draw_seed"]) if row["draw_seed"] is not None else None
            ),
            brief=(json.loads(row["brief"]) if row["brief"] else None)
            or None,
        )


class SurveyDesk:
    """The laws, in one place: answering, revealing, closing."""

    def __init__(self, store: SurveyStore, pairwise=None) -> None:
        self._store = store
        self._pairwise = pairwise  # pairwise.PairwiseStore | None

    @property
    def store(self) -> SurveyStore:
        return self._store

    def answer(
        self,
        survey_id: str,
        *,
        tenant: str,
        principal: str,
        choice: str,
        learning: bool = False,
    ) -> dict:
        """One idempotent answer. The aggregate counts it; the pairwise
        row (telling-kind) is written only under ``learning`` — the
        member's consent, checked at the door."""
        survey = self._store.get(survey_id, tenant=tenant)
        if survey is None:
            raise PressError("no such survey", status=404)
        if survey.status != "open":
            raise PressError("this survey has closed", status=409)
        keys = {o.key for o in survey.options}
        if choice not in keys:
            known = ", ".join(sorted(keys))
            raise PressError(f"choice must be one of: {known}")
        standing = self._store.answer_of(
            survey_id, tenant=tenant, principal=principal
        )
        if standing is not None:
            if standing == choice:
                return self.reveal(
                    survey_id, tenant=tenant, principal=principal
                )
            raise PressError(
                "your answer already stands — the first answer is the "
                "answer",
                status=409,
            )
        self._store.record_answer(
            survey_id, tenant=tenant, principal=principal, choice=choice
        )
        if (
            learning
            and self._pairwise is not None
            and survey.kind == "telling"
        ):
            picked = next(o for o in survey.options if o.key == choice)
            other = next(o for o in survey.options if o.key != choice)
            self._pairwise.record(
                tenant=tenant,
                principal=principal,
                source="survey",
                prompt=survey.question,
                chosen=picked.label,
                rejected=other.label,
            )
        return self.reveal(survey_id, tenant=tenant, principal=principal)

    def reveal(
        self, survey_id: str, *, tenant: str, principal: str
    ) -> dict:
        """Answer first, floor second — no individual answer, ever."""
        survey = self._store.get(survey_id, tenant=tenant)
        if survey is None:
            raise PressError("no such survey", status=404)
        mine = self._store.answer_of(
            survey_id, tenant=tenant, principal=principal
        )
        if mine is None:
            return {
                "survey_id": survey_id,
                "answered": False,
                "revealed": False,
                "reason": "answer first — results follow your own answer",
            }
        counts = self._store.counts(survey_id, tenant=tenant)
        total = sum(counts.values())
        if total < SURVEY_K_FLOOR:
            return {
                "survey_id": survey_id,
                "answered": True,
                "choice": mine,
                "revealed": False,
                "reason": "not enough answers yet",
            }
        return {
            "survey_id": survey_id,
            "answered": True,
            "choice": mine,
            "revealed": True,
            "counts": counts,
            "total": total,
        }

    def close_expired(self, *, tenant: str) -> list[Survey]:
        """The tick's housekeeping: an open survey past its TTL closes,
        whatever its count — its result row stands at whatever the
        floor allows."""
        closed = []
        now = self._store.now()
        for survey in self._store.list(tenant=tenant, status="open"):
            if now - survey.opened_at >= timedelta(hours=SURVEY_TTL_HOURS):
                self._store.close(survey.survey_id, tenant=tenant)
                closed.append(survey)
        return closed

    def result_row(self, survey_id: str, *, tenant: str) -> dict:
        """The typed SOURCE ROW a composed post cites (N4): survey id,
        topic key, question, sample size, and the aggregate — floored;
        never an individual answer."""
        survey = self._store.get(survey_id, tenant=tenant)
        if survey is None:
            raise PressError("no such survey", status=404)
        counts = self._store.counts(survey_id, tenant=tenant)
        total = sum(counts.values())
        row = {
            "survey_id": survey.survey_id,
            "topic_key": survey.topic_key,
            "kind": survey.kind,
            "question": survey.question,
            "status": survey.status,
            "sample_size": len(
                self._store.sample_of(survey.survey_id, tenant=tenant)
            ),
            "answers": total,
        }
        if total >= SURVEY_K_FLOOR:
            row["counts"] = counts
        else:
            row["reason"] = "not enough answers yet"
        return row
