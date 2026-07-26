"""Commerce risk (M2): deterministic signals derived from the order book.

M0 took risk facts as caller-supplied typed inputs; this module is where the
host starts DERIVING them. Everything here is rule-based arithmetic over the
durable order store — no model, no randomness — so the same history always
yields the same signals, and the policy ladder they feed stays a pure
function end to end.

Three derivations, three consumers:

- **Spend totals** feed the daily/monthly autonomy budgets.
- **Counterparty familiarity and reputation** feed the first-time trigger
  and the ladder's one reputation lever (the auto-execution trust bar —
  never an explicit constraint).
- **Risk signals** — duplicate orders, self-dealing, refund abuse, price
  anomaly — feed the fraud/anomaly scores whose thresholds the ladder
  already enforces (elevated → strong approval; past the deny bar →
  refused deterministically).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

from .orders import OrderStore, StoredOrder

# States that count as committed spend for the autonomy budgets. Everything
# a card could still be charged for counts; only never-happened orders
# (cancelled before fulfillment) do not.
_SPEND_STATES = frozenset(
    {
        "payment_authorizing",
        "confirmed",
        "fulfilling",
        "delivered",
        "accepted",
        "completed",
        "refund_pending",
        "refunded",
        "disputed",
        "resolved",
    }
)

_GOOD_END = frozenset({"completed", "accepted", "resolved"})
_BAD_END = frozenset({"refunded", "disputed"})


class RiskSignals(BaseModel):
    """What the deterministic checks concluded, with every reason named."""

    model_config = ConfigDict(frozen=True)

    fraud_score: float = Field(ge=0.0, le=1.0)
    anomaly_score: float = Field(ge=0.0, le=1.0)
    reasons: tuple[str, ...] = ()


class OrderHistory:
    """The spine's history port, read from the durable order book."""

    def __init__(self, conn) -> None:
        self._orders = OrderStore(conn)

    def _mine(self, *, tenant: str, principal: str) -> list[StoredOrder]:
        return [
            order
            for order in self._orders.list_for(tenant=tenant, principal=principal)
            if order.record.buyer_principal == principal
        ]

    def spend_totals(
        self, *, tenant: str, principal: str, now: datetime
    ) -> tuple[int, int]:
        """(daily, monthly) committed spend in micros — the budget inputs."""
        daily = monthly = 0
        for order in self._mine(tenant=tenant, principal=principal):
            if order.state not in _SPEND_STATES:
                continue
            age = now - order.record.created_at
            total = order.record.offer.total_micros
            if age <= timedelta(days=1):
                daily += total
            if age <= timedelta(days=30):
                monthly += total
        return daily, monthly

    def seen_counterparty(
        self, *, tenant: str, principal: str, counterparty: str
    ) -> bool:
        """A counterparty is familiar only after an order actually ENDED
        well — an authorized-but-unfulfilled order proves nothing."""
        return any(
            order.record.seller_id == counterparty and order.state in _GOOD_END
            for order in self._mine(tenant=tenant, principal=principal)
        )

    def reputation(self, *, counterparty: str) -> float | None:
        """The seller's earned rate of good endings across the whole book.

        An operator aggregate (rates only — no buyer data crosses tenants).
        None until the seller has enough finished history to judge."""
        ended = [
            order.state
            for order in self._orders.for_seller(seller_id=counterparty)
            if order.state in (_GOOD_END | _BAD_END)
        ]
        if len(ended) < 3:
            return None
        good = sum(1 for state in ended if state in _GOOD_END)
        return good / len(ended)

    def assess(
        self,
        *,
        tenant: str,
        principal: str,
        counterparty: str,
        amount_micros: int,
        price_benchmark_micros: int | None,
        now: datetime,
    ) -> RiskSignals:
        """The deterministic commerce checks, scored and named."""
        fraud = 0.0
        anomaly = 0.0
        reasons: list[str] = []
        if principal == counterparty:
            fraud += 0.9
            reasons.append("buyer and seller are the same principal")
        mine = self._mine(tenant=tenant, principal=principal)
        recent_same = [
            order
            for order in mine
            if order.record.seller_id == counterparty
            and order.record.offer.total_micros == amount_micros
            and order.state in _SPEND_STATES
            and now - order.record.created_at <= timedelta(hours=1)
        ]
        if recent_same:
            fraud += 0.4
            reasons.append(
                "an identical order to this seller exists within the hour"
            )
        ended = [order for order in mine if order.state in (_GOOD_END | _BAD_END)]
        if len(ended) >= 3:
            bad = sum(1 for order in ended if order.state in _BAD_END)
            if bad / len(ended) > 0.5:
                fraud += 0.3
                reasons.append("the buyer's refund/dispute rate is elevated")
        if (
            price_benchmark_micros
            and amount_micros > 3 * price_benchmark_micros
        ):
            anomaly += 0.5
            reasons.append("the price is far outside the benchmark")
        return RiskSignals(
            fraud_score=min(fraud, 1.0),
            anomaly_score=min(anomaly, 1.0),
            reasons=tuple(reasons),
        )
