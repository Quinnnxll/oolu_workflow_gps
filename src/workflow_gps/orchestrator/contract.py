"""Compile a ``NodeContract`` into an executable ``Blueprint``.

The last seam of the unification (planning review, item 6): one contract —
whatever its body kind — becomes the partial-order blueprint the
``DagRouteRunner`` already executes:

- an ``ActionsBody`` compiles to its reserved actions chained in
  demonstrated order (explicit edges, ``provenance="learned"``);
- a ``ScriptBody`` compiles to one ``adapter="script"`` action that
  ``NodeScriptRunner`` executes with node-granular caching — the contract id
  is the cache's node key unless the body pins its own;
- a ``SubgraphBody`` flattens recursively: every child contributes its
  fragment, explicit child edges connect exits to entries, and
  ``derive_data_edges`` adds the dependencies implied by slot flow
  (``provenance="data"``) — children with no relation stay parallel;
- a contract's ``fallback`` compiles to real fallback edges from every
  primary action to every repair action, so the scheduler's substitution
  semantics gate dependents on the whole repair, at any nesting level.

Everything compiles with ``ordering="graph"``: the edges ARE the order.
Mutual slot production between two children yields edges both ways, which
the runner's cycle preflight rejects loudly — a contradiction is surfaced,
never silently reordered.
"""

from __future__ import annotations

from ..skills.contract import (
    ActionsBody,
    NodeContract,
    ScriptBody,
    SubgraphBody,
    derive_data_edges,
)
from ..skills.models import ActionEvent
from .planner import _reserved_action
from .state import Blueprint, BlueprintEdge, ReservedAction


class _Fragment:
    """One compiled contract: its actions, edges, and graph boundary."""

    def __init__(
        self,
        actions: list[ReservedAction],
        edges: list[BlueprintEdge],
        entries: list[str],
        exits: list[str],
    ):
        self.actions = actions
        self.edges = edges
        self.entries = entries  # action ids with no internal dependencies
        self.exits = exits  # action ids nothing internal depends on

    @property
    def ids(self) -> list[str]:
        return [item.action.id for item in self.actions]


def contract_to_blueprint(contract: NodeContract) -> Blueprint:
    """The public entry point: one contract, one executable blueprint."""
    fragment = _compile(contract)
    return Blueprint(
        name=contract.name,
        actions=fragment.actions,
        edges=fragment.edges,
        ordering="graph",
        estimated_cost=(
            contract.stats.cost_ewma
            if contract.stats is not None and contract.stats.cost_ewma is not None
            else float(len(fragment.actions))
        ),
    )


def _compile(contract: NodeContract) -> _Fragment:
    body = contract.body
    if isinstance(body, ActionsBody):
        fragment = _compile_actions(contract, body)
    elif isinstance(body, ScriptBody):
        fragment = _compile_script(contract, body)
    elif isinstance(body, SubgraphBody):
        fragment = _compile_subgraph(contract, body)
    else:  # pragma: no cover - the union is closed
        raise ValueError(f"unknown body kind: {body!r}")

    if contract.fallback is not None:
        repair = _compile(contract.fallback)
        edges = fragment.edges + repair.edges
        seen = {(e.source, e.target, e.relation) for e in edges}
        # Every repair action is a fallback target, so dependents of the
        # failed primary wait for the WHOLE repair (its internal before
        # edges keep the repair's own order).
        for action_id in fragment.ids:
            for repair_id in repair.ids:
                marker = (action_id, repair_id, "fallback")
                if marker not in seen:
                    edges.append(
                        BlueprintEdge(
                            source=action_id,
                            target=repair_id,
                            relation="fallback",
                            provenance="learned",
                        )
                    )
                    seen.add(marker)
        # The repair branch is dormant; the primary's boundary stands — the
        # scheduler substitutes dependents onto the repair on failure.
        return _Fragment(
            fragment.actions + repair.actions,
            edges,
            fragment.entries,
            fragment.exits,
        )
    return fragment


def _compile_actions(contract: NodeContract, body: ActionsBody) -> _Fragment:
    if not body.actions:
        raise ValueError(f"contract '{contract.name}' has an empty actions body")
    reserved = [_reserved_action(action) for action in body.actions]
    edges = [
        BlueprintEdge(
            source=earlier.action.id,
            target=later.action.id,
            relation="before",
            provenance="learned",
        )
        for earlier, later in zip(reserved, reserved[1:])
    ]
    return _Fragment(reserved, edges, [reserved[0].action.id], [reserved[-1].action.id])


def _compile_script(contract: NodeContract, body: ScriptBody) -> _Fragment:
    action = ActionEvent(
        correlation_id=contract.id,
        adapter="script",
        operation="run",
        parameters={
            "goal": body.goal,
            "node_key": body.node_key or contract.id,
            "bindings": dict(body.bindings),
        },
    )
    reserved = _reserved_action(action)
    return _Fragment([reserved], [], [action.id], [action.id])


def _compile_subgraph(contract: NodeContract, body: SubgraphBody) -> _Fragment:
    if not body.nodes:
        raise ValueError(f"contract '{contract.name}' has an empty subgraph body")
    fragments = {child.id: _compile(child) for child in body.nodes}

    actions: list[ReservedAction] = []
    edges: list[BlueprintEdge] = []
    for fragment in fragments.values():
        actions.extend(fragment.actions)
        edges.extend(fragment.edges)

    seen = {(e.source, e.target, e.relation) for e in edges}

    def connect(source_id: str, target_id: str, relation: str, provenance: str):
        source, target = fragments[source_id], fragments[target_id]
        endpoints = (
            (source.exits, target.entries)
            if relation == "before"
            # fallback: the whole repair fragment gates the substitution
            else (source.ids, target.ids)
        )
        for from_id in endpoints[0]:
            for to_id in endpoints[1]:
                marker = (from_id, to_id, relation)
                if marker not in seen:
                    edges.append(
                        BlueprintEdge(
                            source=from_id,
                            target=to_id,
                            relation=relation,  # type: ignore[arg-type]
                            provenance=provenance,  # type: ignore[arg-type]
                        )
                    )
                    seen.add(marker)

    known = set(fragments)
    for edge in body.edges:
        if edge.source not in known or edge.target not in known:
            raise ValueError(
                f"subgraph edge references unknown child: {edge.source}->{edge.target}"
            )
        connect(edge.source, edge.target, edge.relation, edge.provenance)
    for edge in derive_data_edges(body.nodes):
        connect(edge.source, edge.target, edge.relation, edge.provenance)

    # The subgraph's boundary: children nothing points at / nothing leaves.
    targeted = {e.target for e in edges if e.relation == "before"}
    sourced = {e.source for e in edges if e.relation == "before"}
    entries = [
        aid
        for fragment in fragments.values()
        for aid in fragment.entries
        if aid not in targeted
    ]
    exits = [
        aid
        for fragment in fragments.values()
        for aid in fragment.exits
        if aid not in sourced
    ]
    if not entries:  # a fully connected cycle: preflight will reject it loudly
        entries = [actions[0].action.id]
    if not exits:
        exits = [actions[-1].action.id]
    return _Fragment(actions, edges, entries, exits)
