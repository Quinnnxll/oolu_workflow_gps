"""The topic desk and the marketplace beat (N2) — WHAT to report, on evidence.

Inside the genre desk's demand reading (N1), the second editorial
decision: deterministic TOPIC MINING over the platform's own typed
records — the marketplace beat (listings, the discount fact, the
order-book trust score, verified-buyer feedback, member lab results)
and the contribution shelf (independent voices agreeing on a subject).
The closed loop does not open: this module imports nothing outside the
press package — the gateway hands it typed rows derived from records
the platform already keeps. No web search, no model call, anywhere.

The laws, from the roadmap:

- **Every candidate carries its evidence rows.** A topic is typed
  events — a discount past the floor, a trust score in a band, the lab
  and the reviews disagreeing, a corroborated member subject — each
  with the record refs it resolves to. A brief without facts cannot be
  stored (the lineage insert-refusal law, applied to topics).
- **The disclosure travels from birth (law 3).** A topic whose seller
  runs active advertising — or whose listing IS a campaign's target —
  carries the named disclosure on the candidate itself, before any
  selection, composition, or rendering.
- **Selection is a blend of named factors** — demand (N1's reading),
  evidence weight, freshness — versioned, recorded with its breakdown,
  reproducible from its inputs.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Callable, Mapping, Sequence

from .contributions import PressError
from .standards import AGREE_MIN, agreement

TOPIC_VERSION = 1
# How many briefs a reading selects — a slate, not a firehose.
TOPICS_PER_RUN = 3
# The discount fact becomes a story at or past this floor.
PRICE_MOVE_FLOOR = 10  # percent off the recorded list price
# The trust bands: concern below, proven above — only with a real book.
TRUST_CONCERN_BAND = 0.4
TRUST_PROVEN_BAND = 0.85
TRUST_BOOK_FLOOR = 3  # orders before the score is worth words
TRUST_PROVEN_FLOOR = 5  # finished orders before "proven" is earned
# The lab and the reviews disagree when their normalized factors split
# by at least this much — and only when BOTH sides have real evidence.
MEASURED_GAP = 0.35
# A member subject corroborates when this many DISTINCT authors agree.
CLUSTER_FLOOR = 2
# The selection blend, named.
DEMAND_WEIGHT = 0.4
EVIDENCE_WEIGHT = 0.35
FRESHNESS_WEIGHT = 0.25
EVIDENCE_SATURATION = 4  # facts beyond this stop raising the factor
FRESHNESS_HALF_LIFE_HOURS = 36.0


@dataclass(frozen=True)
class MarketFact:
    """One typed evidence row — the record it resolves to, named."""

    kind: str  # "listing" | "price" | "trust" | "feedback" | "lab" | "contribution"
    ref: str  # the record's own id
    summary: str  # the fact in words, composed deterministically
    value: float | None = None


@dataclass(frozen=True)
class BeatRow:
    """One marketplace listing as the beat reads it — built by the
    gateway from the explorer's normalized comparison row, with the
    advertiser flag stamped at the door (law 3)."""

    listing_id: str
    title: str
    seller: str
    price_micros: int
    list_price_micros: int | None
    discount_percent: int | None
    feedback: dict  # {count, mean, factor}
    trust: dict  # {score, finished, refunded, disputed, basis}
    lab: dict  # {count, mean_score, factor}
    advertiser: bool = False  # the seller runs an ACTIVE campaign
    promoted: bool = False  # this listing IS a campaign's target


@dataclass(frozen=True)
class ClusterPiece:
    """One live contribution as the cluster miner reads it."""

    contribution_id: str
    author: str
    genre: str  # the piece's first genre — where the cluster files
    title: str
    text: str
    created_at: datetime


@dataclass(frozen=True)
class TopicCandidate:
    key: str  # stable: "{kind}:{anchor ref}" — mining never twins
    genre: str
    subject: str  # the topic in words
    facts: tuple[MarketFact, ...]
    fresh_at: datetime
    disclosure: str = ""  # law 3 — named, or honestly empty


@dataclass(frozen=True)
class TopicBrief:
    key: str
    genre: str
    subject: str
    facts: tuple[MarketFact, ...]
    disclosure: str
    rank: int
    score: float
    factors: dict = field(default_factory=dict)


def _disclosure(row: BeatRow) -> str:
    if row.promoted:
        return (
            f"Disclosure: this listing is the target of an active "
            f"advertising campaign by {row.seller}."
        )
    if row.advertiser:
        return (
            f"Disclosure: seller {row.seller} runs active advertising "
            "on this platform."
        )
    return ""


def _listing_fact(row: BeatRow) -> MarketFact:
    return MarketFact(
        kind="listing",
        ref=row.listing_id,
        summary=f"“{row.title}” by {row.seller}, "
        f"{row.price_micros / 1_000_000:.2f}",
        value=row.price_micros / 1_000_000,
    )


# --------------------------------------------------------------------------- #
# The miners — typed events, never a hunch.                                    #
# --------------------------------------------------------------------------- #
def mine_price_moves(
    rows: Sequence[BeatRow], *, now: datetime
) -> list[TopicCandidate]:
    """The discount FACT past the floor — a recorded list price beaten
    by the asking price; never a seller's claim."""
    found = []
    for row in rows:
        if row.discount_percent is None or row.discount_percent < PRICE_MOVE_FLOOR:
            continue
        found.append(
            TopicCandidate(
                key=f"price:{row.listing_id}",
                genre="products",
                subject=(
                    f"{row.title}: {row.discount_percent}% off its "
                    "recorded list price"
                ),
                facts=(
                    _listing_fact(row),
                    MarketFact(
                        kind="price",
                        ref=row.listing_id,
                        summary=(
                            f"list {row.list_price_micros / 1_000_000:.2f} → "
                            f"asking {row.price_micros / 1_000_000:.2f} "
                            f"({row.discount_percent}% off)"
                        ),
                        value=float(row.discount_percent),
                    ),
                ),
                fresh_at=now,
                disclosure=_disclosure(row),
            )
        )
    return found


