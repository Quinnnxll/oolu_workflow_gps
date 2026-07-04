"""Adaptive planning — routes that grow with the registry and the user's runs.

Three pieces close the learning loop around the phase machine, with no model
in the loop and no separate training step:

- ``AdaptivePlanner`` builds candidate blueprints *fresh on every plan* from a
  live ``SkillRegistry``, so a skill learned five minutes ago is a candidate
  route on the next run. Structure comes from three layered sources, kept
  distinguishable by edge provenance: the demonstrated order (the conservative
  ``sequential`` baseline), learned precedence edges from the ``TraceStore``
  (promoted to a real partial order — ``ordering="graph"`` — only once every
  action pair has enough evidence), and human SOP edges compiled on top,
  which evidence can never remove.
- ``ThompsonRouteOptimizer`` chooses among viable blueprints by sampling each
  route's Beta success posterior (personalized by construction: the counts
  are this user's history) instead of a fixed cost sort, with cost as the
  tiebreak. New routes start at Beta(1,1), so they get explored.
- ``TraceFeedbackSink`` records route outcomes back into the ``TraceStore``
  at finalization for setups whose executor is not trace-aware. When the
  executor is a ``DagRouteRunner`` constructed with a ``trace_store``, the
  runner already records richer per-action traces — attach the store to one
  of the two, not both, or successes will be double-counted.
"""

from __future__ import annotations

import random
from typing import Callable, Iterable, Protocol, runtime_checkable

from ..knowledge.traces import TraceStore, route_node_key
from ..skills.models import ReusableSkill
from ..skills.sop import StandardOperatingProcedure, operation_matches
from .planner import _reserved_action
from .scheduler import action_node_key
from .state import (
    Blueprint,
    BlueprintEdge,
    FeedbackRecord,
    RoutePlan,
    SemanticGrounding,
)

# Numeric weights for the risk classes, used against an SOP risk budget.
RISK_WEIGHTS = {"read": 0.1, "write": 0.5, "irreversible": 1.0}


@runtime_checkable
class SkillSource(Protocol):
    """Anything that can list the current latest skills (``SkillRegistry`` does)."""

    def list(self, *, limit: int = 100): ...


# --------------------------------------------------------------------------- #
# SOP -> blueprint compilation.                                               #
# --------------------------------------------------------------------------- #
def apply_sop_to_blueprint(
    blueprint: Blueprint, sop: StandardOperatingProcedure
) -> Blueprint:
    """Compile an SOP's controls into a blueprint (deterministic, idempotent).

    - ``forbid`` without ``unless_approved`` excludes the route; with it, the
      matching actions become reserved (approval-gated).
    - ``approval`` marks matching actions reserved.
    - ``risk_budget``: an over-budget route forces approval on every non-read
      action.
    - ``require_order`` adds hard ``provenance="sop"`` edges; a route missing
      a required step is excluded, never silently reordered.
    """
    if blueprint.excluded:
        return blueprint

    actions = list(blueprint.actions)
    edges = list(blueprint.edges)

    def matching_ids(pattern: str) -> list[str]:
        return [
            item.action.id
            for item in actions
            if operation_matches(
                pattern,
                adapter=item.action.adapter,
                operation=item.action.operation,
            )
        ]

    reserved_ids: set[str] = set()
    for rule in sop.forbid:
        hits = matching_ids(rule.operation)
        if not hits:
            continue
        if not rule.unless_approved:
            return blueprint.model_copy(
                update={
                    "excluded": True,
                    "exclusion_reason": (
                        f"SOP '{sop.name}' forbids operation: {rule.operation}"
                    ),
                }
            )
        reserved_ids.update(hits)

    if sop.approval is not None:
        for pattern in sop.approval.operations:
            reserved_ids.update(matching_ids(pattern))

    if sop.risk_budget is not None:
        total = sum(
            RISK_WEIGHTS.get(item.risk, RISK_WEIGHTS["write"]) for item in actions
        )
        if total > sop.risk_budget:
            reserved_ids.update(
                item.action.id for item in actions if item.risk != "read"
            )

    if reserved_ids:
        actions = [
            item.model_copy(update={"reserved": True})
            if item.action.id in reserved_ids and not item.reserved
            else item
            for item in actions
        ]

    if sop.require_order:
        step_ids: list[list[str]] = []
        for operation in sop.require_order:
            ids = matching_ids(operation)
            if not ids:
                return blueprint.model_copy(
                    update={
                        "excluded": True,
                        "exclusion_reason": (
                            f"SOP '{sop.name}' requires step '{operation}' "
                            "which the route does not contain"
                        ),
                    }
                )
            step_ids.append(ids)
        existing = {(e.source, e.target) for e in edges if e.relation == "before"}
        for earlier, later in zip(step_ids, step_ids[1:]):
            for source in earlier:
                for target in later:
                    if (source, target) not in existing:
                        edges.append(
                            BlueprintEdge(
                                source=source,
                                target=target,
                                relation="before",
                                provenance="sop",
                            )
                        )
                        existing.add((source, target))

    return blueprint.model_copy(update={"actions": actions, "edges": edges})


