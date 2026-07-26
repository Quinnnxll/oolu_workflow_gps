"""The travel desk — Explorer's logic plus time, people, and budget (A7).

A trip is a comparison under REAL constraints: the date window, the
party's shared free time (consented free-busy intersection — busy/free
only, never event contents), and the budget envelope. Candidates come
from in-app supply (marketplace listings in the ``travel`` category —
thin at first, and the plan says so honestly: v1 is a planner over real
constraints first, a booking engine to the extent supply exists).

Two laws:

- **Infeasible is named, never ranked.** A candidate that breaks a hard
  constraint appears in the brief with its violated constraint spelled
  out — "over budget by 120.00 USD", "no common free window of 3 days"
  — and never outranks a feasible plan, whatever its score.
- **Typed data in, typed briefs out.** The desk consumes Explorer's
  `ComparisonRow`s and the calendar's interval shapes — never a
  conversation transcript (the import scan holds the wall: nothing here
  touches chat or the assistant history).

Group choice rides A3 unchanged: destination pieces in the ``travel``
genre poll under the standing honesty laws. Booking rides the standing
commerce spine unchanged; a confirmed booking lands as calendar events
(source ``trip``) through the gateway's confirm door.

Deferred, named: multi-leg itineraries and inter-leg travel time — v1
compares single-destination packages; the constraint model carries the
window/party/budget terms a leg graph will extend.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict

from .bestbuy import BRIEF_MODES
from .comparisons import ComparisonRow

TRAVEL_CATEGORY = "travel"


class TravelConstraints(BaseModel):
    model_config = ConfigDict(frozen=True)

    window_start: datetime
    window_end: datetime
    nights: int  # the trip's length, in nights
    party: tuple[str, ...]  # who is going — the free-busy intersection set
    budget_micros: int  # the whole party's envelope

    def trip_length(self) -> timedelta:
        return timedelta(days=max(1, int(self.nights)))


class TravelCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    listing_id: str
    title: str
    seller: str
    price_micros: int  # per person
    party_cost_micros: int
    score: float
    factors: dict
    feasible: bool
    violations: tuple[str, ...]  # named, or empty


class TravelBrief(BaseModel):
    model_config = ConfigDict(frozen=True)

    mode: str
    window_start: datetime
    window_end: datetime
    nights: int
    party: tuple[str, ...]
    budget_micros: int
    # The group's shared free slots long enough for the trip — what the
    # whole brief stands on. Intervals only: the privacy shape.
    open_slots: tuple[tuple[datetime, datetime], ...]
    feasible: tuple[TravelCandidate, ...]  # ranked, best first
    infeasible: tuple[TravelCandidate, ...]  # named violations, unranked


def _money(micros: int) -> str:
    return f"{micros / 1_000_000:.2f}"


def plan_trip(
    rows: list[ComparisonRow],
    *,
    constraints: TravelConstraints,
    open_slots: list[tuple[datetime, datetime]],
    mode: str = "balanced",
) -> TravelBrief:
    """The brief: every travel candidate judged against the constraints,
    feasible plans ranked with Explorer's own factor arithmetic,
    infeasible ones named and set apart. Deterministic end to end."""
    if mode not in BRIEF_MODES:
        known = ", ".join(sorted(BRIEF_MODES))
        raise ValueError(f"unknown mode: {mode!r} — one of: {known}")
    weights = BRIEF_MODES[mode]
    priced = [r for r in rows if r.eligible and r.price_micros > 0]
    cheapest = min((r.price_micros for r in priced), default=0)
    feasible: list[TravelCandidate] = []
    infeasible: list[TravelCandidate] = []
    for row in rows:
        violations: list[str] = list(row.gaps)
        party_cost = row.price_micros * max(1, len(constraints.party))
        if party_cost > constraints.budget_micros:
            over = party_cost - constraints.budget_micros
            violations.append(
                f"over budget by {_money(over)} {row.currency} for "
                f"{len(constraints.party)} travellers"
            )
        if not open_slots:
            violations.append(
                f"no common free window of {constraints.nights} "
                f"night{'s' if constraints.nights != 1 else ''} in the range"
            )
        factors = {
            "price": (
                round(cheapest / row.price_micros, 4)
                if row.price_micros > 0 and cheapest > 0
                else 0.0
            ),
            "discount": (row.discount_percent or 0) / 100.0,
            "feedback": float(row.feedback.get("factor", 0.5)),
            "trust": float(row.trust.get("score", 0.5)),
            "lab": float(row.lab.get("factor", 0.5)),
        }
        score = sum(weights[name] * value for name, value in factors.items())
        candidate = TravelCandidate(
            listing_id=row.listing_id,
            title=row.title,
            seller=row.seller,
            price_micros=row.price_micros,
            party_cost_micros=party_cost,
            score=round(score, 4),
            factors=factors,
            feasible=not violations,
            violations=tuple(violations),
        )
        (feasible if candidate.feasible else infeasible).append(candidate)
    # Feasible plans rank; infeasible ones stand aside, named — a broken
    # constraint is a wall, not a weight.
    feasible.sort(key=lambda c: (-c.score, c.listing_id))
    infeasible.sort(key=lambda c: c.listing_id)
    return TravelBrief(
        mode=mode,
        window_start=constraints.window_start,
        window_end=constraints.window_end,
        nights=constraints.nights,
        party=constraints.party,
        budget_micros=constraints.budget_micros,
        open_slots=tuple(open_slots),
        feasible=tuple(feasible),
        infeasible=tuple(infeasible),
    )
