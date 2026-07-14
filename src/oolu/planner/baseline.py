"""The modest baseline in the node-token planning seat — and the seam it fills.

A 30B transformer is not what proves the idea; a working autoregressive
planner over node tokens is. :class:`MarkovPlanner` is that proof, in pure
Python: trained on verified runs, it *generates* a whole plan for a goal by
rolling out next-node tokens — the "route like a sentence" claim, made to run
with no framework at all. It is deliberately the humble end of the ladder
(a goal-conditioned back-off bigram over node tokens); ``torch_model.py``'s
transformer trains on the same sequences and must beat this before it earns
its inference bill, exactly as ``TinyTransformerProposalModel`` had to beat
the counting baseline.

Two capabilities, one model:

- :meth:`MarkovPlanner.plan` — the new one. Given a goal, emit an ordered
  node plan autoregressively. This is whole-mission planning in one cheap
  pass, the thing per-slot ranking cannot do and the thing that matters for
  cold start and long-horizon ("vision level") missions.
- :class:`PlannerProposalModel` — the safe one. It adapts the same
  next-node distribution to the assembler's ``ProposalModel`` protocol, so
  the generative planner enters the marketplace as *bounded advice* (its
  endorsements fold into the Beta posterior at ``DEFAULT_PROPOSAL_STRENGTH``,
  unknown ids drop, exceptions downgrade to evidence-only). The type system
  still enumerates and verifies every route; the planner only proposes. That
  containment is what lets a generative model live safely beside the proof's
  "a model must never author a route" thesis — its plan is a prior, never a
  commitment.
"""

from __future__ import annotations

import random
from typing import Sequence

from ..knowledge.traces import RecordedRun, TraceStore
from ..skills.contract import NodeContract, Slot
from .vocab import DEFAULT_GOAL_BUCKETS, goal_token

# Sentinels used only inside the count tables (never node keys themselves).
_BOS = "\x00bos"
_EOS = "\x00eos"

# Below this many verified runs the planner has learned nothing worth saying.
MIN_TRAINING_RUNS = 3


class MarkovPlanner:
    """A goal-conditioned back-off bigram over node tokens.

    Counts are kept in four tables of decreasing specificity; a distribution
    is read from the most specific table that has evidence (stupid-backoff).
    Trained only on verified runs — an unverified plan is not an example of
    good planning.
    """

    def __init__(self, *, goal_buckets: int = DEFAULT_GOAL_BUCKETS):
        self._goal_buckets = goal_buckets
        self._goal_bigram: dict[tuple[str, str], dict[str, float]] = {}
        self._goal_unigram: dict[str, dict[str, float]] = {}
        self._global_bigram: dict[str, dict[str, float]] = {}
        self._global_unigram: dict[str, float] = {}
        self._runs_seen = 0

    # ------------------------------------------------------------------ #
    # Training.                                                          #
    # ------------------------------------------------------------------ #
    def observe(self, goal: str, node_keys: Sequence[str]) -> None:
        """Fold one verified plan into the counts."""
        gt = goal_token(goal, buckets=self._goal_buckets)
        tokens = [_BOS, *node_keys, _EOS]
        for prev, nxt in zip(tokens, tokens[1:]):
            _bump(self._goal_bigram.setdefault((gt, prev), {}), nxt)
            _bump(self._goal_unigram.setdefault(gt, {}), nxt)
            _bump(self._global_bigram.setdefault(prev, {}), nxt)
            _bump(self._global_unigram, nxt)
        self._runs_seen += 1

    def train(
        self, runs: Sequence[RecordedRun], *, only_successful: bool = True
    ) -> int:
        """Train on recorded runs; returns how many were used."""
        used = 0
        for run in runs:
            if only_successful and not run.success:
                continue
            self.observe(run.goal, [step.node_key for step in run.steps])
            used += 1
        return used

    @classmethod
    def from_store(
        cls,
        store: TraceStore,
        *,
        context: str = "",
        limit: int = 500,
        goal_buckets: int = DEFAULT_GOAL_BUCKETS,
    ) -> "MarkovPlanner":
        planner = cls(goal_buckets=goal_buckets)
        planner.train(store.runs(context=context, limit=limit))
        return planner

    @property
    def runs_seen(self) -> int:
        return self._runs_seen

    # ------------------------------------------------------------------ #
    # The autoregressive head.                                           #
    # ------------------------------------------------------------------ #
    def next_distribution(
        self, goal: str, prefix: Sequence[str]
    ) -> dict[str, float]:
        """P(next node | goal, prefix) by stupid-backoff; may include ``_EOS``.

        Empty when the model has no evidence at any level — an honest "no
        opinion", not a uniform guess.
        """
        gt = goal_token(goal, buckets=self._goal_buckets)
        prev = prefix[-1] if prefix else _BOS
        for table in (
            self._goal_bigram.get((gt, prev)),
            self._goal_unigram.get(gt),
            self._global_bigram.get(prev),
            self._global_unigram,
        ):
            if table:
                return _normalize(table)
        return {}

    def plan(
        self,
        goal: str,
        *,
        max_len: int = 32,
        rng: random.Random | None = None,
        greedy: bool = True,
    ) -> list[str]:
        """Generate an ordered node plan for a goal, autoregressively.

        Rolls out next-node tokens until ``_EOS``, ``max_len``, or a dead end.
        A node already in the plan is not emitted again (workflows rarely
        repeat a node, and it forecloses trivial loops). ``greedy`` takes the
        argmax; otherwise samples from the distribution with ``rng``.
        """
        chooser = rng or random.Random()
        plan: list[str] = []
        for _ in range(max_len):
            dist = self.next_distribution(goal, plan)
            dist = {
                key: weight
                for key, weight in dist.items()
                if key == _EOS or key not in plan
            }
            if not dist:
                break
            choice = _argmax(dist) if greedy else _sample(dist, chooser)
            if choice == _EOS:
                break
            plan.append(choice)
        return plan


