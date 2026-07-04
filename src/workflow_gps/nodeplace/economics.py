"""Candidate assembly — live production data in, ``CandidateEconomics`` out.

This is the seam between the marketplace's records and the pricing/reward
engines: given a discovery query, ``CandidateAssembler`` joins

- the **registry** (active public listings, their versions, asks, and owners),
- the **metering ledger** (platform-verified successes and their measured
  provider cost — the only success signal the economics trust),
- the **audit log** (failed executions, mapped to versions through the run
  bindings, so failure counts are real too, not self-reported), and
- the **rating store** (the reputation mu from verified raters),

into one ``AssembledCandidate`` per listing: the ``CandidateEconomics`` the
router ranks, plus the ``RewardSignals`` the reward engine multiplies, with
substitute counts computed per class key across the assembled set.

Listing tags carry the market classification, by convention:

- ``class:<commodity|workflow|professional|regulated>`` sets the pricing
  class (default: ``workflow`` — the safe middle ground);
- ``market:<segment>`` sets the class key's segment, e.g.
  ``market:file_conversion.csv_xlsx``; without it the listing title's slug is
  used. Class keys look like ``commodity:file_conversion.csv_xlsx``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from ..durable.audit import DurableAuditLog
from ..metering.attribution import AttributionStore
from ..metering.store import MeteringLedger
from ..skills.models import ExecutionStatus
from .market import CandidateEconomics, CostVector, NodeClass
from .pricing import gross_from_policy
from .ratings import RatingService
from .rewards import RewardSignals
from .store import RegistryStore

_EXECUTED_EVENT = "workflow.executed"
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_CLASS_TAGS = {c.value: c for c in NodeClass}


class VersionStats(BaseModel):
    """Platform-verified outcome counts and measured cost for one version."""

    model_config = ConfigDict(frozen=True)

    successes: int = 0
    failures: int = 0
    provider_cost_mean: float | None = None


@runtime_checkable
class StatsSource(Protocol):
    def version_stats(self, version_id: str) -> VersionStats: ...


class LiveVersionStats:
    """Verified stats from production records.

    Successes (and their measured provider cost) come from the **metering
    ledger** — the exactly-once record of platform-verified success events.
    Failures come from the **audit log**'s ``workflow.executed`` records with
    a non-success status, attributed to a version through the run bindings.
    Neither number can be self-reported.
    """

    def __init__(
        self,
        *,
        metering: MeteringLedger,
        audit: DurableAuditLog | None = None,
        attribution: AttributionStore | None = None,
    ):
        self._metering = metering
        self._audit = audit
        self._attribution = attribution

    def version_stats(self, version_id: str) -> VersionStats:
        successes = 0
        costs: list[float] = []
        for event in self._metering.events():
            if event.version_id != version_id:
                continue
            successes += 1
            if event.provider_cost is not None:
                costs.append(event.provider_cost)

        failures = 0
        if self._audit is not None and self._attribution is not None:
            for record in self._audit.records():
                if record.event_type != _EXECUTED_EVENT:
                    continue
                if record.payload.get("status") == ExecutionStatus.SUCCEEDED.value:
                    continue
                run_id = record.payload.get("run_id", "")
                binding = self._attribution.get_binding(run_id)
                if binding is not None and binding.version_id == version_id:
                    failures += 1

        return VersionStats(
            successes=successes,
            failures=failures,
            provider_cost_mean=(sum(costs) / len(costs)) if costs else None,
        )


class AssembledCandidate(BaseModel):
    """One marketplace listing, joined with everything the economics need."""

    model_config = ConfigDict(frozen=True)

    listing_id: str
    title: str
    candidate: CandidateEconomics
    signals: RewardSignals
    tags: list[str] = Field(default_factory=list)


def _slug(text: str) -> str:
    return _SLUG_RE.sub("_", text.strip().lower()).strip("_") or "listing"


def classify_listing(tags: list[str], title: str) -> tuple[NodeClass, str]:
    """Market classification from listing tags (see module docstring)."""
    node_class = NodeClass.WORKFLOW
    segment = None
    for tag in tags:
        if tag.startswith("class:"):
            node_class = _CLASS_TAGS.get(tag[6:].strip().lower(), NodeClass.WORKFLOW)
        elif tag.startswith("market:"):
            segment = _slug(tag[7:])
    return node_class, f"{node_class.value}:{segment or _slug(title)}"


class CandidateAssembler:
    """Join registry + verified stats + reputation into candidate economics."""

    def __init__(
        self,
        *,
        registry: RegistryStore,
        stats: StatsSource,
        ratings: RatingService | None = None,
        clock=None,
    ):
        self._registry = registry
        self._stats = stats
        self._ratings = ratings
        self._clock = clock or (lambda: datetime.now(UTC))

    def assemble_version(self, version_id: str) -> AssembledCandidate | None:
        """Assemble one version, with substitutes counted against the whole
        public market. ``None`` when the version has no active public listing
        (a revoked or unpublished node cannot be bound to a paying run)."""
        for entry in self.assemble(""):
            if entry.candidate.version_id == version_id:
                return entry
        return None

    def assemble(self, query: str = "") -> list[AssembledCandidate]:
        listings = self._registry.discover(query)
        now = self._clock()

        drafts: list[tuple] = []
        for listing in listings:
            version = self._registry.get_version(listing.version_id)
            if version is None:
                continue
            node = self._registry.get_node(version.node_id)
            if node is None or node.revoked_at is not None:
                continue
            policy = self._registry.get_pricing(listing.version_id)
            ask = gross_from_policy(policy) if policy is not None else 0.0
            stats = self._stats.version_stats(listing.version_id)
            reputation = (
                self._ratings.reputation(listing.version_id)
                if self._ratings is not None
                else 1.0
            )
            node_class, class_key = classify_listing(listing.tags, listing.title)
            days_since_update = max(
                0.0, (now - version.published_at).total_seconds() / 86_400.0
            )
            drafts.append(
                (
                    listing,
                    node,
                    ask,
                    stats,
                    reputation,
                    node_class,
                    class_key,
                    days_since_update,
                )
            )

        # Substitutes: the OTHER assembled candidates sharing a class key.
        per_class: dict[str, int] = {}
        for draft in drafts:
            per_class[draft[6]] = per_class.get(draft[6], 0) + 1

        assembled: list[AssembledCandidate] = []
        for (
            listing,
            node,
            ask,
            stats,
            reputation,
            node_class,
            class_key,
            days_since_update,
        ) in drafts:
            substitutes = per_class[class_key] - 1
            cost = (
                CostVector(compute=stats.provider_cost_mean)
                if stats.provider_cost_mean is not None
                else CostVector()
            )
            assembled.append(
                AssembledCandidate(
                    listing_id=listing.listing_id,
                    title=listing.title,
                    tags=list(listing.tags),
                    candidate=CandidateEconomics(
                        version_id=listing.version_id,
                        noder_principal=node.noder_principal,
                        node_class=node_class,
                        class_key=class_key,
                        cleared_price=ask,  # the ask; clearing happens downstream
                        cost=cost,
                        verified_successes=stats.successes,
                        verified_failures=stats.failures,
                        reputation=reputation,
                    ),
                    signals=RewardSignals(
                        node_class=node_class,
                        reputation=reputation,
                        verified_successes=stats.successes,
                        verified_failures=stats.failures,
                        substitutes=substitutes,
                        days_since_update=days_since_update,
                    ),
                )
            )
        return assembled
