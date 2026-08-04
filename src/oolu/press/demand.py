"""The genre desk (N1, v2) — which genre are readers interested in, on evidence.

The first editorial decision of the benchmark pipeline
(docs/news-agent-benchmark-roadmap.md): a ranking of the taxonomy's
genres from three typed inputs, every factor named, no model call
anywhere in the decision:

- **engagement** — the N0 measuring stick rolled up per genre, read as
  a POSTERIOR, not a point: the completion evidence is a Beta over
  (completions, opens) and the desk THOMPSON-SAMPLES it. A cold genre
  draws from the wide prior — cold start is exploration by
  construction — and a genre with unlucky early numbers keeps earning
  re-tests as long as its posterior stays wide, so the desk never
  locks into a cumulative suboptimal choice. Below the reader floor
  the row is flagged ``explored``: ranked on a draw, honestly named.
- **interest** — the anonymous genre taps from the News thread. A tap
  names a stream, not a member: the counter stores no principal at all,
  so there is nothing to consent to and nothing to erase.
- **supply** — live contributions per genre. A stream with no material
  is a stream the newsroom cannot serve, however loved.

Version 2's amendment (the route/desk doctrine): the ROUTE between
desks stays deterministic; the decision INSIDE this desk samples — and
stays auditable, because every reading records its draw seed, so the
exact ranking replays from the stored inputs plus the stored seed.
Reproducible-given-the-record is the audit; a desk that can never
explore is the defect. Callers that need the deterministic reading
(rng=None) get the posterior MEAN — same factors, no draw.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable, Mapping, Sequence

DEMAND_VERSION = 2
# Below this many distinct readers in a genre, its posterior is wide
# enough that the row is honestly EXPLORING. Deliberately the metrics
# floor's number.
DEMAND_READER_FLOOR = 5
# The blend, named. Engagement leads; spoken interest and supply steer.
ENGAGEMENT_WEIGHT = 0.5
INTEREST_WEIGHT = 0.3
SUPPLY_WEIGHT = 0.2
# Inside the engagement factor: the sampled completion posterior leads;
# lingering and liking steer.
COMPLETION_WEIGHT = 0.6
DWELL_WEIGHT = 0.25
LIKES_WEIGHT = 0.15


@dataclass(frozen=True)
class GenreEvidence:
    """One genre's reading evidence, already aggregated — per-member
    rows never leave the metrics store."""

    readers: int = 0  # distinct principals who opened a story here
    opens: int = 0  # receipts (one per story+reader)
    completions: int = 0
    dwell_ms: int = 0  # summed over receipts
    likes: int = 0
    pushed: int = 0  # deliveries (N5) — the report's denominator


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
    rng: random.Random | None = None,
) -> list[GenreDemand]:
    """The ranking. With ``rng`` (the desk's standard posture) the
    completion evidence is THOMPSON-SAMPLED from its Beta posterior —
    exploration by construction, replayable from the recorded seed.
    With ``rng=None`` the posterior MEAN ranks instead: the
    deterministic reading for callers that need one. Either way the
    same named factors, the same recorded breakdowns."""
    max_taps = max((int(taps.get(g, 0)) for g in genres), default=0)
    max_supply = max((int(supply.get(g, 0)) for g in genres), default=0)
    mean_dwell = {
        g: engagement[g].dwell_ms / engagement[g].opens
        for g in genres
        if g in engagement and engagement[g].opens > 0
    }
    max_dwell = max(mean_dwell.values(), default=0.0)

    scored: list[tuple[float, str, bool, dict, dict]] = []
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
            "pushed": ev.pushed,
            "taps": int(taps.get(genre, 0)),
            "pieces": int(supply.get(genre, 0)),
        }
        # The completion posterior: Beta(completions+1, misses+1). A
        # cold genre is the uniform prior — the widest honest claim.
        alpha = ev.completions + 1
        beta = max(ev.opens - ev.completions, 0) + 1
        completion = (
            rng.betavariate(alpha, beta)
            if rng is not None
            else alpha / (alpha + beta)
        )
        dwell = (
            mean_dwell.get(genre, 0.0) / max_dwell if max_dwell > 0 else 0.0
        )
        likes = min(1.0, ev.likes / ev.readers) if ev.readers else 0.0
        engagement_factor = round(
            COMPLETION_WEIGHT * completion
            + DWELL_WEIGHT * dwell
            + LIKES_WEIGHT * likes,
            4,
        )
        score = round(
            ENGAGEMENT_WEIGHT * engagement_factor
            + INTEREST_WEIGHT * interest
            + SUPPLY_WEIGHT * pieces,
            4,
        )
        factors = {
            "engagement": engagement_factor,
            "interest": round(interest, 4),
            "supply": round(pieces, 4),
        }
        # Below the reader floor the posterior is wide: the rank rests
        # on a draw more than a record, and the row says so.
        scored.append(
            (score, genre, ev.readers < reader_floor, factors, evidence)
        )

    scored.sort(key=lambda s: (-s[0], s[1]))
    return [
        GenreDemand(
            genre=genre,
            rank=index + 1,
            score=score,
            explored=explored,
            factors=factors,
            evidence=evidence,
        )
        for index, (score, genre, explored, factors, evidence) in enumerate(
            scored
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
    draw_seed INTEGER,
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
        self._migrate_seed_column()

    def _migrate_seed_column(self) -> None:
        # Tables born before v2's recorded draws lack the seed column;
        # old readings replay as what they were — deterministic.
        try:
            with self._conn.transaction() as db:
                db.execute("SELECT draw_seed FROM press_genre_demand LIMIT 1")
            return
        except Exception:
            pass
        with self._conn.transaction() as db:
            db.execute(
                "ALTER TABLE press_genre_demand ADD COLUMN draw_seed INTEGER"
            )

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
    def record(
        self,
        *,
        tenant: str,
        items: Sequence[GenreDemand],
        seed: int | None = None,
    ) -> None:
        """One whole reading replaces the last — the desk's standing
        answer, never a mixed vintage. The DRAW SEED rides every row:
        a sampled reading replays exactly from its stored inputs plus
        its stored seed — auditability without forbidding exploration."""
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
                          factors, evidence, demand_version, computed_at,
                          draw_seed)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                        seed,
                    ),
                )

    def reading(self, *, tenant: str) -> list[dict]:
        """The standing reading, rank order — every row carries its
        breakdown (and the reading's draw seed), so the decision
        explains AND replays itself."""
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
                "draw_seed": (
                    int(row["draw_seed"])
                    if row["draw_seed"] is not None
                    else None
                ),
            }
            for row in rows
        ]
