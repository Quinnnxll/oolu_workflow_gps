"""Price formation for the Nodeplace — how a node's ask becomes a cleared price.

The noder's static ``PricingPolicy.unit_price`` stays the *ask*; this module
turns asks into **cleared prices** through four deterministic forces, applied
in order:

1. **Cost floor.** A price never clears below the node's automation cost plus
   a minimum margin — nobody is silently subsidized into negative-margin work.
2. **Competition pull.** A class with many quality-comparable substitutes is a
   commodity: the cleared target is pulled toward the class reference price
   with a class-dependent sensitivity (strong for commodities, weak for
   professional work). Scarce supply keeps pricing power; crowded supply
   converges — the "commodity decay" the marketplace needs to stay a bargain.
3. **Value anchor.** The price is capped at a fraction of the value created
   for the consumer (time saved x rate), so automation stays visibly cheaper
   than doing the work by hand.
4. **Damping.** The cleared price moves from the persisted reference by at
   most a per-class band per period (no shocks), via an EMA whose horizon is
   also per class. Reference prices live in SQLite, so damping is stable
   across processes and restarts.

**Regulated pass-through is exempt.** Government fees, monopoly audits, and
third-party company invoices are never floored, pulled, anchored, damped, or
marked up — they flow through at face value on their own invoice lines.

Ranking (`utility`) deliberately uses only **platform-verified** statistics —
the success posterior from metered runs and the ratings-derived reputation —
never the noder's self-declared quality, which would be gameable. The
effective price is retry-adjusted (`price / p(success)`): an unreliable node
is expensive even when its sticker price is low, which is what makes the
router's economics honest.
"""

from __future__ import annotations

import sqlite3
import threading
from enum import Enum
from math import exp, log
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..persistence import Migration, migrate

MARKET_SCHEMA_VERSION = 1


class NodeClass(str, Enum):
    """The pricing class of a node — how much market force applies to it."""

    COMMODITY = "commodity"  # interchangeable utility (file conversion, ...)
    WORKFLOW = "workflow"  # differentiated multi-step automation
    PROFESSIONAL = "professional"  # licensed/expert human-backed service
    REGULATED = "regulated"  # government fee / monopoly audit / company invoice


class QuoteMode(str, Enum):
    BUDGET = "budget"
    STANDARD = "standard"
    PREMIUM = "premium"
    CERTIFIED = "certified"


class CostVector(BaseModel):
    """The platform's own cost of running a node once (currency units)."""

    model_config = ConfigDict(frozen=True)

    model: float = 0.0
    cli: float = 0.0
    api: float = 0.0
    compute: float = 0.0
    storage: float = 0.0
    verification: float = 0.0
    retry: float = 0.0
    risk: float = 0.0
    support: float = 0.0
    external_invoice: float = 0.0  # pass-through, never part of automation cost

    @property
    def automation_cost(self) -> float:
        return (
            self.model
            + self.cli
            + self.api
            + self.compute
            + self.storage
            + self.verification
            + self.retry
            + self.risk
            + self.support
        )

    @property
    def total_cost(self) -> float:
        return self.automation_cost + self.external_invoice

    def __add__(self, other: "CostVector") -> "CostVector":
        return CostVector(
            **{
                name: getattr(self, name) + getattr(other, name)
                for name in type(self).model_fields
            }
        )


class DampingPolicy(BaseModel):
    """Per-class smoothing: EMA horizon plus a max movement band per period."""

    model_config = ConfigDict(frozen=True)

    horizon_days: float
    max_up_per_period: float
    max_down_per_period: float
    period_days: float = 30.0
    competition_sensitivity: float = 0.0  # how hard substitutes pull the price
    min_margin: float = 0.15  # cost floor = automation_cost * (1 + this)
    max_value_share: float = 0.35  # price <= this fraction of user value

    def alpha(self, days_elapsed: float) -> float:
        return 1.0 - exp(-max(days_elapsed, 0.0) / self.horizon_days)

    def band(self, reference: float, days_elapsed: float) -> tuple[float, float]:
        periods = max(days_elapsed, 0.0) / self.period_days
        return (
            reference * (1.0 - self.max_down_per_period) ** periods,
            reference * (1.0 + self.max_up_per_period) ** periods,
        )


