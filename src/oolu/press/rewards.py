"""The reward desk (N6) — revenue flows back over the FULL source table.

A settled post's money no longer stops at contributor lineage: every
class of member work the post was composed from earns its tranche —

- **contributors** (the standing lineage weights, recorded at
  composition) carry the lineage tranche;
- **survey respondents** (the retained pseudonymous respondent set of
  every survey the post cites) split the survey tranche evenly;
- **research-source members** (whoever's lab result or verified review
  anchored a cited claim, resolved LIVE at settle time) split the
  research tranche evenly.

The laws, inherited from A5 and extended:

- **One conserved split, no parallel pipeline.** ``source_split``
  produces plain (principal, weight) mass summing to 1.0 (the last
  share absorbs rounding) — tranches renormalize over the classes
  actually present, so a post with lineage alone pays exactly what A5
  always paid — and the money still moves through the standing
  ``PricingEngine`` / ``EarningsLedger`` / double-entry recognition,
  untouched: conservation TO THE MICRO is the engine's structural
  law, never this table's float arithmetic.
- **Every payout row cites its source row.** Each share carries a
  ``<kind>:<ref>`` citation resolvable to the post's stored provenance
  (a lineage row or a ``press_story_sources`` row), and the citations
  land beside the ledger in :class:`RewardCitationStore` — the
  provenance surface and the ledger agree.
- **Erasure stops future shares by construction.** Respondent rows are
  deleted with the member's account and research rows resolve live, so
  the NEXT settle simply no longer finds them — while settled history
  (money and citations alike) stays balanced, the standing financial-
  record retention.
- **Fully deterministic, so it opts out of the route** (the desk
  doctrine): certain inputs, certain split — library code, no seat,
  no draw, no model.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from pydantic import BaseModel, ConfigDict

from .newsroom import LineageShare

REWARDS_VERSION = 1

# The tranches: how the pool divides across the classes of source work.
# Renormalized over the classes PRESENT on the post — an absent class
# donates its tranche proportionally, never silently to the platform.
TRANCHE_LINEAGE = 0.6
TRANCHE_SURVEY = 0.25
TRANCHE_RESEARCH = 0.15

# The research classes the split recognizes — a topic brief's measured
# claims cite exactly these kinds against a listing.
_RESEARCH_KINDS = ("lab", "feedback")

# Weight precision: fine enough that a 6-decimal lineage weight passes
# through unchanged; the LAST share absorbs the rounding remainder
# (the lineage_from discipline). Money conserves in the engine's
# integer micros regardless of this table's float dust.
_PRECISION = 9


class SourceShare(BaseModel):
    """One payout line before money: who, their fraction of the whole,
    and the source row the payment cites."""

    model_config = ConfigDict(frozen=True)

    principal: str
    weight: float
    citation: str  # "<kind>:<ref>" — resolves to a stored source row


def source_split(
    *,
    lineage: Sequence[LineageShare] = (),
    respondents: Mapping[str, Sequence[str]] | None = None,
    research: Sequence[tuple[str, str, Sequence[str]]] = (),
) -> list[SourceShare]:
    """The full-source-table split: tranche mass over every class
    present, summing exactly to 1.0; empty everything is honestly []."""
    lineage = [share for share in lineage if share.weight > 0]
    surveys = {
        str(survey_id): [str(p) for p in names]
        for survey_id, names in (respondents or {}).items()
        if names
    }
    measured = [
        (str(kind), str(ref), [str(p) for p in names])
        for kind, ref, names in research
        if names and str(kind) in _RESEARCH_KINDS
    ]

    tranches: dict[str, float] = {}
    if lineage:
        tranches["lineage"] = TRANCHE_LINEAGE
    if surveys:
        tranches["survey"] = TRANCHE_SURVEY
    if measured:
        tranches["research"] = TRANCHE_RESEARCH
    if not tranches:
        return []
    total = sum(tranches.values())

    shares: list[SourceShare] = []
    if lineage:
        recorded = sum(share.weight for share in lineage)
        scale = tranches["lineage"] / total / recorded
        shares.extend(
            SourceShare(
                principal=share.author,
                weight=round(share.weight * scale, _PRECISION),
                citation=f"lineage:{share.contribution_id}",
            )
            for share in lineage
        )
    if surveys:
        rows = [
            (survey_id, principal)
            for survey_id in sorted(surveys)
            for principal in surveys[survey_id]
        ]
        per = round(tranches["survey"] / total / len(rows), _PRECISION)
        shares.extend(
            SourceShare(
                principal=principal,
                weight=per,
                citation=f"survey:{survey_id}",
            )
            for survey_id, principal in rows
        )
    if measured:
        rows = [
            (kind, ref, principal)
            for kind, ref, names in measured
            for principal in names
        ]
        per = round(tranches["research"] / total / len(rows), _PRECISION)
        shares.extend(
            SourceShare(
                principal=principal,
                weight=per,
                citation=f"{kind}:{ref}",
            )
            for kind, ref, principal in rows
        )
    # The last share absorbs the rounding remainder: the table sums to
    # 1.0, and the engine's integer split conserves the micros.
    body, last = shares[:-1], shares[-1]
    return [
        *body,
        SourceShare(
            principal=last.principal,
            weight=round(1.0 - sum(s.weight for s in body), _PRECISION),
            citation=last.citation,
        ),
    ]


def merged_shares(shares: Sequence[SourceShare]) -> list[tuple[str, float]]:
    """The engine's input shape: one (principal, weight) line per
    member, weights summed across their citations — the ``PricingEngine``
    pays per principal; the citation table keeps the finer grain."""
    merged: dict[str, float] = {}
    for share in shares:
        merged[share.principal] = merged.get(share.principal, 0.0) + share.weight
    return list(merged.items())


_CITATIONS_SCHEMA = """CREATE TABLE IF NOT EXISTS press_reward_citations (
    tenant_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    principal TEXT NOT NULL,
    citation TEXT NOT NULL,
    weight REAL NOT NULL,
    PRIMARY KEY (tenant_id, event_id, principal, citation)
)"""


class RewardCitationStore:
    """Every payout row's citations, beside the ledger it explains.

    Rows are payout records: they ride the earnings ledger's retention —
    settled history stays balanced with its provenance attached — while
    erasure stops FUTURE shares upstream (deleted respondent rows and
    live research resolution shrink the next split by construction).
    Idempotent by key: a replayed settle re-records nothing."""

    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_CITATIONS_SCHEMA)

    def record(
        self, *, tenant: str, event_id: str, shares: Sequence[SourceShare]
    ) -> int:
        added = 0
        with self._conn.transaction() as db:
            for share in shares:
                cursor = db.execute(
                    """INSERT OR IGNORE INTO press_reward_citations
                         (tenant_id, event_id, principal, citation, weight)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        tenant,
                        event_id,
                        share.principal,
                        share.citation,
                        share.weight,
                    ),
                )
                added += int(getattr(cursor, "rowcount", 0) or 0)
        return added

    def of(self, event_id: str, *, tenant: str) -> list[dict]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                """SELECT principal, citation, weight
                   FROM press_reward_citations
                   WHERE tenant_id = ? AND event_id = ?
                   ORDER BY principal, citation""",
                (tenant, event_id),
            ).fetchall()
        return [
            {
                "principal": str(row["principal"]),
                "citation": str(row["citation"]),
                "weight": float(row["weight"]),
            }
            for row in rows
        ]
