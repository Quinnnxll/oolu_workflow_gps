"""Editions — the per-member ranking, and the signals that shape it (A2).

Two laws govern this module:

1. **Consent shapes ranking; its absence shapes nothing.** A member with
   ``press.personalize`` off gets the NEUTRAL edition (pure rubric
   order) and their taps are never recorded — off by default, the
   ADR-0005 discipline. The consent check itself lives at the doors;
   this module takes the affinity it is given, or None.
2. **The serendipity slice.** A personalized edition always reserves its
   last slot for the best story OUTSIDE the member's leaning — the
   bandit-exploration house pattern as an editorial rule, so preference
   ranking never collapses into an echo chamber.

The preference store is the generic signal table the plan promises A3
will formalize into pairwise instruments; A2 writes the unary signals
(like / read / skip) the feedback doors deliver.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Callable

from .newsroom import Story

# The pulse sentinel: a schedule with this goal fires the member's
# edition instead of a workflow run (the MORNING_PULSE_GOAL pattern).
EDITION_PULSE_GOAL = "press.morning_edition"
EDITION_LABEL = "Morning edition"

EDITION_SIZE = 5
# How strongly affinity bends the neutral order. Bounded on purpose: a
# loved genre rises; it never makes the rubric irrelevant.
AFFINITY_PULL = 0.5

# Signal weights: a like speaks loudest, a read counts, a skip pushes
# away. The affinity read is bounded per genre to [-1, 1].
_SIGNAL_WEIGHT = {"like": 1.0, "read": 0.4, "skip": -1.0}
_AFFINITY_SCAN = 500  # the most recent signals shape the leaning

_PREFS_SCHEMA = """CREATE TABLE IF NOT EXISTS press_preferences (
    tenant_id TEXT NOT NULL,
    principal TEXT NOT NULL,
    signal TEXT NOT NULL,
    subject TEXT NOT NULL,
    genres TEXT NOT NULL,
    at TEXT NOT NULL
)"""


class PreferenceStore:
    """The member's own signals — consented, revocable-forward, and
    readable only as THEIR ranking input. Nothing aggregates across
    members here; the k-anonymous public statistics are A3's poll
    machinery, not this table."""

    def __init__(self, conn, *, clock: Callable[[], datetime] | None = None) -> None:
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_PREFS_SCHEMA)

    def record(
        self,
        *,
        tenant: str,
        principal: str,
        signal: str,
        subject: str,
        genres: tuple[str, ...] | list[str] = (),
    ) -> None:
        if signal not in _SIGNAL_WEIGHT:
            known = ", ".join(sorted(_SIGNAL_WEIGHT))
            raise ValueError(f"unknown signal: {signal!r} — one of: {known}")
        import json

        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO press_preferences
                     (tenant_id, principal, signal, subject, genres, at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    tenant,
                    principal,
                    signal,
                    str(subject),
                    json.dumps(list(genres)),
                    self._clock().isoformat(),
                ),
            )

    def erase(self, *, tenant: str, principal: str) -> int:
        """Revocation and account erasure: the member's signals, gone."""
        with self._conn.transaction() as db:
            cursor = db.execute(
                "DELETE FROM press_preferences"
                " WHERE tenant_id = ? AND principal = ?",
                (tenant, principal),
            )
        return int(getattr(cursor, "rowcount", 0) or 0)

    def genre_affinity(self, *, tenant: str, principal: str) -> dict[str, float]:
        """The member's leaning, per genre, bounded to [-1, 1]."""
        import json

        with self._conn.lock:
            rows = self._conn.db.execute(
                """SELECT signal, genres FROM press_preferences
                   WHERE tenant_id = ? AND principal = ?
                   ORDER BY at DESC LIMIT ?""",
                (tenant, principal, _AFFINITY_SCAN),
            ).fetchall()
        totals: dict[str, float] = {}
        counts: dict[str, int] = {}
        for row in rows:
            weight = _SIGNAL_WEIGHT.get(row["signal"], 0.0)
            for genre in json.loads(row["genres"] or "[]"):
                totals[genre] = totals.get(genre, 0.0) + weight
                counts[genre] = counts.get(genre, 0) + 1
        return {
            genre: max(-1.0, min(1.0, totals[genre] / max(counts[genre], 1)))
            for genre in totals
        }


# --------------------------------------------------------------------------- #
# Ranking.                                                                     #
# --------------------------------------------------------------------------- #
def _neutral_key(story: Story) -> tuple:
    return (
        float(story.breakdown.get("selection", 0.0)),
        story.created_at.isoformat(),
    )


def _affinity_of(story: Story, affinity: dict[str, float]) -> float:
    scores = [affinity[g] for g in story.genres if g in affinity]
    return sum(scores) / len(scores) if scores else 0.0


def rank_edition(
    stories: list[Story],
    *,
    affinity: dict[str, float] | None = None,
    size: int = EDITION_SIZE,
) -> list[Story]:
    """The edition: neutral rubric order, bent (bounded) by consented
    affinity — with the serendipity slice holding the last slot."""
    neutral = sorted(stories, key=_neutral_key, reverse=True)
    if not affinity:
        return neutral[: int(size)]
    ranked = sorted(
        stories,
        key=lambda s: (
            float(s.breakdown.get("selection", 0.0))
            * (1.0 + AFFINITY_PULL * _affinity_of(s, affinity)),
            s.created_at.isoformat(),
        ),
        reverse=True,
    )
    edition = ranked[: int(size)]
    # The slice: the member's leaning (their positive genres) must not
    # be the whole edition. If every chosen story leans in, the best
    # story OUTSIDE the leaning takes the last slot.
    leaning = {g for g, v in affinity.items() if v > 0}
    if leaning and edition:
        outside = [
            s for s in neutral if not (set(s.genres) & leaning)
        ]
        if outside and all(set(s.genres) & leaning for s in edition):
            edition = [*edition[:-1], outside[0]]
    return edition


def edition_message(edition: list[Story], *, skipped: int = 0) -> str:
    """The News thread's own words: headlines and bylines, the pulse's
    skipped count named — never a fabricated backlog."""
    if not edition:
        return (
            "Good morning — nothing new on the shelf today. Contributions "
            "compose into stories as members publish them."
        )
    lines = ["Good morning — your edition:"]
    for i, story in enumerate(edition, start=1):
        authors = ", ".join(
            dict.fromkeys(share.author for share in story.lineage)
        )
        lines.append(f"{i}. {story.headline} — by {authors}")
    if skipped:
        lines.append(
            f"(Catching up: {skipped} earlier edition"
            f"{'s were' if skipped != 1 else ' was'} missed while this "
            "host was asleep.)"
        )
    return "\n".join(lines)
