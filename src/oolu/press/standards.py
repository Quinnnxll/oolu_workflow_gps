"""The magazine rubric — editorial selection as a typed, versioned policy.

Plan invariant 3 (agents-expansion): selection is explainable or it does
not happen. The three magazine virtues are NAMED scores over typed
signals, every selection records its full factor breakdown and the
rubric version it was judged under, and nothing here reads a
self-declared quality field — the `REWARD_PRICING_DESIGN.md` lesson,
applied to prose:

- **Inspiring** — freshness: novelty against the standing corpus and
  recency. New material, newly told.
- **Critical** — corroboration: independent members (distinct authors)
  telling the same story. One voice is a claim; several are a report.
- **Knowledgeable** — depth: enough words to actually inform, and
  attached evidence (media) counts.

This is rubric VERSION 1 — deliberately simple, deterministic mappings.
Verified engagement joins the signals when the preference store has had
time to observe real reading (A3); the version number is what lets that
happen without silently re-judging old selections.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from .contributions import ContentContribution

RUBRIC_VERSION = 1

# Corroboration: this many INDEPENDENT agreeing authors earn the full
# critical score (decision-log item 5 fixes the number for publication;
# this is the working default).
CORROBORATION_FLOOR = 2
# The agreement band: enough CONTENT-token overlap to be the same
# subject, below the near-duplicate flag (which is credit, not
# corroboration). Agreement is judged on content words only — raw
# Jaccard lets stopwords ("the, this, morning, with") make strangers
# agree, which is exactly the false corroboration a critical score
# cannot afford.
AGREE_MIN = 0.25
# Depth: a body this long earns the full depth score.
DEPTH_CHARS = 1_200
# Recency half-life: a piece this old scores half on recency.
RECENCY_HALF_LIFE_HOURS = 36.0


@dataclass(frozen=True)
class RubricBreakdown:
    """Why a piece was selected — the record the reasons render from."""

    inspiring: float
    critical: float
    knowledgeable: float
    selection: float  # the blended score the ordering uses
    novelty: float
    recency: float
    depth: float
    corroborators: int  # distinct OTHER authors telling the same story
    rubric_version: int = RUBRIC_VERSION

    def as_dict(self) -> dict:
        return {
            "inspiring": self.inspiring,
            "critical": self.critical,
            "knowledgeable": self.knowledgeable,
            "selection": self.selection,
            "novelty": self.novelty,
            "recency": self.recency,
            "depth": self.depth,
            "corroborators": self.corroborators,
            "rubric_version": self.rubric_version,
        }


def _text(record: ContentContribution) -> str:
    return f"{record.title}\n{record.body}"


_WORD = re.compile(r"[A-Za-z0-9_]+")
# The short-and-common words that carry no subject. Small on purpose:
# agreement needs the nouns and verbs of the story, not a linguistics
# model.
_STOP = frozenset(
    "this that with from were have been they them then than there their "
    "which would could should about into over down under only also more "
    "most very when where while what whose during between after before "
    "because through against among".split()
)


def _content_tokens(text: str) -> set[str]:
    return {
        t.lower()
        for t in _WORD.findall(text)
        if len(t) >= 4 and t.lower() not in _STOP
    }


def agreement(a: str, b: str) -> float:
    """Content-token Jaccard: what the two texts are ABOUT, shared."""
    ta, tb = _content_tokens(a), _content_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def corroborating(
    anchor: ContentContribution, corpus: list[ContentContribution]
) -> list[ContentContribution]:
    """The independent agreeing set: same subject (overlap in the band),
    a DIFFERENT author, and at most one piece per author — a member
    retelling themselves three times is still one voice."""
    seen_authors = {anchor.author}
    agreeing: list[ContentContribution] = []
    for other in corpus:
        if other.contribution_id == anchor.contribution_id:
            continue
        if other.author in seen_authors:
            continue
        if agreement(_text(anchor), _text(other)) >= AGREE_MIN:
            agreeing.append(other)
            seen_authors.add(other.author)
    return agreeing


def score(
    anchor: ContentContribution,
    corpus: list[ContentContribution],
    *,
    now: datetime,
) -> RubricBreakdown:
    """Judge one piece against the standing corpus. Pure and typed: the
    same inputs always produce the same breakdown."""
    # Novelty: how unlike everything OLDER it is. The agreeing band and
    # the near-duplicate flag pull the other way on purpose — the
    # tension is visible in the breakdown, never hidden in a blend.
    older = [
        c
        for c in corpus
        if c.contribution_id != anchor.contribution_id
        and c.created_at <= anchor.created_at
    ]
    novelty = 1.0 - max(
        (agreement(_text(anchor), _text(c)) for c in older), default=0.0
    )
    age_hours = max(0.0, (now - anchor.created_at).total_seconds() / 3600.0)
    recency = 0.5 ** (age_hours / RECENCY_HALF_LIFE_HOURS)
    depth = min(1.0, len(anchor.body) / DEPTH_CHARS)
    evidence = 1.0 if anchor.media else 0.0
    agreeing = corroborating(anchor, corpus)

    inspiring = 0.6 * novelty + 0.4 * recency
    critical = min(1.0, len(agreeing) / CORROBORATION_FLOOR)
    knowledgeable = 0.7 * depth + 0.3 * evidence
    selection = 0.4 * inspiring + 0.3 * critical + 0.3 * knowledgeable
    return RubricBreakdown(
        inspiring=round(inspiring, 4),
        critical=round(critical, 4),
        knowledgeable=round(knowledgeable, 4),
        selection=round(selection, 4),
        novelty=round(novelty, 4),
        recency=round(recency, 4),
        depth=round(depth, 4),
        corroborators=len(agreeing),
    )


def select(
    corpus: list[ContentContribution],
    *,
    now: datetime,
    limit: int,
    exclude: set[str] = frozenset(),
) -> list[tuple[ContentContribution, RubricBreakdown]]:
    """The editor's pass: judge every eligible piece, best first.

    A flagged near-duplicate never anchors its own story — it folds into
    the original's lineage as credit. ``exclude`` names contributions
    already cited by standing stories, so the newsroom never retells
    what it already told.
    """
    anchors = [
        c
        for c in corpus
        if c.similar_to is None and c.contribution_id not in exclude
    ]
    judged = [(c, score(c, corpus, now=now)) for c in anchors]
    judged.sort(
        key=lambda pair: (pair[1].selection, pair[0].created_at.isoformat()),
        reverse=True,
    )
    return judged[: int(limit)]