DEFAULT_POLICIES: dict[NodeClass, DampingPolicy] = {
    NodeClass.COMMODITY: DampingPolicy(
        horizon_days=90.0,
        max_up_per_period=0.08,
        max_down_per_period=0.20,
        competition_sensitivity=0.60,
    ),
    NodeClass.WORKFLOW: DampingPolicy(
        horizon_days=30.0,
        max_up_per_period=0.20,
        max_down_per_period=0.30,
        competition_sensitivity=0.30,
    ),
    NodeClass.PROFESSIONAL: DampingPolicy(
        horizon_days=60.0,
        max_up_per_period=0.25,
        max_down_per_period=0.10,
        competition_sensitivity=0.05,
        max_value_share=0.60,  # expert work may capture more of its value
    ),
}


def competition_index(
    substitutes: int, *, quality_parity: float = 1.0, saturation: int = 4
) -> float:
    """How commoditized a class is, in [0, 1).

    ``substitutes`` counts *other* active nodes in the same class key;
    ``quality_parity`` in [0, 1] discounts substitutes that are not actually
    comparable. Saturates: the difference between 8 and 20 substitutes is
    economically small.
    """
    effective = max(0, substitutes) * min(max(quality_parity, 0.0), 1.0)
    return effective / (effective + saturation)


def estimate_user_value(minutes_saved: float, hourly_rate: float) -> float:
    """The value anchor: what the run is worth to the consumer."""
    return max(0.0, minutes_saved) / 60.0 * max(0.0, hourly_rate)


class ClearedPrice(BaseModel):
    """A cleared price plus every force that shaped it (explainability)."""

    model_config = ConfigDict(frozen=True)

    schema_version: int = MARKET_SCHEMA_VERSION
    class_key: str
    node_class: NodeClass
    ask: float
    cleared: float
    reference_before: float | None
    cost_floor: float
    competition: float
    value_cap: float | None
    notes: list[str] = Field(default_factory=list)


