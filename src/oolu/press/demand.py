"""The genre desk (N1) — which genre are readers interested in, on evidence.

The first editorial decision of the benchmark pipeline
(docs/news-agent-benchmark-roadmap.md): a deterministic ranking of the
taxonomy's genres from three typed inputs, every factor named, no model
call anywhere in the decision:

- **engagement** — the N0 measuring stick rolled up per genre: distinct
  readers, completion rate, mean dwell, likes-per-reader. A genre below
  the reader floor has NO engagement factor — an honest "unevidenced",
  never a number faked from two receipts.
- **interest** — the anonymous genre taps from the News thread. A tap
  names a stream, not a member: the counter stores no principal at all,
  so there is nothing to consent to and nothing to erase.
- **supply** — live contributions per genre. A stream with no material
  is a stream the newsroom cannot serve, however loved.

The blend is versioned and recorded with every reading, so a ranking is
reproducible from its stored inputs (the rubric discipline, applied to
demand). Exploration is principled and bounded: the best UNEVIDENCED
genre is promoted to second place — one trial slot, chosen
deterministically (score, then name), never an editor's hunch and never
a random draw.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable, Mapping, Sequence

DEMAND_VERSION = 1
# Below this many distinct readers in a genre, its engagement factor is
# honestly absent. Deliberately the metrics floor's number.
DEMAND_READER_FLOOR = 5
# The blend, named. Engagement leads; spoken interest and supply steer.
ENGAGEMENT_WEIGHT = 0.5
INTEREST_WEIGHT = 0.3
SUPPLY_WEIGHT = 0.2
# Inside the engagement factor: finishing beats lingering beats liking.
COMPLETION_WEIGHT = 0.5
DWELL_WEIGHT = 0.3
LIKES_WEIGHT = 0.2


@dataclass(frozen=True)
class GenreEvidence:
    """One genre's reading evidence, already aggregated — per-member
    rows never leave the metrics store."""

    readers: int = 0  # distinct principals who opened a story here
    opens: int = 0  # receipts (one per story+reader)
    completions: int = 0
    dwell_ms: int = 0  # summed over receipts
    likes: int = 0


@dataclass(frozen=True)
class GenreDemand:
    genre: str
    rank: int
    score: float
    explored: bool  # ranked by the exploration rule (no engagement)
    factors: dict  # engagement (or None), interest, supply
    evidence: dict  # the raw counts the factors came from


def rank_demand(
    *,
    genres: Sequence[str],
    engagement: Mapping[str, GenreEvidence],
    taps: Mapping[str, int],
    supply: Mapping[str, int],
    reader_floor: int = DEMAND_READER_FLOOR,
) -> list[GenreDemand]:
    """The deterministic ranking. Same inputs, same order, same
    breakdowns — reproducibility IS the audit."""
    max_taps = max((int(taps.get(g, 0)) for g in genres), default=0)
    max_supply = max((int(supply.get(g, 0)) for g in genres), default=0)
    evidenced_mean_dwell = {
        g: engagement[g].dwell_ms / engagement[g].opens
        for g in genres
        if g in engagement
        and engagement[g].readers >= reader_floor
        and engagement[g].opens > 0
    }
    max_dwell = max(evidenced_mean_dwell.values(), default=0.0)

    scored: list[tuple[bool, float, str, dict, dict]] = []
    for genre in genres:
        ev = engagement.get(genre, GenreEvidence())
        interest = (
            int(taps.get(genre, 0)) / max_taps if max_taps > 0 else 0.0
        )
        pieces = (
            int(supply.get(genre, 0)) / max_supply if max_supply > 0 else 0.0
        )
        evidence = {
            "readers": ev.readers,
            "opens": ev.opens,
            "completions": ev.completions,
            "likes": ev.likes,
            "taps": int(taps.get(genre, 0)),
            "pieces": int(supply.get(genre, 0)),
        }
        if ev.readers >= reader_floor and ev.opens > 0:
            completion = ev.completions / ev.opens
            dwell = (
                evidenced_mean_dwell.get(genre, 0.0) / max_dwell
                if max_dwell > 0
                else 0.0
            )
            likes = min(1.0, ev.likes / ev.readers)
            engagement_factor = round(
                COMPLETION_WEIGHT * completion
                + DWELL_WEIGHT * dwell
                + LIKES_WEIGHT * likes,
                4,
            )
            score = (
                ENGAGEMENT_WEIGHT * engagement_factor
                + INTEREST_WEIGHT * interest
                + SUPPLY_WEIGHT * pieces
            )
            factors = {
                "engagement": engagement_factor,
                "interest": round(interest, 4),
                "supply": round(pieces, 4),
            }
            scored.append((True, round(score, 4), genre, factors, evidence))
        else:
            # Unevidenced: the engagement term is honestly ABSENT, the
            # partial score ranks only among fellow explorers.
            score = INTEREST_WEIGHT * interest + SUPPLY_WEIGHT * pieces
            factors = {
                "engagement": None,
                "interest": round(interest, 4),
                "supply": round(pieces, 4),
            }
            scored.append((False, round(score, 4), genre, factors, evidence))

    evidenced = sorted(
        (s for s in scored if s[0]), key=lambda s: (-s[1], s[2])
    )
    explorers = sorted(
        (s for s in scored if not s[0]), key=lambda s: (-s[1], s[2])
    )
    # The one bounded trial slot: the best explorer takes SECOND place
    # when the floor has evidence to lead with; a floor with no evidence
    # at all is exploration end to end.
    ordered: list[tuple[bool, float, str, dict, dict]] = []
    if evidenced and explorers:
        trial = explorers.pop(0)
        ordered = [evidenced[0], trial, *evidenced[1:], *explorers]
    else:
        ordered = [*evidenced, *explorers]
    return [
        GenreDemand(
            genre=genre,
            rank=index + 1,
            score=score,
            explored=not has_evidence,
            factors=factors,
            evidence=evidence,
        )
        for index, (has_evidence, score, genre, factors, evidence) in enumerate(
            ordered
        )
    ]


def demand_line(
    items: Sequence[GenreDemand], label_of: Mapping[str, str], limit: int = 3
) -> str:
    """The reading, in the desk's words — the top of the order with the
    trial slot named for what it is."""
    if not items:
        return "No demand reading yet — the streams are unmeasured."
    parts = []
    for item in items[:limit]:
        name = label_of.get(item.genre, item.genre)
        parts.append(f"{name} (trial)" if item.explored else name)
    return "What readers lean toward now: " + ", ".join(parts) + "."


_DEMAND_SCHEMA = """CREATE TABLE IF NOT EXISTS press_genre_demand (
    tenant_id TEXT NOT NULL,
    genre TEXT NOT NULL,
    rank INTEGER NOT NULL,
    score REAL NOT NULL,
    explored INTEGER NOT NULL,
    factors TEXT NOT NULL,
    evidence TEXT NOT NULL,
    demand_version INTEGER NOT NULL,
    computed_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, genre)
)"""

# The anonymous interest counter: a tap names a STREAM, never a member.
_INTEREST_SCHEMA = """CREATE TABLE IF NOT EXISTS press_genre_interest (
    tenant_id TEXT NOT NULL,
    genre TEXT NOT NULL,
    taps INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, genre)
)"""


class GenreDemandStore:
    """The desk's standing reading and the anonymous interest book —
    durable, tenant-scoped. Nothing in here names a member."""

    def __init__(self, conn, *, clock: Callable[[], datetime] | None = None) -> None:
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_DEMAND_SCHEMA)
            db.execute(_INTEREST_SCHEMA)

    # -- the interest book ---------------------------------------------- #
    def tap(self, *, tenant: str, genre: str) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO press_genre_interest (tenant_id, genre, taps)
                   VALUES (?, ?, 1)
                   ON CONFLICT (tenant_id, genre) DO UPDATE SET
                     taps = press_genre_interest.taps + 1""",
                (tenant, genre),
            )

    def taps(self, *, tenant: str) -> dict[str, int]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT genre, taps FROM press_genre_interest"
                " WHERE tenant_id = ?",
                (tenant,),
            ).fetchall()
        return {str(row["genre"]): int(row["taps"]) for row in rows}

    # -- the standing reading ------------------------------------------- #
    def record(self, *, tenant: str, items: Sequence[GenreDemand]) -> None:
        """One whole reading replaces the last — the desk's standing
        answer, never a mixed vintage."""
        stamp = self._clock().isoformat()
        with self._conn.transaction() as db:
            db.execute(
                "DELETE FROM press_genre_demand WHERE tenant_id = ?",
                (tenant,),
            )
            for item in items:
                db.execute(
                    """INSERT INTO press_genre_demand
                         (tenant_id, genre, rank, score, explored,
                          factors, evidence, demand_version, computed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        tenant,
                        item.genre,
                        item.rank,
                        item.score,
                        1 if item.explored else 0,
                        json.dumps(item.factors),
                        json.dumps(item.evidence),
                        DEMAND_VERSION,
                        stamp,
                    ),
                )

    def reading(self, *, tenant: str) -> list[dict]:
        """The standing reading, rank order — every row carries its
        breakdown, so the decision explains itself."""
        with self._conn.lock:
            rows = self._conn.db.execute(
                """SELECT * FROM press_genre_demand
                   WHERE tenant_id = ? ORDER BY rank ASC""",
                (tenant,),
            ).fetchall()
        return [
            {
                "genre": row["genre"],
                "rank": int(row["rank"]),
                "score": float(row["score"]),
                "explored": bool(row["explored"]),
                "factors": json.loads(row["factors"]),
                "evidence": json.loads(row["evidence"]),
                "demand_version": int(row["demand_version"]),
                "computed_at": row["computed_at"],
            }
            for row in rows
        ]
