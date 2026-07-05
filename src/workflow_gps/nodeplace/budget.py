"""Cost-aware assembly budgets: caps, review thresholds, and learned comfort.

Three signals gate what an assembled plan may cost, each with its own
authority:

- a **caller-set hard cap** is absolute — estimates above it are refused
  (:class:`BudgetExceededError`), no acknowledgement can override it;
- a **user-set review threshold** demands an explicit second look — the plan
  is fine to run, but only after the user acknowledges the review
  (:class:`ReviewRequiredError` until they do);
- a **behavioral comfort ceiling** learned from the user's own committed
  spending (median and peak of their bound run grosses): a plan well above
  anything their history demonstrates needs review even when no explicit
  threshold was set. New users (too little history) are never judged by it.

The linked wallet is deliberately the weakest signal: its remaining balance
may be a small slice of the user's true assets, so it NEVER caps or scales
the budget — an estimate above the balance only adds a review reason (top up
or confirm), and a large balance grants nothing. Behavior is the signal;
balance is a logistics note.
"""

from __future__ import annotations

from statistics import median

from pydantic import BaseModel, ConfigDict, Field


class BudgetExceededError(PermissionError):
    """The estimate is above the caller-set hard cap. Not acknowledgeable."""


class ReviewRequiredError(PermissionError):
    """The estimate needs an explicit review acknowledgement before running."""


class BudgetPolicy(BaseModel):
    """What the caller/user declared, plus how behavior is judged."""

    model_config = ConfigDict(frozen=True)

    hard_cap: float | None = None  # refuse above this; never overridable
    review_threshold: float | None = None  # review above this
    # Behavior: review when the estimate exceeds BOTH typical * multiplier
    # and the user's demonstrated peak — growth within habit passes free.
    behavior_multiplier: float = 2.0
    min_history: int = 3  # runs before behavior may judge at all


class SpendingProfile(BaseModel):
    """What the user's committed run history says they normally spend."""

    model_config = ConfigDict(frozen=True)

    runs: int = 0
    typical: float = 0.0  # median committed gross per run
    peak: float = 0.0
    # None until there is enough history to judge anyone by it.
    comfort_ceiling: float | None = None

    @classmethod
    def from_history(
        cls,
        grosses: list[float],
        *,
        multiplier: float = 2.0,
        min_history: int = 3,
    ) -> "SpendingProfile":
        spent = [g for g in grosses if g > 0]
        if not spent:
            return cls()
        typical = float(median(spent))
        peak = float(max(spent))
        ceiling = max(typical * multiplier, peak) if len(spent) >= min_history else None
        return cls(runs=len(spent), typical=typical, peak=peak, comfort_ceiling=ceiling)


class BudgetVerdict(BaseModel):
    """The assessment a surface renders and a run path enforces."""

    model_config = ConfigDict(frozen=True)

    estimated: float
    allowed: bool = True  # False ONLY when the hard cap is exceeded
    needs_review: bool = False
    reasons: list[str] = Field(default_factory=list)
    profile: SpendingProfile | None = None


def assess_budget(
    estimated: float,
    *,
    policy: BudgetPolicy | None = None,
    spend_history: list[float] | None = None,
    wallet_balance: float | None = None,
) -> BudgetVerdict:
    """Judge an estimated plan cost against every configured signal.

    Reasons accumulate (like quote warnings): every tripped signal is
    reported, none overwrites another, so the user sees the whole picture
    in one verdict.
    """
    policy = policy or BudgetPolicy()
    profile = SpendingProfile.from_history(
        spend_history or [],
        multiplier=policy.behavior_multiplier,
        min_history=policy.min_history,
    )
    reasons: list[str] = []
    allowed = True
    needs_review = False

    if policy.hard_cap is not None and estimated > policy.hard_cap:
        allowed = False
        reasons.append(
            f"estimated cost {estimated:.2f} exceeds the hard cap {policy.hard_cap:.2f}"
        )
    if policy.review_threshold is not None and estimated > policy.review_threshold:
        needs_review = True
        reasons.append(
            f"estimated cost {estimated:.2f} is above the review threshold "
            f"{policy.review_threshold:.2f}"
        )
    if profile.comfort_ceiling is not None and estimated > profile.comfort_ceiling:
        needs_review = True
        reasons.append(
            f"estimated cost {estimated:.2f} is well above your usual "
            f"spending (typical {profile.typical:.2f} per run, highest so "
            f"far {profile.peak:.2f})"
        )
    if wallet_balance is not None and estimated > wallet_balance:
        # The wallet may be a slice of the user's true assets: never a cap,
        # always worth a look — the run would need funds the link can't see.
        needs_review = True
        reasons.append(
            f"estimated cost {estimated:.2f} exceeds the linked wallet's "
            f"remaining balance {wallet_balance:.2f}; the linked account "
            "may be partial — top up or confirm to proceed"
        )
    return BudgetVerdict(
        estimated=estimated,
        allowed=allowed,
        needs_review=needs_review,
        reasons=reasons,
        profile=profile,
    )


def enforce_budget(
    verdict: BudgetVerdict, *, review_acknowledged: bool = False
) -> None:
    """Raise if the verdict blocks a run.

    A hard-cap breach refuses outright; review reasons block until the
    caller explicitly acknowledges them. Acknowledgement never overrides
    the cap.
    """
    if not verdict.allowed:
        raise BudgetExceededError("; ".join(verdict.reasons))
    if verdict.needs_review and not review_acknowledged:
        raise ReviewRequiredError("; ".join(verdict.reasons))
