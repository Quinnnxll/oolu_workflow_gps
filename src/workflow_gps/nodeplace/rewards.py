"""Noder rewards — how verified success turns into earnings, and for whom.

Design contract with the rest of the money stack (roadmap invariants):

- **Payouts follow verified success only.** This module never moves money at
  quote time. It computes the *shares and multipliers* that ride on the
  ``RunBinding``; the exactly-once pipeline (metering deriver -> billing
  ``PricingEngine`` -> earnings ledger -> settlement) does the accrual when a
  platform-verified success event exists, and clawback/disputes handle the
  rest.
- **Multipliers redistribute, never inflate.** The billing ``PricingEngine``
  normalizes ``weight x multiplier`` within the pool, so the sum of payouts
  always equals the pool exactly (integer-micros conservation). A reward
  multiplier changes a noder's *slice*, not the size of the pie — which fixes
  the classic bug where stacked bonus factors pay out more than the pool.

What the multiplier rewards (each factor bounded and documented):

- **Reputation** — the ratings-derived mu in [0, mu_max] (verified raters only).
- **Verified reliability** — the Beta-posterior mean of metered success,
  which starts neutral and must be *earned* with real runs.
- **Scarcity** — supply of quality-comparable substitutes in the class; a
  crowded commodity class decays toward parity (there is no rent in doing
  what four other nodes do equally well), scarce expert supply earns a bonus.
- **Maintenance** — a version that keeps being updated holds its multiplier;
  an abandoned one decays gently. Encourages stewardship, not squatting.

The platform's commission is class-aware: lower for professional/scarce work
(to attract the supply that is hardest to get) and higher for commodities —
``commission_rate`` feeds directly into ``billing.PricingEngine(rho=...)``.

**Lineage royalties**: a node built from other nodes (a super-node composed of
sub-workflows, or a derived version of someone else's node) shares earnings
upstream with geometric decay per level. Expressed as plain ``NoderShare``
weights, so attribution, disputes, and settlement all keep working unchanged.
"""

from __future__ import annotations

from math import exp

from pydantic import BaseModel, ConfigDict, Field

from ..metering.models import NoderShare, RunBinding
from .market import CandidateEconomics, NodeClass

# Class-aware platform commission (rho) — reviewed money knobs, like
# billing.policy. Lower take where supply is scarcest.
CLASS_COMMISSION: dict[NodeClass, float] = {
    NodeClass.COMMODITY: 0.35,
    NodeClass.WORKFLOW: 0.30,
    NodeClass.PROFESSIONAL: 0.20,
    NodeClass.REGULATED: 0.0,  # pass-through fees carry no platform commission
}

DEFAULT_LINEAGE_DECAY = 0.35  # each ancestry level earns this fraction of the next
_MIN_MULTIPLIER = 0.10
_MAX_MULTIPLIER = 4.0


class RewardSignals(BaseModel):
    """The observable, non-gameable inputs to one noder's reward multiplier."""

    model_config = ConfigDict(frozen=True)

    node_class: NodeClass
    reputation: float = 1.0  # ratings-derived mu, [0, mu_max]
    verified_successes: int = 0
    verified_failures: int = 0
    substitutes: int = 0  # quality-comparable nodes in the same class key
    quality_parity: float = 1.0
    days_since_update: float = 0.0
    difficulty: float = 1.0

    @property
    def reliability_mean(self) -> float:
        return (1.0 + self.verified_successes) / (
            2.0 + self.verified_successes + self.verified_failures
        )


class RewardBreakdown(BaseModel):
    """The multiplier plus every factor that produced it (explainability)."""

    model_config = ConfigDict(frozen=True)

    multiplier: float
    reputation_factor: float
    reliability_factor: float
    scarcity_factor: float
    maintenance_factor: float
    commodity_decay: float


