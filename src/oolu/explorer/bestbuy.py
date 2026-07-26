"""The best buy — deterministic, mode-weighted, breakdown-first (A6).

The QuoteMode pattern for shoppers: one enum of modes, each a declared
weight set over the five factors — price, the discount fact, verified
feedback, the order-book trust score, and member-measured lab results.
Same rows, same mode → same ranking, ties broken by listing_id, and
every row in the brief carries its full factor breakdown (invariant 10:
a verdict without its reasons is a vibe).

Buying is a handoff, not a feature: the brief names the winner's
listing; the standing commerce spine — intent, digest, policy ladder,
approval, order — does the rest, unchanged.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .comparisons import ComparisonRow

# The modes, each an explainable stance. Weights are the mechanism;
# the numbers are working defaults (decision-log territory).
BRIEF_MODES: dict[str, dict[str, float]] = {
    # cheapest that stands behind itself
    "value": {"price": 0.45, "discount": 0.15, "feedback": 0.15, "trust": 0.15, "lab": 0.10},
    # the even hand
    "balanced": {"price": 0.20, "discount": 0.10, "feedback": 0.25, "trust": 0.25, "lab": 0.20},
    # most proven by people and the order book; price is secondary
    "proven": {"price": 0.10, "discount": 0.05, "feedback": 0.35, "trust": 0.35, "lab": 0.15},
    # the numbers decide: measured results first
    "measured": {"price": 0.10, "discount": 0.05, "feedback": 0.15, "trust": 0.20, "lab": 0.50},
}


class BestBuyBrief(BaseModel):
    model_config = ConfigDict(frozen=True)

    mode: str
    winner_listing_id: str | None  # None = nothing eligible to crown
    ranked: tuple[dict, ...]  # every eligible row: score + factor breakdown


def _price_factor(row: ComparisonRow, cheapest_micros: int) -> float:
    if row.price_micros <= 0:
        return 0.0
    return cheapest_micros / row.price_micros


def best_buy(rows: list[ComparisonRow], *, mode: str = "balanced") -> BestBuyBrief:
    if mode not in BRIEF_MODES:
        known = ", ".join(sorted(BRIEF_MODES))
        raise ValueError(f"unknown mode: {mode!r} — one of: {known}")
    weights = BRIEF_MODES[mode]
    eligible = [r for r in rows if r.eligible]
    if not eligible:
        return BestBuyBrief(mode=mode, winner_listing_id=None, ranked=())
    cheapest = min(r.price_micros for r in eligible if r.price_micros > 0)
    ranked: list[dict] = []
    for row in eligible:
        factors = {
            "price": round(_price_factor(row, cheapest), 4),
            "discount": (row.discount_percent or 0) / 100.0,
            "feedback": float(row.feedback.get("factor", 0.5)),
            "trust": float(row.trust.get("score", 0.5)),
            "lab": float(row.lab.get("factor", 0.5)),
        }
        score = sum(weights[name] * value for name, value in factors.items())
        ranked.append(
            {
                "listing_id": row.listing_id,
                "title": row.title,
                "seller": row.seller,
                "score": round(score, 4),
                "factors": factors,  # the whole breakdown, always
                "weights": weights,  # and the stance that weighed it
            }
        )
    ranked.sort(key=lambda item: (-item["score"], item["listing_id"]))
    return BestBuyBrief(
        mode=mode,
        winner_listing_id=ranked[0]["listing_id"],
        ranked=tuple(ranked),
    )
