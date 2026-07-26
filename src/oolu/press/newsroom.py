"""The newsroom — contributions become attributed stories (A2).

A :class:`Story` is composed from a selected contribution set, and its
**lineage weights are recorded at composition time**: the anchor piece,
its corroborators, and any near-duplicate credits, each with the weight
the ad dividend (A5) will later split over. The law is the episodes.py
law, applied to prose: *a story nobody can trace is a story, not a
memory* — a Story with no resolvable contribution lineage cannot be
stored, full stop.

Composition speaks through the ``news.compose`` seat when a model is
present (the caller passes the seat's router; this module never reaches
for a provider itself — the closed loop holds). The prompt is a hard
boundary: only facts from the cited contributions, never an invented
detail. A model-less host — or a model that breaks the output contract —
falls back to the desk composition: the anchor's own words, verbatim,
credited. Degraded is honest; invented is a defect.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Callable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from .contributions import (
    MAX_BODY_CHARS,
    ContentContribution,
    ContributionStore,
    PressError,
)
from .standards import RUBRIC_VERSION, RubricBreakdown, corroborating, select

# The anchor holds this share of a story's lineage; corroborators and
# near-duplicate credits split the remainder equally. A lone anchor
# holds everything. Recorded per story — the split A5 pays over.
ANCHOR_WEIGHT = 0.6
MAX_HEADLINE_CHARS = 140
STORIES_PER_RUN = 3


class LineageShare(BaseModel):
    model_config = ConfigDict(frozen=True)

    contribution_id: str
    author: str  # denormalized so bylines render without a join
    weight: float


class Story(BaseModel):
    model_config = ConfigDict(frozen=True)

    story_id: str
    tenant_id: str
    headline: str
    prose: str
    genres: tuple[str, ...]
    lineage: tuple[LineageShare, ...]
    breakdown: dict  # the rubric's factor breakdown — why it was selected
    rubric_version: int
    source: str  # "model" (the seat composed) | "desk" (verbatim, credited)
    created_at: datetime


_STORIES_SCHEMA = """CREATE TABLE IF NOT EXISTS press_stories (
    story_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    headline TEXT NOT NULL,
    prose TEXT NOT NULL,
    genres TEXT NOT NULL,
    breakdown TEXT NOT NULL,
    rubric_version INTEGER NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL
)"""

# The lineage is its own table — queryable (which pieces are already
# told? what does the dividend split over?) rather than buried in JSON.
_LINEAGE_SCHEMA = """CREATE TABLE IF NOT EXISTS press_story_lineage (
    story_id TEXT NOT NULL,
    contribution_id TEXT NOT NULL,
    author TEXT NOT NULL,
    weight REAL NOT NULL,
    PRIMARY KEY (story_id, contribution_id)
)"""


class StoryStore:
    """Durable, tenant-scoped stories with queryable lineage."""

    def __init__(self, conn, *, clock: Callable[[], datetime] | None = None) -> None:
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_STORIES_SCHEMA)
            db.execute(_LINEAGE_SCHEMA)

    def now(self) -> datetime:
        return self._clock()

    def insert(self, story: Story) -> None:
        # The provenance law: a story with no lineage cannot be stored.
        if not story.lineage:
            raise PressError(
                "a story with no contribution lineage cannot publish — "
                "provenance is mandatory"
            )
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO press_stories
                     (story_id, tenant_id, headline, prose, genres,
                      breakdown, rubric_version, source, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    story.story_id,
                    story.tenant_id,
                    story.headline,
                    story.prose,
                    json.dumps(list(story.genres)),
                    json.dumps(story.breakdown),
                    story.rubric_version,
                    story.source,
                    story.created_at.isoformat(),
                ),
            )
            for share in story.lineage:
                db.execute(
                    """INSERT INTO press_story_lineage
                         (story_id, contribution_id, author, weight)
                       VALUES (?, ?, ?, ?)""",
                    (
                        story.story_id,
                        share.contribution_id,
                        share.author,
                        share.weight,
                    ),
                )

    def get(self, story_id: str, *, tenant: str) -> Story | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT * FROM press_stories"
                " WHERE story_id = ? AND tenant_id = ?",
                (story_id, tenant),
            ).fetchone()
        if row is None:
            return None
        return self._assemble(row)

    def list(self, *, tenant: str, limit: int = 50) -> list[Story]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                """SELECT * FROM press_stories WHERE tenant_id = ?
                   ORDER BY created_at DESC, story_id DESC LIMIT ?""",
                (tenant, int(limit)),
            ).fetchall()
        return [self._assemble(row) for row in rows]

    def cited_contribution_ids(self, *, tenant: str) -> set[str]:
        """Every contribution a standing story already cites — what the
        newsroom must not retell."""
        with self._conn.lock:
            rows = self._conn.db.execute(
                """SELECT DISTINCT l.contribution_id
                   FROM press_story_lineage l
                   JOIN press_stories s ON s.story_id = l.story_id
                   WHERE s.tenant_id = ?""",
                (tenant,),
            ).fetchall()
        return {row["contribution_id"] for row in rows}

    def _assemble(self, row) -> Story:
        with self._conn.lock:
            shares = self._conn.db.execute(
                """SELECT contribution_id, author, weight
                   FROM press_story_lineage WHERE story_id = ?
                   ORDER BY weight DESC, contribution_id""",
                (row["story_id"],),
            ).fetchall()
        return Story(
            story_id=row["story_id"],
            tenant_id=row["tenant_id"],
            headline=row["headline"],
            prose=row["prose"],
            genres=tuple(json.loads(row["genres"])),
            lineage=tuple(
                LineageShare(
                    contribution_id=s["contribution_id"],
                    author=s["author"],
                    weight=s["weight"],
                )
                for s in shares
            ),
            breakdown=json.loads(row["breakdown"]),
            rubric_version=int(row["rubric_version"]),
            source=row["source"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )


# --------------------------------------------------------------------------- #
# Composition.                                                                 #
# --------------------------------------------------------------------------- #
_COMPOSE_FRAME = """\
You are the newsroom of a member-written magazine. Compose ONE short
story from ONLY the member contributions below. Hard rules:
- Every fact must come from the contributions. Never invent a detail,
  a number, a name, or a quote. Omission is fine; invention is not.