def mine_trust_bands(
    rows: Sequence[BeatRow], *, now: datetime
) -> list[TopicCandidate]:
    """The order book speaking: a seller whose score crossed a band —
    concern or proven — and ONLY with a real book behind the number."""
    found = []
    for row in rows:
        trust = row.trust or {}
        score = trust.get("score")
        if score is None:
            continue
        book = sum(
            int(trust.get(k, 0) or 0)
            for k in ("finished", "refunded", "disputed")
        )
        fact = MarketFact(
            kind="trust",
            ref=row.listing_id,
            summary=(
                f"order-book trust {score} for {row.seller} "
                f"({trust.get('finished', 0)} finished, "
                f"{trust.get('refunded', 0)} refunded, "
                f"{trust.get('disputed', 0)} disputed)"
            ),
            value=float(score),
        )
        if score <= TRUST_CONCERN_BAND and book >= TRUST_BOOK_FLOOR:
            found.append(
                TopicCandidate(
                    key=f"trust:{row.listing_id}",
                    genre="products",
                    subject=(
                        f"{row.title}: the order book raises a trust "
                        f"concern about {row.seller}"
                    ),
                    facts=(_listing_fact(row), fact),
                    fresh_at=now,
                    disclosure=_disclosure(row),
                )
            )
        elif (
            score >= TRUST_PROVEN_BAND
            and int(trust.get("finished", 0) or 0) >= TRUST_PROVEN_FLOOR
        ):
            found.append(
                TopicCandidate(
                    key=f"trust:{row.listing_id}",
                    genre="products",
                    subject=(
                        f"{row.title}: {row.seller}'s order book has "
                        "earned a proven record"
                    ),
                    facts=(_listing_fact(row), fact),
                    fresh_at=now,
                    disclosure=_disclosure(row),
                )
            )
    return found