def reward_multiplier(signals: RewardSignals) -> RewardBreakdown:
    """One noder's share multiplier from bounded, verified signals.

    Every factor is individually bounded, and the product is clamped to
    [0.1, 4.0]; the billing engine normalizes within the pool anyway, so this
    can only shift slices between noders, never overspend the pool.
    """
    reputation = min(max(signals.reputation, 0.0), 2.0)
    reputation_factor = max(reputation, 0.25)

    # 0.5 (all failures) .. 1.5 (all successes); exactly 1.0 with no evidence.
    reliability_factor = 0.5 + signals.reliability_mean

    # Scarce supply earns up to +50%; crowded supply tends to 1.0 from above.
    effective_subs = max(0, signals.substitutes) * min(
        max(signals.quality_parity, 0.0), 1.0
    )
    scarcity_factor = 1.0 + 0.5 / (1.0 + effective_subs)

    # Commodities in crowded classes decay toward a floor of 0.35 (no rent for
    # interchangeable work); other classes are exempt.
    if signals.node_class is NodeClass.COMMODITY:
        commodity_decay = 0.35 + 0.65 / (1.0 + 0.5 * effective_subs)
    else:
        commodity_decay = 1.0

    # Full credit while maintained; drifts to 0.7 after ~a year untouched.
    maintenance_factor = 0.7 + 0.3 * exp(-max(signals.days_since_update, 0.0) / 365.0)

    product = (
        reputation_factor
        * reliability_factor
        * scarcity_factor
        * maintenance_factor
        * commodity_decay
    )
    return RewardBreakdown(
        multiplier=min(max(product, _MIN_MULTIPLIER), _MAX_MULTIPLIER),
        reputation_factor=reputation_factor,
        reliability_factor=reliability_factor,
        scarcity_factor=scarcity_factor,
        maintenance_factor=maintenance_factor,
        commodity_decay=commodity_decay,
    )


def commission_rate(node_class: NodeClass, *, scarcity_bonus: float = 0.0) -> float:
    """The platform's rho for this run, feeding ``billing.PricingEngine``.

    ``scarcity_bonus`` (0..1) further reduces the take for provably scarce
    supply; the result is clamped to [0.10, 0.45] except for regulated
    pass-through, which is always 0.
    """
    if node_class is NodeClass.REGULATED:
        return 0.0
    base = CLASS_COMMISSION[node_class]
    reduced = base * (1.0 - 0.5 * min(max(scarcity_bonus, 0.0), 1.0))
    return min(max(reduced, 0.10), 0.45)


class LineageLink(BaseModel):
    """One ancestor in a node's derivation chain (level 1 = direct parent)."""

    model_config = ConfigDict(frozen=True)

    noder_principal: str
    level: int = Field(ge=1)


def lineage_shares(
    executing_noder: str,
    ancestors: list[LineageLink] = (),
    *,
    decay: float = DEFAULT_LINEAGE_DECAY,
    executing_multiplier: float = 1.0,
    ancestor_multipliers: dict[str, float] | None = None,
) -> list[NoderShare]:
    """Split one run's contributor pool across the derivation chain.

    The executing noder holds weight 1; each ancestor at level ``n`` holds
    ``decay ** n``. Weights are then normalized to sum to 1, so royalties are
    carved *out of* the pool, never added on top. Duplicate principals (a
    noder deriving from their own node) merge into one share.
    """
    if not 0.0 < decay < 1.0:
        raise ValueError("lineage decay must be in (0, 1)")
    multipliers = dict(ancestor_multipliers or {})
    raw: dict[str, float] = {executing_noder: 1.0}
    for link in ancestors:
        raw[link.noder_principal] = raw.get(link.noder_principal, 0.0) + (
            decay**link.level
        )
    total = sum(raw.values())
    shares = []
    for principal, weight in sorted(raw.items()):
        multiplier = (
            executing_multiplier
            if principal == executing_noder
            else multipliers.get(principal, 1.0)
        )
        shares.append(
            NoderShare(
                noder_principal=principal,
                weight=weight / total,
                multiplier=multiplier,
            )
        )
    return shares


def build_run_binding(
    *,
    run_id: str,
    consumer_tenant: str,
    candidate: CandidateEconomics,
    signals: RewardSignals,
    ancestors: list[LineageLink] = (),
    consumer_principal: str | None = None,
    lineage_decay: float = DEFAULT_LINEAGE_DECAY,
) -> RunBinding:
    """The bridge into the exactly-once pipeline.

    Bind the run to its economics *before* execution; the metering deriver
    only turns this into money if the audit log later shows a verified
    success. Gross is the cleared price (pass-through invoices ride on their
    own lines, not through the contributor pool); provider cost is the
    platform's automation cost.
    """
    breakdown = reward_multiplier(signals)
    shares = lineage_shares(
        candidate.noder_principal,
        list(ancestors),
        decay=lineage_decay,
        executing_multiplier=breakdown.multiplier,
    )
    return RunBinding(
        run_id=run_id,
        version_id=candidate.version_id,
        consumer_tenant=consumer_tenant,
        consumer_principal=consumer_principal,
        gross=candidate.cleared_price,
        provider_cost=candidate.cost.automation_cost,
        shares=shares,
    )