# --------------------------------------------------------------------------- #
# The planner.                                                                #
# --------------------------------------------------------------------------- #
class AdaptivePlanner:
    """Blueprints from a live skill registry + trace evidence + SOPs.

    ``skills`` may be a ``SkillRegistry`` (queried fresh on every call, so the
    candidate set grows as skills are learned) or any zero-argument callable
    returning the current ``ReusableSkill`` list.
    """

    def __init__(
        self,
        skills: SkillSource | Callable[[], list[ReusableSkill]],
        *,
        trace_store: TraceStore | None = None,
        sops: Iterable[StandardOperatingProcedure] = (),
        trace_context: str = "",
        min_observations: int = 3,
        min_consistency: float = 0.9,
    ):
        self._skills = skills
        self._traces = trace_store
        self._sops = list(sops)
        self._context = trace_context
        self._min_observations = min_observations
        self._min_consistency = min_consistency

    # -- current skills (with tags where the registry provides them) ---- #
    def _current(self) -> list[tuple[ReusableSkill, list[str]]]:
        if callable(self._skills):
            return [(skill, []) for skill in self._skills()]
        current: list[tuple[ReusableSkill, list[str]]] = []
        for entry in self._skills.list():
            if hasattr(entry, "skill"):  # SkillRegistry returns RegisteredSkill
                current.append((entry.skill, list(getattr(entry, "tags", []))))
            else:
                current.append((entry, []))
        return current

    def capabilities(self) -> frozenset[str]:
        caps: set[str] = set()
        for skill, _tags in self._current():
            caps |= {action.operation for action in skill.actions}
        return frozenset(caps)

    def blueprints(self) -> list[Blueprint]:
        plans: list[Blueprint] = []
        for skill, tags in self._current():
            if not skill.actions:
                continue
            blueprint = self._blueprint_for(skill)
            for sop in self._sops:
                if sop.matches(name=skill.name, tags=tags):
                    blueprint = apply_sop_to_blueprint(blueprint, sop)
            plans.append(blueprint)
        return plans

    def _blueprint_for(self, skill: ReusableSkill) -> Blueprint:
        actions = [_reserved_action(action) for action in skill.actions]
        blueprint = Blueprint(
            name=skill.name,
            actions=actions,
            estimated_cost=float(len(actions)),
        )
        if self._traces is None:
            return blueprint

        # Measured cost when available.
        keys = [action_node_key(skill.name, item.action) for item in actions]
        costs = [self._traces.expected_cost(key, self._context) for key in keys]
        if any(cost is not None for cost in costs):
            blueprint = blueprint.model_copy(
                update={
                    "estimated_cost": sum(
                        cost if cost is not None else 1.0 for cost in costs
                    )
                }
            )

        # Learned structure: promote the demonstrated chain to a real partial
        # order only once every action pair has enough shared observations —
        # until then the conservative sequential baseline stands.
        unique_keys = list(dict.fromkeys(keys))
        if len(unique_keys) < 2 or len(unique_keys) != len(keys):
            return blueprint  # duplicate operations: keys are ambiguous
        for i, a in enumerate(unique_keys):
            for b in unique_keys[i + 1 :]:
                ab, ba = self._traces.precedence(a, b)
                if ab + ba < self._min_observations:
                    return blueprint
        learned = self._traces.derive_edges(
            unique_keys,
            min_observations=self._min_observations,
            min_consistency=self._min_consistency,
        )
        id_for = {key: item.action.id for key, item in zip(keys, blueprint.actions)}
        edges = list(blueprint.edges) + [
            BlueprintEdge(
                source=id_for[a],
                target=id_for[b],
                relation="before",
                provenance="learned",
            )
            for a, b in learned
        ]
        return blueprint.model_copy(update={"edges": edges, "ordering": "graph"})