def _create(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS market_reference_prices (
               class_key TEXT PRIMARY KEY,
               price REAL NOT NULL,
               updated_at_days REAL NOT NULL DEFAULT 0
           )"""
    )


def _drop(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS market_reference_prices")


PRICE_BOOK_MIGRATIONS: tuple[Migration, ...] = (Migration(up=_create, down=_drop),)


class PriceBook:
    """Persisted per-class reference prices + the clearing computation."""

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        policies: dict[NodeClass, DampingPolicy] | None = None,
    ):
        self._lock = threading.RLock()
        location = (
            str(path) if str(path) == ":memory:" else str(Path(path).expanduser())
        )
        self._db = sqlite3.connect(location, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._policies = dict(policies or DEFAULT_POLICIES)
        with self._lock:
            migrate(self._db, PRICE_BOOK_MIGRATIONS, label="price-book")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def reference(self, class_key: str) -> float | None:
        with self._lock:
            row = self._db.execute(
                "SELECT price FROM market_reference_prices WHERE class_key = ?",
                (class_key,),
            ).fetchone()
        return None if row is None else row["price"]

    def clear(
        self,
        *,
        class_key: str,
        node_class: NodeClass,
        ask: float,
        cost: CostVector,
        substitutes: int = 0,
        quality_parity: float = 1.0,
        user_value: float | None = None,
        days_elapsed: float = 30.0,
    ) -> ClearedPrice:
        """Clear one ask through floor -> competition -> anchor -> damping."""
        notes: list[str] = []

        if node_class is NodeClass.REGULATED:
            # Pass-through: face value, no forces, no reference tracking.
            return ClearedPrice(
                class_key=class_key,
                node_class=node_class,
                ask=ask,
                cleared=ask,
                reference_before=None,
                cost_floor=0.0,
                competition=0.0,
                value_cap=None,
                notes=["regulated pass-through: cleared at face value"],
            )

        policy = self._policies[node_class]
        floor = cost.automation_cost * (1.0 + policy.min_margin)
        competition = competition_index(substitutes, quality_parity=quality_parity)

        # Competition pulls the target below the ask; never below the floor.
        target = ask * (1.0 - competition * policy.competition_sensitivity)
        if target < floor:
            target = floor
            notes.append("cost floor engaged")

        value_cap: float | None = None
        if user_value is not None and user_value > 0:
            value_cap = user_value * policy.max_value_share
            if target > value_cap:
                target = max(value_cap, floor)
                notes.append("value anchor engaged")

        with self._lock:
            reference = self.reference(class_key)
            if reference is None:
                cleared = target
                notes.append("first observation: target becomes the reference")
            else:
                low, high = policy.band(reference, days_elapsed)
                damped = reference * (1.0 - policy.alpha(days_elapsed)) + (
                    target * policy.alpha(days_elapsed)
                )
                cleared = min(max(damped, low), high)
                if cleared != damped:
                    notes.append("damping band engaged")
                cleared = max(cleared, floor)
            self._db.execute(
                """INSERT OR REPLACE INTO market_reference_prices
                   (class_key, price, updated_at_days) VALUES (?, ?, ?)""",
                (class_key, cleared, days_elapsed),
            )
            self._db.commit()

        return ClearedPrice(
            class_key=class_key,
            node_class=node_class,
            ask=ask,
            cleared=cleared,
            reference_before=reference,
            cost_floor=floor,
            competition=competition,
            value_cap=value_cap,
            notes=notes,
        )


# --------------------------------------------------------------------------- #
# Route economics: verified stats -> mode-weighted utility.                    #
# --------------------------------------------------------------------------- #
class CandidateEconomics(BaseModel):
    """Everything the router needs to score one candidate node for one step.

    ``verified_successes``/``verified_failures`` come from the metering ledger
    (platform-verified runs), never from noder self-declaration. ``reputation``
    is the ratings-derived mu in [0, mu_max].
    """

    model_config = ConfigDict(frozen=True)

    version_id: str
    noder_principal: str
    node_class: NodeClass
    class_key: str
    cleared_price: float
    cost: CostVector = Field(default_factory=CostVector)
    verified_successes: int = 0
    verified_failures: int = 0
    reputation: float = 1.0
    latency_seconds: float = 1.0
    difficulty: float = 1.0
    scarcity: float = 1.0
    liability: float = 1.0

    @property
    def success_mean(self) -> float:
        """Posterior mean under Beta(1,1) — unproven nodes sit at 0.5."""
        return (1.0 + self.verified_successes) / (
            2.0 + self.verified_successes + self.verified_failures
        )

    @property
    def effective_price(self) -> float:
        """Retry-adjusted price: expected spend until one verified success."""
        return (self.cleared_price + self.cost.external_invoice) / max(
            self.success_mean, 0.05
        )


# (price_weight, quality_weight, latency_weight) per mode.
MODE_WEIGHTS: dict[QuoteMode, tuple[float, float, float]] = {
    QuoteMode.BUDGET: (1.45, 0.85, 0.6),
    QuoteMode.STANDARD: (1.00, 1.00, 1.0),
    QuoteMode.PREMIUM: (0.70, 1.30, 1.4),
    QuoteMode.CERTIFIED: (0.55, 1.60, 1.0),
}


def utility(candidate: CandidateEconomics, mode: QuoteMode) -> float:
    """Mode-weighted utility from verified quality per retry-adjusted dollar."""
    price_w, quality_w, latency_w = MODE_WEIGHTS[mode]
    quality = (
        max(candidate.success_mean, 0.01) * max(min(candidate.reputation, 2.0), 0.01)
    ) ** quality_w
    expertise = (
        max(candidate.difficulty, 1.0)
        * max(candidate.scarcity, 1.0)
        * max(candidate.liability, 1.0)
    ) ** 0.2
    price_penalty = max(candidate.effective_price, 0.001) ** price_w
    latency_penalty = (
        1.0 + latency_w * log(1.0 + max(candidate.latency_seconds, 0.0)) / 20.0
    )
    return quality * expertise / (price_penalty * latency_penalty)


def rank_candidates(
    candidates: list[CandidateEconomics], mode: QuoteMode
) -> list[CandidateEconomics]:
    """Candidates sorted best-first for the mode (stable for equal scores)."""
    return sorted(candidates, key=lambda c: utility(c, mode), reverse=True)