class PlannerProposalModel:
    """Adapts a :class:`MarkovPlanner` to the assembler's ``ProposalModel``.

    A candidate's endorsement is the planner's belief that it is the next
    node given the goal and the plan so far (``selected``). Candidates the
    model has no evidence for are omitted — no opinion, never a zero that
    would read as "advised against". Never raises: advice is optional by the
    port's contract, and any failure returns ``{}``.
    """

    def __init__(
        self,
        store: TraceStore | None = None,
        *,
        context: str = "",
        max_runs: int = 500,
        goal_buckets: int = DEFAULT_GOAL_BUCKETS,
    ):
        self._store = store
        self._context = context
        self._max_runs = max_runs
        self._goal_buckets = goal_buckets
        self._planner = MarkovPlanner(goal_buckets=goal_buckets)
        self._trained_on = -1

    def _refresh(self) -> int:
        if self._store is None:
            return self._planner.runs_seen
        runs = self._store.runs(context=self._context, limit=self._max_runs)
        if len(runs) != self._trained_on:
            self._planner = MarkovPlanner(goal_buckets=self._goal_buckets)
            self._planner.train(runs)
            self._trained_on = len(runs)
        return self._planner.runs_seen

    def propose(
        self,
        *,
        goal,
        slot: Slot,
        selected: Sequence[str],
        candidates: Sequence[NodeContract],
    ):
        from ..orchestrator.assembler import Proposal  # local: avoid a cycle

        try:
            if self._refresh() < MIN_TRAINING_RUNS:
                return Proposal(weights={}, cost=0.0)
            dist = self._planner.next_distribution(goal.name, list(selected))
            if not dist:
                return Proposal(weights={}, cost=0.0)
            peak = max(dist.values()) or 1.0
            weights: dict[str, float] = {}
            for candidate in candidates:
                weight = dist.get(_route_key(candidate.name))
                if weight is None:
                    continue  # no evidence: no opinion
                weights[candidate.id] = min(1.0, weight / peak)
            return Proposal(weights=weights, cost=0.0)
        except Exception:  # noqa: BLE001 - advice is optional by contract
            return Proposal(weights={}, cost=0.0)


def _route_key(name: str) -> str:
    from ..knowledge.traces import route_node_key

    return route_node_key(name)


def _bump(table: dict[str, float], key: str, amount: float = 1.0) -> None:
    table[key] = table.get(key, 0.0) + amount


def _normalize(table: dict[str, float]) -> dict[str, float]:
    total = sum(table.values()) or 1.0
    return {key: value / total for key, value in table.items()}


def _argmax(dist: dict[str, float]) -> str:
    return max(dist.items(), key=lambda kv: (kv[1], kv[0]))[0]


def _sample(dist: dict[str, float], rng: random.Random) -> str:
    items = sorted(dist.items())
    total = sum(weight for _, weight in items) or 1.0
    threshold = rng.random() * total
    cumulative = 0.0
    for key, weight in items:
        cumulative += weight
        if cumulative >= threshold:
            return key
    return items[-1][0]
