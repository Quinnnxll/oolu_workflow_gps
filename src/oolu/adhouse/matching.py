"""Matching — a deterministic, explainable second-price pick (A4).

The matcher is a pure function: the same campaigns, the same content
genres, the same consent state, and the same clock produce the same
placement with the same factor breakdown — reproducibility IS the
audit. Nothing from the editorial side flows in (invariant 5: the
matcher sees content genres, never rubric scores), and nothing flows
back out.

Second-price keeps bids honest: the winner pays the runner-up's bid
(floored), never its own — overbidding buys placement, not price.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .campaigns import Campaign

MIN_PRICE_MICROS = 500  # the price floor when a winner runs unopposed
# How strongly a consented viewer's genre affinity bends the score.
# Bounded: a loved genre helps a fitting campaign; it never makes the
# taxonomy fit irrelevant.
AFFINITY_PULL = 0.25


@dataclass(frozen=True)
class MatchResult:
    campaign: Campaign
    price_micros: int  # what the winner pays — second price, floored
    breakdown: dict  # every factor, named — the explainable record


def _fit(campaign: Campaign, content_genres: tuple[str, ...]) -> float:
    """Taxonomy fit: how much of the campaign's targeting this content
    satisfies. Zero overlap = ineligible, whatever the bid."""
    target = set(campaign.genres)
    if not target:
        return 0.0
    return len(target & set(content_genres)) / len(target)


def _affinity_boost(
    campaign: Campaign,
    content_genres: tuple[str, ...],
    affinity: dict[str, float] | None,
) -> float:
    if not affinity:
        return 1.0
    overlap = set(campaign.genres) & set(content_genres)
    scores = [affinity[g] for g in overlap if g in affinity]
    if not scores:
        return 1.0
    mean = sum(scores) / len(scores)
    return max(0.5, 1.0 + AFFINITY_PULL * mean)


def eligible(
    campaign: Campaign,
    *,
    content_genres: tuple[str, ...],
    now: datetime,
    exclude: frozenset[str] = frozenset(),
) -> bool:
    return (
        campaign.status == "active"
        and campaign.flight_start <= now <= campaign.flight_end
        and campaign.campaign_id not in exclude
        and campaign.remaining_micros >= max(campaign.bid_micros, MIN_PRICE_MICROS)
        and _fit(campaign, content_genres) > 0.0
    )


def match(
    campaigns: list[Campaign],
    *,
    content_genres: tuple[str, ...],
    now: datetime,
    affinity: dict[str, float] | None = None,
    exclude: frozenset[str] = frozenset(),
) -> MatchResult | None:
    """The pick. Ties break on campaign_id — deterministic to the end."""
    scored: list[tuple[float, Campaign, dict]] = []
    for campaign in campaigns:
        if not eligible(
            campaign, content_genres=content_genres, now=now, exclude=exclude
        ):
            continue
        fit = _fit(campaign, content_genres)
        boost = _affinity_boost(campaign, content_genres, affinity)
        score = campaign.bid_micros * fit * boost
        scored.append(
            (
                score,
                campaign,
                {
                    "bid_micros": campaign.bid_micros,
                    "fit": round(fit, 4),
                    "affinity_boost": round(boost, 4),
                    "score": round(score, 4),
                },
            )
        )
    if not scored:
        return None
    scored.sort(key=lambda item: (-item[0], item[1].campaign_id))
    _, winner, breakdown = scored[0]
    runner_up_bid = scored[1][1].bid_micros if len(scored) > 1 else None
    price = max(
        MIN_PRICE_MICROS,
        min(
            winner.bid_micros,
            runner_up_bid if runner_up_bid is not None else MIN_PRICE_MICROS,
        ),
    )
    breakdown["second_price"] = {
        "runner_up_bid_micros": runner_up_bid,
        "price_micros": price,
    }
    breakdown["contenders"] = len(scored)
    return MatchResult(campaign=winner, price_micros=price, breakdown=breakdown)
