"""Goal-directed assembly: want these slots, get a subgraph contract.

The planner the typed contracts were built for. A ``GoalSpec`` names the
slots the user wants produced and the slots they already have;
``ContractAssembler`` backward-chains through a library of contracts:

1. every wanted slot needs a producer — the assembler picks the best one by
   **verified history** (posterior success mean, then measured cost, then the
   fewest unresolved inputs, then name for determinism; pass an ``rng`` to
   Thompson-sample instead, which personalizes and explores exactly like the
   route optimizer). A ``ProposalModel`` may weigh in, but only as a **prior**:
   its weights enter the same Beta posterior as pseudo-observations, so a
   model's opinion decides thin-history ties and washes out as verified runs
   accumulate — advice never outranks evidence, and a failing model never
   blocks assembly;
2. the chosen producer's ``consumes`` become new wants, minus what is on hand
   and what other selected contracts already produce — recursively, until the
   plan closes;
3. slots nobody can produce are reported as ``missing`` — or, with
   ``fill_gaps_with_scripts=True``, become synthesized ``ScriptBody`` gap
   nodes that the node-cached script runner will realize (and memoize) at
   execution time.

The result is one ``SubgraphBody`` contract with **no explicit edges**:
ordering falls out of slot flow at compile time (``derive_data_edges``), so
whatever can run in parallel will. The assembler selects and closes; the
compiler orders; the scheduler executes; the trace store grades — each layer
keeps its one job.
"""

from __future__ import annotations

import logging
import random
from typing import Callable, Iterable, Protocol, Sequence, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from ..knowledge.traces import TraceStore, route_node_key
from ..skills.contract import (
    NodeContract,
    NodeStats,
    ScriptBody,
    Slot,
    SubgraphBody,
)
from ..skills.registry import RegisteredSkill

logger = logging.getLogger(__name__)

_MAX_ROUNDS = 32  # a plan deeper than this is a modelling error, not a plan

# A full endorsement is worth this many verified observations: enough to
# decide a thin-history tie, few enough that a handful of real runs
# overrides the model's opinion.
DEFAULT_PROPOSAL_STRENGTH = 3.0


class Proposal(BaseModel):
    """A model's opinion about one pick — and what forming it cost.

    ``weights`` maps candidate contract ids to endorsements in ``[0, 1]``
    (1 = strongly recommended, 0 = strongly advised against; an absent id
    is "no opinion"). ``cost`` is what the model call itself cost, so
    planning advice enters the same budget math as the plan it shaped.
    """

    model_config = ConfigDict(frozen=True)

    weights: dict[str, float] = Field(default_factory=dict)
    cost: float = 0.0


@runtime_checkable
class ProposalModel(Protocol):
    """Advises the assembler on producer picks. Advisory by contract:
    implementations may raise or return garbage and assembly still proceeds
    on verified history alone."""

    def propose(
        self,
        *,
        goal: GoalSpec,
        slot: Slot,
        selected: Sequence[str],
        candidates: Sequence[NodeContract],
    ) -> Proposal: ...


class GoalSpec(BaseModel):
    """What the user wants produced, and what they already have."""

    model_config = ConfigDict(frozen=True)

    name: str
    want: list[Slot]
    have: list[Slot] = Field(default_factory=list)


class AssemblyResult(BaseModel):
    """The assembled workflow contract, or the reasons there is none."""

    model_config = ConfigDict(frozen=True)

    contract: NodeContract | None = None
    selected: list[str] = Field(default_factory=list)  # contract names, in pick order
    gap_filled: list[str] = Field(default_factory=list)  # slot names given script nodes
    missing: list[Slot] = Field(default_factory=list)
    # What the proposal model's advice cost, summed over every consulted
    # pick — planning is not free, and budgets get to see that.
    planning_cost: float = 0.0

    @property
    def complete(self) -> bool:
        return self.contract is not None and not self.missing


