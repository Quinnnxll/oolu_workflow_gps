"""The launch guard: real charging waits for calm prices and proven code.

A marketplace opening behaves like a coin listing — early prices swing
violently while supply, demand, and damping find each other. Charging real
cards against a price that halves an hour later is how trust dies on day
one. So the guard holds three doors in series, and ALL must be open before
a class of work may charge:

1. **The transaction port** — a deliberate operator switch, off for the
   whole pre-launch. Nothing overrides it.
2. **Price settlement** — the guard watches every cleared price per class
   key; a class is settled only after enough observations land inside a
   narrow band around their median (relative swing below the threshold).
   A violent tick reopens the wait.
3. **Verification** — the platform-verified success count for the class
   must clear a floor: functions charge only after they have provably
   worked for free.

The guard never blocks execution, settlement simulation, or accrual
bookkeeping — only the step where a real card is charged.
"""

from __future__ import annotations

from statistics import median

from pydantic import BaseModel, ConfigDict, Field


class LaunchStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    open: bool
    mode: str  # "pre_launch" | "live"
    reasons: list[str] = Field(default_factory=list)


class LaunchGuard:
    def __init__(
        self,
        *,
        transactions_enabled: bool = False,
        window: int = 8,
        max_relative_swing: float = 0.20,
        min_verified_successes: int = 5,
    ):
        self._enabled = transactions_enabled
        self._window = window
        self._swing = max_relative_swing
        self._min_verified = min_verified_successes
        self._prices: dict[str, list[float]] = {}

    # ------------------------------------------------------------------ #
    # Observations.                                                        #
    # ------------------------------------------------------------------ #
    def record_price(self, class_key: str, price: float) -> None:
        """Feed every cleared price in; only the last `window` matter."""
        history = self._prices.setdefault(class_key, [])
        history.append(float(price))
        del history[: -self._window]

    def price_settled(self, class_key: str) -> bool:
        history = self._prices.get(class_key, [])
        if len(history) < self._window:
            return False
        mid = median(history)
        if mid <= 0:
            return False
        return (max(history) - min(history)) / mid <= self._swing

    # ------------------------------------------------------------------ #
    # The gate.                                                            #
    # ------------------------------------------------------------------ #
    def status(self, class_key: str, *, verified_successes: int = 0) -> LaunchStatus:
        reasons: list[str] = []
        if not self._enabled:
            reasons.append(
                "pre-launch: the real transaction port is not opened"
            )
        if not self.price_settled(class_key):
            observed = len(self._prices.get(class_key, []))
            reasons.append(
                f"price for '{class_key}' has not settled "
                f"({observed}/{self._window} observations in band)"
            )
        if verified_successes < self._min_verified:
            reasons.append(
                f"only {verified_successes}/{self._min_verified} verified "
                "successes — the function is not proven yet"
            )
        return LaunchStatus(
            open=not reasons,
            mode="live" if self._enabled else "pre_launch",
            reasons=reasons,
        )

    def assert_chargeable(
        self, class_key: str, *, verified_successes: int = 0
    ) -> None:
        state = self.status(class_key, verified_successes=verified_successes)
        if not state.open:
            raise LaunchClosedError("; ".join(state.reasons))


class LaunchClosedError(RuntimeError):
    """A real charge was attempted while the launch guard is closed."""
