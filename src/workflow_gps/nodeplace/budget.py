"""Cost-aware assembly budgets: caps, review thresholds, and learned comfort.

Three signals gate what an assembled plan may cost, each with its own
authority:

- a **caller-set hard cap** is absolute — estimates above it are refused
  (:class:`BudgetExceededError`), no acknowledgement can override it;
- a **user-set review threshold** demands an explicit second look — the plan
  is fine to run, but only after the user acknowledges the review
  (:class:`ReviewRequiredError` until they do);
- a **behavioral comfort ceiling** learned from the user's own committed
  spending (recency-weighted median and peak of their bound run grosses): a
  plan well above anything their history demonstrates needs review even when
  no explicit threshold was set. New users (too little history) are never
  judged by it.
  Behavior is judged **per class of goal** when the class has its own
  history: someone who spends lucratively on gifts but keeps everyday
  automation tight is two different spenders, and neither habit should
  loosen — or flag — the other. A class without enough history falls back
  to the global profile (so a first lavish run in a new class gets one
  review, and from then on the class speaks for itself).
  History **decays with recency**: each run back weighs ``recency_decay``
  less, so the typical tracks where spending is trending, and the ceiling
  uses a decaying peak — one lavish run long ago stops waving outliers
  through as it ages, and a user who has tightened gets a ceiling that
  followed them down. ``recency_decay=1.0`` restores flat history.

The linked wallet is deliberately the weakest signal: its remaining balance
may be a small slice of the user's true assets, so it NEVER caps or scales
the budget — an estimate above the balance only adds a review reason (top up
or confirm), and a large balance grants nothing. Behavior is the signal;
balance is a logistics note.
"""

from __future__ import annotations

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
    # and the user's demonstrated (recent) peak — growth within current
    # habit passes free.
    behavior_multiplier: float = 2.0
    min_history: int = 3  # runs before behavior may judge at all
    # Each run back in history weighs this much less. Comfort tracks where
    # spending is trending; 1.0 = flat history (no decay).
    recency_decay: float = Field(default=0.9, gt=0.0, le=1.0)


def _weighted_median(values: list[float], weights: list[float]) -> float:
    """The value at half the total weight (values need not be sorted)."""
    pairs = sorted(zip(values, weights))
    half = sum(weights) / 2.0
    accumulated = 0.0
    for value, weight in pairs:
        accumulated += weight
        if accumulated >= half:
            return float(value)
    return float(pairs[-1][0])  # pragma: no cover - half is always reached


class SpendingProfile(BaseModel):
    """What the user's committed run history says they normally spend.

    Recency-weighted: ``typical`` is the weighted median (recent runs count
    more), ``recent_peak`` is a decaying maximum — the ceiling an aging
    lavish run can still justify — and ``peak`` stays the raw historical
    maximum for honest display.
    """

    model_config = ConfigDict(frozen=True)

    runs: int = 0
    typical: float = 0.0  # recency-weighted median gross per run
    peak: float = 0.0  # raw historical maximum (display)
    recent_peak: float = 0.0  # decayed maximum (drives the ceiling)
    # None until there is enough history to judge anyone by it.
    comfort_ceiling: float | None = None

    @classmethod
    def from_history(
        cls,
        grosses: list[float],
        *,
        multiplier: float = 2.0,
        min_history: int = 3,
        decay: float = 0.9,
    ) -> "SpendingProfile":
        """``grosses`` must be most-recent-first (as ``consumer_spend``
        returns them); each step back in history weighs ``decay`` less."""
        spent = [g for g in grosses if g > 0]
        if not spent:
            return cls()
        weights = [decay**i for i in range(len(spent))]
        typical = _weighted_median(spent, weights)
        recent_peak = max(g * w for g, w in zip(spent, weights))
        ceiling = (
            max(typical * multiplier, recent_peak)
            if len(spent) >= min_history
            else None
        )
        return cls(
            runs=len(spent),
            typical=typical,
            peak=float(max(spent)),
            recent_peak=recent_peak,
            comfort_ceiling=ceiling,
        )


class BudgetVerdict(BaseModel):
    """The assessment a surface renders and a run path enforces."""

    model_config = ConfigDict(frozen=True)

    estimated: float
    allowed: bool = True  # False ONLY when the hard cap is exceeded
    needs_review: bool = False
    reasons: list[str] = Field(default_factory=list)
    profile: SpendingProfile | None = None
    # When the plan has a class and the class has history, behavior was
    # judged by THIS profile, not the global one.
    goal_class: str | None = None
    class_profile: SpendingProfile | None = None


def assess_budget(
    estimated: float,
    *,
    policy: BudgetPolicy | None = None,
    spend_history: list[float] | None = None,
    class_history: list[float] | None = None,
    goal_class: str | None = None,
    wallet_balance: float | None = None,
) -> BudgetVerdict:
    """Judge an estimated plan cost against every configured signal.

    Reasons accumulate (like quote warnings): every tripped signal is
    reported, none overwrites another, so the user sees the whole picture
    in one verdict.

    Behavior is class-first: when ``class_history`` (the caller's runs of
    this same class of goal) is deep enough to judge, it REPLACES the
    global profile for the behavioral check — the birthday-gift class may
    be lavish while everyday automation stays tight, and neither habit
    leaks into the other. Only a class without enough history falls back
    to the global profile.
    """
    policy = policy or BudgetPolicy()
    profile = SpendingProfile.from_history(
        spend_history or [],
        multiplier=policy.behavior_multiplier,
        min_history=policy.min_history,
        decay=policy.recency_decay,
    )
    class_profile = (
        SpendingProfile.from_history(
            class_history or [],
            multiplier=policy.behavior_multiplier,
            min_history=policy.min_history,
            decay=policy.recency_decay,
        )
        if goal_class is not None
        else None
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
    judge, scope = profile, "your usual spending"
    if class_profile is not None and class_profile.comfort_ceiling is not None:
        judge, scope = class_profile, f"your usual spending on {goal_class}"
    if judge.comfort_ceiling is not None and estimated > judge.comfort_ceiling:
        needs_review = True
        reasons.append(
            f"estimated cost {estimated:.2f} is well above {scope} "
            f"(typically {judge.typical:.2f} per run lately; recent "
            f"peak {judge.recent_peak:.2f})"
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
        goal_class=goal_class,
        class_profile=class_profile,
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