- Write plainly and warmly, like a good local magazine.
- Answer in EXACTLY this shape (first line, then a blank line, then
  the prose):
HEADLINE: <one line, at most 120 characters>

<two to four short paragraphs>"""


def _contribution_block(records: list[ContentContribution]) -> str:
    parts = []
    for record in records:
        parts.append(
            f"--- contribution by {record.author} ---\n"
            f"{record.title}\n{record.body}"
        )
    return "\n\n".join(parts)


def _desk_composition(anchor: ContentContribution, others: int) -> tuple[str, str]:
    """The model-less composition: the anchor's own words, verbatim —
    degraded is honest. The corroboration note states a real count."""
    prose = anchor.body
    if others:
        prose += (
            f"\n\n({others} other member"
            f"{'s' if others != 1 else ''} contributed to this story.)"
        )
    return anchor.title[:MAX_HEADLINE_CHARS], prose[:MAX_BODY_CHARS]


def compose_story_text(
    anchor: ContentContribution,
    cited: list[ContentContribution],
    *,
    model=None,  # chat.ChatModel | None — the news.compose seat's router
) -> tuple[str, str, str]:
    """``(headline, prose, source)`` — the seat composes when it can and
    keeps the output contract; the desk composes otherwise."""
    if model is not None:
        from ..chat import ModelBudgetExceeded, ModelUnavailable

        try:
            raw = model.reply(
                [
                    {"role": "system", "content": _COMPOSE_FRAME},
                    {
                        "role": "user",
                        "content": _contribution_block([anchor, *cited]),
                    },
                ]
            )
        except (ModelUnavailable, ModelBudgetExceeded):
            raw = ""
        headline, prose = _parse_composed(raw)
        if headline and prose:
            return headline, prose, "model"
    headline, prose = _desk_composition(anchor, len(cited))
    return headline, prose, "desk"


def _parse_composed(raw: str) -> tuple[str, str]:
    """The output contract, checked — a reply outside it composes
    nothing (the desk takes over), never a half-parsed story."""
    text = str(raw or "").strip()
    if not text.upper().startswith("HEADLINE:"):
        return "", ""
    first, _, rest = text.partition("\n")
    headline = first[len("HEADLINE:") :].strip()[:MAX_HEADLINE_CHARS]
    prose = rest.strip()[:MAX_BODY_CHARS]
    if not headline or not prose:
        return "", ""
    return headline, prose


def lineage_shares(
    anchor: ContentContribution, cited: list[ContentContribution]
) -> tuple[LineageShare, ...]:
    """The attribution set, weighted at composition time — anchor first,
    the rest equal, summing to 1.0 exactly (the last share absorbs the
    rounding remainder, the integer-micros discipline in miniature)."""
    if not cited:
        return (
            LineageShare(
                contribution_id=anchor.contribution_id,
                author=anchor.author,
                weight=1.0,
            ),
        )
    rest = round((1.0 - ANCHOR_WEIGHT) / len(cited), 4)
    shares = [
        LineageShare(
            contribution_id=anchor.contribution_id,
            author=anchor.author,
            weight=ANCHOR_WEIGHT,
        )
    ]
    for i, record in enumerate(cited):
        weight = rest
        if i == len(cited) - 1:
            weight = round(1.0 - ANCHOR_WEIGHT - rest * (len(cited) - 1), 4)
        shares.append(
            LineageShare(
                contribution_id=record.contribution_id,
                author=record.author,
                weight=weight,
            )
        )
    return tuple(shares)


class Newsroom:
    """The composition pass: judge the shelf, tell the untold, record
    the lineage. Idempotent by construction — a contribution a standing
    story already cites never anchors or joins another story."""

    def __init__(
        self, contributions: ContributionStore, stories: StoryStore
    ) -> None:
        self._contributions = contributions
        self._stories = stories

    def run(
        self,
        *,
        tenant: str,
        model=None,
        limit: int = STORIES_PER_RUN,
    ) -> list[Story]:
        corpus = self._contributions.list(tenant=tenant, limit=500)
        told = self._stories.cited_contribution_ids(tenant=tenant)
        selected = select(
            corpus,
            now=self._stories.now(),
            limit=limit,
            exclude=told,
        )
        composed: list[Story] = []
        for anchor, breakdown in selected:
            # A piece consumed as a corroborator earlier in this same
            # pass is told — it never retells itself as its own anchor.
            if anchor.contribution_id in told:
                continue
            cited = [
                c
                for c in corroborating(anchor, corpus)
                if c.contribution_id not in told
            ]
            # A flagged retelling of the anchor is credit, not a source
            # of new facts — it joins the lineage all the same.
            credits = [
                c
                for c in corpus
                if c.similar_to == anchor.contribution_id
                and c.contribution_id not in told
                and all(x.contribution_id != c.contribution_id for x in cited)
            ]
            headline, prose, source = compose_story_text(
                anchor, cited, model=model
            )
            story = Story(
                story_id=str(uuid4()),
                tenant_id=tenant,
                headline=headline,
                prose=prose,
                genres=anchor.genres,
                lineage=lineage_shares(anchor, [*cited, *credits]),
                breakdown=breakdown.as_dict(),
                rubric_version=RUBRIC_VERSION,
                source=source,
                created_at=self._stories.now(),
            )
            self._stories.insert(story)
            told.update(s.contribution_id for s in story.lineage)
            composed.append(story)
        return composed
