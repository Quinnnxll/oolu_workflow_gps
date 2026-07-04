"""Noder rewards: multipliers, class-aware commission, lineage, conservation."""

from __future__ import annotations

import pytest

from workflow_gps.billing.pricing import PricingEngine
from workflow_gps.nodeplace import (
    CandidateEconomics,
    CostVector,
    LineageLink,
    NodeClass,
    RewardSignals,
    build_run_binding,
    commission_rate,
    lineage_shares,
    reward_multiplier,
)


def _signals(**kwargs) -> RewardSignals:
    defaults = dict(node_class=NodeClass.WORKFLOW)
    defaults.update(kwargs)
    return RewardSignals(**defaults)


def test_multiplier_rewards_reputation_and_verified_reliability():
    base = reward_multiplier(_signals()).multiplier
    rated = reward_multiplier(_signals(reputation=1.8)).multiplier
    reliable = reward_multiplier(
        _signals(verified_successes=100, verified_failures=2)
    ).multiplier
    unreliable = reward_multiplier(
        _signals(verified_successes=2, verified_failures=100)
    ).multiplier
    assert rated > base > unreliable
    assert reliable > base
    # No evidence = exactly neutral reliability, not a bonus.
    assert reward_multiplier(_signals()).reliability_factor == 1.0


def test_scarce_supply_earns_more_and_crowded_commodities_decay():
    scarce = reward_multiplier(_signals(substitutes=0)).multiplier
    crowded = reward_multiplier(_signals(substitutes=10)).multiplier
    assert scarce > crowded

    commodity_alone = reward_multiplier(
        _signals(node_class=NodeClass.COMMODITY, substitutes=0)
    )
    commodity_crowded = reward_multiplier(
        _signals(node_class=NodeClass.COMMODITY, substitutes=20)
    )
    assert commodity_crowded.commodity_decay < commodity_alone.commodity_decay
    assert commodity_crowded.commodity_decay >= 0.35  # floored, never zeroed


def test_abandoned_nodes_decay_gently():
    fresh = reward_multiplier(_signals(days_since_update=0)).maintenance_factor
    stale = reward_multiplier(_signals(days_since_update=730)).maintenance_factor
    assert fresh == 1.0
    assert 0.7 <= stale < 0.8


def test_multiplier_is_always_bounded():
    extreme = reward_multiplier(
        _signals(
            reputation=2.0,
            verified_successes=10_000,
            substitutes=0,
            days_since_update=0,
        )
    )
    assert extreme.multiplier <= 4.0
    hopeless = reward_multiplier(
        _signals(
            node_class=NodeClass.COMMODITY,
            reputation=0.0,
            verified_failures=10_000,
            substitutes=100,
            days_since_update=10_000,
        )
    )
    assert hopeless.multiplier >= 0.10


def test_commission_is_class_aware_and_pass_through_is_free():
    assert commission_rate(NodeClass.REGULATED) == 0.0
    assert (
        commission_rate(NodeClass.PROFESSIONAL)
        < commission_rate(NodeClass.WORKFLOW)
        < commission_rate(NodeClass.COMMODITY)
    )
    # Scarcity reduces the take, but never below the 10% floor.
    assert commission_rate(NodeClass.PROFESSIONAL, scarcity_bonus=1.0) >= 0.10


def test_lineage_shares_are_geometric_normalized_and_merged():
    shares = lineage_shares(
        "author",
        [
            LineageLink(noder_principal="parent", level=1),
            LineageLink(noder_principal="grandparent", level=2),
        ],
        decay=0.35,
    )
    by = {s.noder_principal: s.weight for s in shares}
    assert abs(sum(by.values()) - 1.0) < 1e-9  # carved out of the pool
    assert by["author"] > by["parent"] > by["grandparent"]
    assert by["parent"] / by["author"] == pytest.approx(0.35)

    # A noder deriving from their own node merges into one share.
    merged = lineage_shares("author", [LineageLink(noder_principal="author", level=1)])
    assert len(merged) == 1 and merged[0].weight == 1.0

    with pytest.raises(ValueError):
        lineage_shares("author", [], decay=1.5)


def test_full_split_conserves_and_pays_the_better_noder_more():
    """Shares + multipliers plug into billing.PricingEngine and conserve."""
    good = _signals(reputation=1.8, verified_successes=200, verified_failures=4)
    plain = _signals(reputation=0.9, verified_successes=20, verified_failures=10)
    shares = [
        s.model_copy(update={"multiplier": reward_multiplier(sig).multiplier})
        for s, sig in zip(
            lineage_shares(
                "good-noder", [LineageLink(noder_principal="plain-noder", level=1)]
            ),
            # lineage_shares sorts principals alphabetically: good-noder, plain-noder
            [good, plain],
        )
    ]
    result = PricingEngine(rho=commission_rate(NodeClass.WORKFLOW)).price(
        gross=1.00, provider_cost=0.10, shares=shares
    )
    assert result.conserves()  # multipliers redistribute, never inflate
    # The executing noder holds weight 1 vs the parent's 0.35 AND has the
    # better multiplier: their slice must dominate.
    assert result.noder_micros["good-noder"] > result.noder_micros["plain-noder"]


def test_build_run_binding_carries_the_economics_not_the_money():
    candidate = CandidateEconomics(
        version_id="v1",
        noder_principal="author",
        node_class=NodeClass.WORKFLOW,
        class_key="wf:clean",
        cleared_price=0.25,
        cost=CostVector(model=0.02, api=0.01),
    )
    binding = build_run_binding(
        run_id="run-1",
        consumer_tenant="tenant-1",
        candidate=candidate,
        signals=_signals(reputation=1.5),
        ancestors=[LineageLink(noder_principal="parent", level=1)],
    )
    assert binding.gross == 0.25
    assert binding.provider_cost == pytest.approx(0.03)
    principals = {s.noder_principal for s in binding.shares}
    assert principals == {"author", "parent"}
    assert abs(sum(s.weight for s in binding.shares) - 1.0) < 1e-9
    # The author's share carries the reward multiplier; money moves only when
    # the metering deriver later sees a verified success for this run_id.
    author = next(s for s in binding.shares if s.noder_principal == "author")
    assert author.multiplier > 1.0