def mine_measured_gaps(
    rows: Sequence[BeatRow], *, now: datetime
) -> list[TopicCandidate]:
    """The lab and the reviews disagreeing — both sides with REAL
    evidence (a neutral no-evidence factor never manufactures a gap)."""
    found = []
    for row in rows:
        feedback, lab = row.feedback or {}, row.lab or {}
        if not int(feedback.get("count", 0) or 0):
            continue
        if not int(lab.get("count", 0) or 0):
            continue
        gap = abs(
            float(feedback.get("factor", 0.5)) - float(lab.get("factor", 0.5))
        )
        if gap < MEASURED_GAP:
            continue
        found.append(
            TopicCandidate(
                key=f"measured:{row.listing_id}",
                genre="results",
                subject=(
                    f"{row.title}: member lab results and verified "
                    "reviews disagree"
                ),
                facts=(
                    _listing_fact(row),
                    MarketFact(
                        kind="feedback",
                        ref=row.listing_id,
                        summary=(
                            f"verified feedback mean {feedback.get('mean')} "
                            f"over {feedback.get('count')} reviews"
                        ),
                        value=float(feedback.get("factor", 0.5)),
                    ),
                    MarketFact(
                        kind="lab",
                        ref=row.listing_id,
                        summary=(
                            f"lab mean score {lab.get('mean_score')} over "
                            f"{lab.get('count')} member reports"
                        ),
                        value=float(lab.get("factor", 0.5)),
                    ),
                ),
                fresh_at=now,
                disclosure=_disclosure(row),
            )
        )
    return found


def mine_clusters(pieces: Sequence[ClusterPiece]) -> list[TopicCandidate]:
    """Independent voices agreeing on a subject — the corroboration
    machinery lifted above the single piece. The anchor is the OLDEST
    agreeing piece (first to tell it); one candidate per cluster."""
    found: list[TopicCandidate] = []
    claimed: set[str] = set()
    for anchor in sorted(pieces, key=lambda p: (p.created_at, p.contribution_id)):
        if anchor.contribution_id in claimed:
            continue
        agreeing = [anchor]
        authors = {anchor.author}
        for other in pieces:
            if other.contribution_id == anchor.contribution_id:
                continue
            if other.author in authors or other.contribution_id in claimed:
                continue
            score = agreement(
                f"{anchor.title}\n{anchor.text}",
                f"{other.title}\n{other.text}",
            )
            if score >= AGREE_MIN:
                agreeing.append(other)
                authors.add(other.author)
        if len(authors) < CLUSTER_FLOOR:
            continue
        claimed.update(p.contribution_id for p in agreeing)
        found.append(
            TopicCandidate(
                key=f"cluster:{anchor.contribution_id}",
                genre=anchor.genre,
                subject=(
                    f"{len(authors)} members are telling the same story: "
                    f"“{anchor.title}”"
                ),
                facts=tuple(
                    MarketFact(
                        kind="contribution",
                        ref=piece.contribution_id,
                        summary=f"“{piece.title}” by {piece.author}",
                    )
                    for piece in agreeing
                ),
                fresh_at=max(p.created_at for p in agreeing),
            )
        )
    return found


# --------------------------------------------------------------------------- #
# Selection — demand, evidence, freshness; every factor named.                 #
# --------------------------------------------------------------------------- #
def select_topics(
    candidates: Sequence[TopicCandidate],
    *,
    demand_rank: Mapping[str, int],
    now: datetime,
    limit: int = TOPICS_PER_RUN,
) -> list[TopicBrief]:
    """The slate: deterministic, explained, reproducible. ``demand_rank``
    is the genre desk's standing reading (genre → rank); an unranked
    genre honestly scores zero demand."""
    ranks = {g: int(r) for g, r in demand_rank.items() if int(r) > 0}
    top = max(ranks.values(), default=0)
    scored = []
    for candidate in candidates:
        if not candidate.facts:
            continue  # a topic without evidence is not a topic
        rank = ranks.get(candidate.genre)
        demand = ((top - rank + 1) / top) if (rank and top) else 0.0
        evidence = min(1.0, len(candidate.facts) / EVIDENCE_SATURATION)
        age_hours = max(
            0.0, (now - candidate.fresh_at).total_seconds() / 3600.0
        )
        freshness = math.exp(
            -age_hours * math.log(2) / FRESHNESS_HALF_LIFE_HOURS
        )
        score = round(
            DEMAND_WEIGHT * demand
            + EVIDENCE_WEIGHT * evidence
            + FRESHNESS_WEIGHT * freshness,
            4,
        )
        factors = {
            "demand": round(demand, 4),
            "evidence": round(evidence, 4),
            "freshness": round(freshness, 4),
        }
        scored.append((score, candidate, factors))
    scored.sort(key=lambda s: (-s[0], s[1].key))
    return [
        TopicBrief(
            key=candidate.key,
            genre=candidate.genre,
            subject=candidate.subject,
            facts=candidate.facts,
            disclosure=candidate.disclosure,
            rank=index + 1,
            score=score,
            factors=factors,
        )
        for index, (score, candidate, factors) in enumerate(scored[:limit])
    ]