# --------------------------------------------------------------------------- #
# Route choice: Thompson sampling over route posteriors.                      #
# --------------------------------------------------------------------------- #
class ThompsonRouteOptimizer:
    """A ``RouteOptimizer`` that learns which route works *for this user*.

    Each viable blueprint's success probability is sampled from its Beta
    posterior in the trace store; the highest sample wins, with estimated
    cost as the tiebreak. Unproven routes sample from Beta(1,1) and so keep
    being explored; consistently failing routes fade without being hard-coded
    away. Capability grounding still excludes impossible routes outright.
    """

    def __init__(
        self,
        planner: AdaptivePlanner | Callable[[], list[Blueprint]],
        trace_store: TraceStore,
        *,
        context: str = "",
        rng: random.Random | None = None,
    ):
        self._planner = planner
        self._traces = trace_store
        self._context = context
        self._rng = rng or random.Random()

    def _candidates(self) -> list[Blueprint]:
        if isinstance(self._planner, AdaptivePlanner):
            return self._planner.blueprints()
        return self._planner()

    def optimize(self, brief, grounding: SemanticGrounding) -> RoutePlan:
        candidates = self._candidates()
        if not candidates:
            raise ValueError("no blueprints available to optimize")

        resolved = set(grounding.resolved_capabilities)
        scored: list[tuple[float, Blueprint]] = []
        for blueprint in candidates:
            if blueprint.excluded:
                scored.append((-1.0, blueprint))
                continue
            needed: set[str] = set()
            for item in blueprint.actions:
                needed |= set(item.required_capabilities)
            missing = needed - resolved
            if missing:
                scored.append(
                    (
                        -1.0,
                        blueprint.model_copy(
                            update={
                                "excluded": True,
                                "exclusion_reason": "unresolved capabilities: "
                                + ", ".join(sorted(missing)),
                            }
                        ),
                    )
                )
                continue
            sample = self._traces.sample_success(
                route_node_key(blueprint.name), self._context, rng=self._rng
            )
            scored.append((sample, blueprint))

        scored.sort(key=lambda pair: (-pair[0], pair[1].estimated_cost))
        ranked = [blueprint for _score, blueprint in scored]
        viable = [item for item in ranked if not item.excluded]
        chosen = viable[0] if viable else ranked[0]
        alternatives = [item for item in ranked if item.id != chosen.id]
        return RoutePlan(
            chosen=chosen,
            alternatives=alternatives,
            total_cost=chosen.estimated_cost,
        )


# --------------------------------------------------------------------------- #
# Feedback: finalization -> trace store.                                      #
# --------------------------------------------------------------------------- #
class TraceFeedbackSink:
    """A ``FeedbackSink`` that folds route outcomes into the trace store.

    Use this when the executor is not trace-aware. A ``DagRouteRunner`` built
    with a ``trace_store`` already records per-action traces including the
    route outcome — in that case do not also attach this sink, or route
    successes will be counted twice.
    """

    def __init__(
        self,
        trace_store: TraceStore,
        *,
        context: str = "",
        forward: Callable[[FeedbackRecord], None] | None = None,
    ):
        self._traces = trace_store
        self._context = context
        self._forward = forward
        self.records: list[FeedbackRecord] = []

    def learn(self, *, route: RoutePlan, success: bool, summary: str) -> FeedbackRecord:
        self._traces.record_run(
            goal=route.chosen.name,
            steps=[],
            success=success,
            context=self._context,
        )
        record = FeedbackRecord(
            learned_route=route.chosen.name, success=success, notes=summary
        )
        self.records.append(record)
        if self._forward is not None:
            self._forward(record)
        return record