class ContractAssembler:
    """Backward-chains a goal into a subgraph contract from a library.

    ``contracts`` may be a list or a zero-argument callable returning the
    current library (so a live registry keeps the assembler growing with
    newly learned contracts, like the adaptive planner).
    """

    def __init__(
        self,
        contracts: Iterable[NodeContract] | Callable[[], list[NodeContract]],
        *,
        rng: random.Random | None = None,
        fill_gaps_with_scripts: bool = False,
        proposal_model: ProposalModel | None = None,
        proposal_strength: float = DEFAULT_PROPOSAL_STRENGTH,
    ):
        self._contracts = contracts
        self._rng = rng
        self._fill_gaps = fill_gaps_with_scripts
        self._proposal_model = proposal_model
        self._proposal_strength = proposal_strength

    def _library(self) -> list[NodeContract]:
        if callable(self._contracts):
            return list(self._contracts())
        return list(self._contracts)

    # ------------------------------------------------------------------ #
    def assemble(self, goal: GoalSpec) -> AssemblyResult:
        library = self._library()
        have = list(goal.have)
        selected: dict[str, NodeContract] = {}
        selected_order: list[str] = []
        missing: list[Slot] = []
        frontier: list[Slot] = list(goal.want)
        rounds = 0
        planning_cost = 0.0

        def satisfied(slot: Slot) -> bool:
            if any(owned.matches(slot) for owned in have):
                return True
            return any(
                produced.matches(slot)
                for contract in selected.values()
                for produced in contract.produces
            )

        while frontier and rounds < _MAX_ROUNDS:
            rounds += 1
            slot = frontier.pop(0)
            if satisfied(slot) or any(m == slot for m in missing):
                continue
            producer, advice_cost = self._pick_producer(
                goal, slot, library, selected=selected_order, exclude=set(selected)
            )
            planning_cost += advice_cost
            if producer is None:
                missing.append(slot)
                continue
            selected[producer.id] = producer
            selected_order.append(producer.name)
            frontier.extend(needed for needed in producer.consumes if needed.required)

        if frontier:  # _MAX_ROUNDS tripped: report the remainder honestly
            missing.extend(s for s in frontier if not satisfied(s))

        gap_filled: list[str] = []
        if missing and self._fill_gaps:
            for slot in missing:
                gap = _gap_script(goal, slot)
                selected[gap.id] = gap
                selected_order.append(gap.name)
                gap_filled.append(slot.name)
            missing = []

        if missing or not selected:
            return AssemblyResult(
                contract=None,
                selected=selected_order,
                gap_filled=gap_filled,
                missing=missing or list(goal.want),
                planning_cost=planning_cost,
            )
        return AssemblyResult(
            contract=NodeContract(
                name=goal.name,
                description=f"assembled for goal '{goal.name}'",
                provenance="synthesized",
                consumes=list(goal.have),
                produces=list(goal.want),
                body=SubgraphBody(nodes=list(selected.values())),
            ),
            selected=selected_order,
            gap_filled=gap_filled,
            planning_cost=planning_cost,
        )

    # ------------------------------------------------------------------ #
    def _pick_producer(
        self,
        goal: GoalSpec,
        slot: Slot,
        library: list[NodeContract],
        *,
        selected: list[str],
        exclude: set[str],
    ) -> tuple[NodeContract | None, float]:
        candidates = [
            contract
            for contract in library
            if contract.id not in exclude
            and any(produced.matches(slot) for produced in contract.produces)
        ]
        if not candidates:
            return None, 0.0

        weights, advice_cost = self._proposal(goal, slot, selected, candidates)

        def score(contract: NodeContract) -> tuple:
            stats = contract.stats or NodeStats()
            successes, failures = float(stats.successes), float(stats.failures)
            endorsement = weights.get(contract.id)
            if endorsement is not None:
                # The model's opinion enters the SAME Beta posterior that
                # verified history feeds, as pseudo-observations — a prior
                # that decides thin-history ties and washes out as real
                # evidence accumulates.
                successes += self._proposal_strength * endorsement
                failures += self._proposal_strength * (1.0 - endorsement)
            if self._rng is not None:
                quality = self._rng.betavariate(1.0 + successes, 1.0 + failures)
            else:
                # The posterior mean — identical to stats.success_mean when
                # the model had no opinion, so no-model picks are unchanged.
                quality = (1.0 + successes) / (2.0 + successes + failures)
            cost = stats.cost_ewma if stats.cost_ewma is not None else 1.0
            # Higher quality first; then cheaper; then fewer inputs to chase;
            # then name, so ties are stable across runs.
            return (-quality, cost, len(contract.consumes), contract.name)

        return min(candidates, key=score), advice_cost

    def _proposal(
        self,
        goal: GoalSpec,
        slot: Slot,
        selected: list[str],
        candidates: list[NodeContract],
    ) -> tuple[dict[str, float], float]:
        """Ask the proposal model, defensively. Bad advice becomes no advice.

        A single candidate is never worth a model call — there is no choice
        to make. Unknown ids are dropped, weights clamp to [0, 1], and any
        exception downgrades to verified-history-only assembly: the model
        advises the marketplace, it can never take it down.
        """
        if self._proposal_model is None or len(candidates) < 2:
            return {}, 0.0
        try:
            proposal = self._proposal_model.propose(
                goal=goal,
                slot=slot,
                selected=list(selected),
                candidates=list(candidates),
            )
        except Exception:
            logger.warning(
                "proposal model failed for slot %r; assembling on verified history",
                slot.name,
                exc_info=True,
            )
            return {}, 0.0
        ids = {contract.id for contract in candidates}
        weights: dict[str, float] = {}
        for contract_id, weight in proposal.weights.items():
            if contract_id not in ids:
                continue
            try:
                weights[contract_id] = min(1.0, max(0.0, float(weight)))
            except (TypeError, ValueError):
                continue
        return weights, max(0.0, float(proposal.cost))


def _gap_script(goal: GoalSpec, slot: Slot) -> NodeContract:
    """A synthesized node that must produce the unresolvable slot.

    Executed by ``NodeScriptRunner``: synthesized once through the graph
    engine, verified, then memoized under this node key like any other
    script node.
    """
    detail = f" ({slot.description})" if slot.description else ""
    return NodeContract(
        id=f"gap.{goal.name}.{slot.name}",
        name=f"produce {slot.name}",
        provenance="synthesized",
        produces=[slot],
        body=ScriptBody(
            goal=(
                f"Produce the '{slot.name}' output "
                f"(type: {slot.value_type}"
                + (f", role: {slot.role}" if slot.role else "")
                + f"){detail} for the goal: {goal.name}"
            ),
            node_key=f"gap.{goal.name}.{slot.name}",
        ),
    )


def contract_from_registered(
    registered: RegisteredSkill,
    *,
    trace_store: TraceStore | None = None,
    context: str = "",
) -> NodeContract:
    """A registry entry as an assembler-ready contract with live stats."""
    stats = None
    if trace_store is not None:
        posterior = trace_store.posterior(
            route_node_key(registered.skill.name), context
        )
        stats = NodeStats(
            successes=posterior.successes,
            failures=posterior.failures,
            cost_ewma=trace_store.expected_cost(
                route_node_key(registered.skill.name), context
            ),
        )
    return NodeContract.from_skill(registered.skill, stats=stats)