def topics_line(briefs: Sequence[TopicBrief]) -> str:
    """The slate in the desk's words — subjects in rank order, every
    disclosure spoken with its topic, never trimmed away."""
    if not briefs:
        return (
            "No topics on the slate yet — the beat has no events past "
            "its floors and no corroborated member subjects."
        )
    lines = ["The slate, on evidence:"]
    for brief in briefs:
        line = f"{brief.rank}. {brief.subject}"
        if brief.disclosure:
            line += f" ({brief.disclosure})"
        lines.append(line)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# The standing slate.                                                          #
# --------------------------------------------------------------------------- #
_TOPICS_SCHEMA = """CREATE TABLE IF NOT EXISTS press_topic_briefs (
    tenant_id TEXT NOT NULL,
    topic_key TEXT NOT NULL,
    genre TEXT NOT NULL,
    subject TEXT NOT NULL,
    facts TEXT NOT NULL,
    disclosure TEXT NOT NULL,
    rank INTEGER NOT NULL,
    score REAL NOT NULL,
    factors TEXT NOT NULL,
    topic_version INTEGER NOT NULL,
    computed_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, topic_key)
)"""


class TopicBriefStore:
    """The desk's standing slate — durable, tenant-scoped, replaced
    whole (never a mixed vintage). A brief without facts is REFUSED at
    the insert: provenance is mandatory for topics exactly as it is
    for story lineage."""

    def __init__(self, conn, *, clock: Callable[[], datetime] | None = None) -> None:
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_TOPICS_SCHEMA)

    def record(self, *, tenant: str, briefs: Sequence[TopicBrief]) -> None:
        for brief in briefs:
            if not brief.facts:
                raise PressError(
                    "a topic brief needs evidence rows — provenance is "
                    "mandatory"
                )
        stamp = self._clock().isoformat()
        with self._conn.transaction() as db:
            db.execute(
                "DELETE FROM press_topic_briefs WHERE tenant_id = ?",
                (tenant,),
            )
            for brief in briefs:
                db.execute(
                    """INSERT INTO press_topic_briefs
                         (tenant_id, topic_key, genre, subject, facts,
                          disclosure, rank, score, factors, topic_version,
                          computed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        tenant,
                        brief.key,
                        brief.genre,
                        brief.subject,
                        json.dumps(
                            [
                                {
                                    "kind": f.kind,
                                    "ref": f.ref,
                                    "summary": f.summary,
                                    "value": f.value,
                                }
                                for f in brief.facts
                            ]
                        ),
                        brief.disclosure,
                        brief.rank,
                        brief.score,
                        json.dumps(brief.factors),
                        TOPIC_VERSION,
                        stamp,
                    ),
                )

    def reading(self, *, tenant: str) -> list[dict]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                """SELECT * FROM press_topic_briefs
                   WHERE tenant_id = ? ORDER BY rank ASC""",
                (tenant,),
            ).fetchall()
        return [
            {
                "topic_key": row["topic_key"],
                "genre": row["genre"],
                "subject": row["subject"],
                "facts": json.loads(row["facts"]),
                "disclosure": row["disclosure"],
                "rank": int(row["rank"]),
                "score": float(row["score"]),
                "factors": json.loads(row["factors"]),
                "topic_version": int(row["topic_version"]),
                "computed_at": row["computed_at"],
            }
            for row in rows
        ]
