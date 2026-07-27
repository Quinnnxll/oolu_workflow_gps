"""Process-capability learning — every outcome counts, under rights.

Two plan rules are structural (acceptance test 35, Phase 8's fourth
exit gate, invariant 29):

* **Negative outcomes are learning data.** Rejected parts, aborted
  cycles, near misses, safety trips, and failed recoveries enter the
  ledger exactly like acceptances, lower the routing signal, and stay
  queryable. There is no code path that ingests successes only.
* **Learning consumes rights.** Ingestion requires the evidence's
  ``route_training`` permission through the durable rights store —
  process outcomes are someone's data before they are anyone's
  training signal.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict

from ..registries.rights_store import RightsStore


class OutcomeKind(str, Enum):
    ACCEPTED = "accepted"
    REWORKED = "reworked"
    QUARANTINED = "quarantined"
    SCRAPPED = "scrapped"
    CONTAINED = "contained"
    NEAR_MISS = "near_miss"
    ABORTED = "aborted"
    SAFETY_TRIP = "safety_trip"


NEGATIVE_KINDS = frozenset(OutcomeKind) - {OutcomeKind.ACCEPTED}


class ProcessOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    job_digest: str
    recipe_version_id: str
    node_version_id: str
    kind: OutcomeKind
    deviation_count: int = 0
    rights_id: str = ""


class ProcessCapabilityLedger:
    """Append-only outcomes; the routing signal is computed over all."""

    def __init__(self, rights: RightsStore) -> None:
        self._rights = rights
        self._outcomes: list[ProcessOutcome] = []

    def ingest(self, outcome: ProcessOutcome, *, now: int) -> None:
        """Refuses without ``route_training`` rights — by name, upstream."""

        self._rights.require(
            outcome.rights_id,
            "route_training",
            evidence_id=outcome.job_digest,
            now=now,
        )
        self._outcomes.append(outcome)

    def outcomes(self) -> tuple[ProcessOutcome, ...]:
        return tuple(self._outcomes)

    def negative_outcomes(self) -> tuple[ProcessOutcome, ...]:
        return tuple(o for o in self._outcomes if o.kind in NEGATIVE_KINDS)

    def historical_yield(self, recipe_version_id: str) -> float | None:
        """Accepted over *all* outcomes — negatives cannot disappear."""

        relevant = [
            o for o in self._outcomes if o.recipe_version_id == recipe_version_id
        ]
        if not relevant:
            return None
        accepted = sum(1 for o in relevant if o.kind is OutcomeKind.ACCEPTED)
        return accepted / len(relevant)
