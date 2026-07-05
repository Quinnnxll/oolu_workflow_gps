"""Offline replay — planner strategies audition on history before they ship.

Changing how picks are made (greedy vs. Thompson, recency decay, cost
weights, a proposal model) should never be judged by vibes. This harness
evaluates strategies the same way the assembler will use them — the same
posterior math over a ``TraceStore`` each strategy updates as it plays —
against a ``ReplayWorld`` whose arms have known success rates and costs.

Worlds can be fitted **from recorded history** (``ReplayWorld.from_trace_store``:
arm success = the posterior mean your real runs produced, arm cost = the
EWMA you actually paid), and an evaluation runs *phases* of worlds so drift
is first-class: flip an arm's success rate mid-run and watch which strategy
notices. Every strategy replays the identical seeded outcome stream, so a
report compares decisions, not luck.

This is a simulator fitted from traces, not a counterfactual log replay:
it answers "which strategy finds and keeps finding the better arms of the
world my history describes", which is exactly the question a planner
change must answer before it touches real money.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence, runtime_checkable

from .traces import NodeObservation, TraceStore


@dataclass(frozen=True, slots=True)
class Arm:
    """One candidate node as the world knows it: true rate, true cost."""

    success_rate: float
    cost: float = 0.0


class ReplayWorld:
    """Ground truth the strategies cannot see — only sample from."""

    def __init__(self, arms: Mapping[str, Arm]):
        if not arms:
            raise ValueError("a replay world needs at least one arm")
        self._arms = dict(arms)

    @classmethod
    def from_trace_store(
        cls, store: TraceStore, node_keys: Sequence[str], *, context: str = ""
    ) -> "ReplayWorld":
        """A world fitted from recorded history: each arm behaves like the
        caller's own runs say it does."""
        arms: dict[str, Arm] = {}
        for key in node_keys:
            posterior = store.posterior(key, context)
            cost = store.expected_cost(key, context)
            arms[key] = Arm(
                success_rate=posterior.mean, cost=cost if cost is not None else 0.0
            )
        return cls(arms)

    def node_keys(self) -> list[str]:
        return sorted(self._arms)

    def arm(self, node_key: str) -> Arm:
        return self._arms[node_key]

    def best_expected_success(self) -> float:
        return max(arm.success_rate for arm in self._arms.values())

    def sample(self, node_key: str, rng: random.Random) -> tuple[bool, float]:
        arm = self._arms[node_key]
        return rng.random() < arm.success_rate, arm.cost


@runtime_checkable
class BanditStrategy(Protocol):
    """Picks an arm, then learns from what actually happened."""

    @property
    def name(self) -> str: ...
    def pick(self, arms: Sequence[str], rng: random.Random) -> str: ...
    def observe(self, arm: str, ok: bool, cost: float) -> None: ...


class PosteriorStrategy:
    """The assembler's pick math, playing over its own private TraceStore.

    ``explore=True`` Thompson-samples the posterior; ``False`` is greedy on
    the mean. ``cost_weight`` turns the rank into expected utility, and
    ``recency_decay`` discounts the store exactly as the live one would —
    so an evaluation compares precisely the knobs the assembler exposes.
    """

    def __init__(
        self,
        name: str,
        *,
        explore: bool = True,
        cost_weight: float = 0.0,
        recency_decay: float = 1.0,
    ):
        self._name = name
        self._explore = explore
        self._cost_weight = cost_weight
        self._store = TraceStore(":memory:", recency_decay=recency_decay)

    @property
    def name(self) -> str:
        return self._name

    def pick(self, arms: Sequence[str], rng: random.Random) -> str:
        def score(arm: str) -> tuple:
            posterior = self._store.posterior(arm)
            if self._explore:
                quality = rng.betavariate(posterior.alpha, posterior.beta)
            else:
                quality = posterior.mean
            cost = self._store.expected_cost(arm)
            cost = cost if cost is not None else 1.0
            utility = quality - self._cost_weight * cost
            return (-utility, cost, arm)

        return min(arms, key=score)

    def observe(self, arm: str, ok: bool, cost: float) -> None:
        self._store.record_run(
            goal="replay",
            steps=[NodeObservation(arm, ok=ok, cost=cost)],
            success=ok,
        )

    def close(self) -> None:
        self._store.close()


@dataclass(frozen=True, slots=True)
class StrategyReport:
    """How one strategy fared over the whole evaluation."""

    name: str
    rounds: int
    successes: int
    spend: float
    # What an oracle that always picks the best true arm would expect to
    # succeed. regret = oracle_successes - successes: the price of not
    # knowing (and having to learn) the world.
    oracle_successes: float
    picks: dict[str, int]

    @property
    def success_rate(self) -> float:
        return self.successes / self.rounds if self.rounds else 0.0

    @property
    def regret(self) -> float:
        return self.oracle_successes - self.successes


def evaluate(
    phases: Sequence[tuple[ReplayWorld, int]],
    strategies: Sequence[BanditStrategy],
    *,
    seed: int = 0,
) -> dict[str, StrategyReport]:
    """Run every strategy through the same seeded phases and report.

    Each strategy replays an identical outcome stream (same seed, fresh
    rng per strategy), so differences in the reports are differences in
    decisions. Phases model drift: the world an arm lives in can change
    mid-evaluation, and a strategy's report spans all of it.
    """
    reports: dict[str, StrategyReport] = {}
    for strategy in strategies:
        rng = random.Random(seed)
        rounds = successes = 0
        spend = oracle = 0.0
        picks: dict[str, int] = {}
        for world, world_rounds in phases:
            arms = world.node_keys()
            for _ in range(world_rounds):
                choice = strategy.pick(arms, rng)
                ok, cost = world.sample(choice, rng)
                strategy.observe(choice, ok, cost)
                rounds += 1
                successes += 1 if ok else 0
                spend += cost
                oracle += world.best_expected_success()
                picks[choice] = picks.get(choice, 0) + 1
        reports[strategy.name] = StrategyReport(
            name=strategy.name,
            rounds=rounds,
            successes=successes,
            spend=spend,
            oracle_successes=oracle,
            picks=picks,
        )
    return reports
