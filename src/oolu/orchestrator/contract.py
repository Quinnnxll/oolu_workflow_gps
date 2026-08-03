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
    ContractEdge,
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
        owners: dict[str, str] | None = None,
    ):
        self.actions = actions
        self.edges = edges
        self.entries = entries  # action ids with no internal dependencies
        self.exits = exits  # action ids nothing internal depends on
        # action id -> name of the immediate child that contributed it
        # (only populated by subgraph compilation).
        self.owners = owners or {}

    @property
    def ids(self) -> list[str]:
        return [item.action.id for item in self.actions]


def promote_sequential_for_gates(blueprint: Blueprint) -> Blueprint:
    """Materialize a sequential blueprint's implicit chain as explicit
    ``before`` edges and switch it to ``ordering="graph"`` (G2), so a gate
    edge (guard/loop) added on top runs — the scheduler refuses gate edges
    under ``ordering="sequential"``, and this is the order-preserving
    promotion that avoids that refusal.

    A no-op unless the blueprint is sequential AND already carries a gate
    edge: legacy sequential blueprints are untouched, so the promotion
    fires only where a caller (an SOP, a builder) actually added a gate.
    The chain is exactly the one ``gates.dependency_edges`` would synthesize
    for a sequential blueprint — same order, now spelled out."""
    if blueprint.ordering != "sequential":
        return blueprint
    if not any(e.relation in ("guard", "loop") for e in blueprint.edges):
        return blueprint
    fallback_ids = {
        edge.target for edge in blueprint.edges if edge.relation == "fallback"
    }
    seen = {(e.source, e.target, e.relation) for e in blueprint.edges}
    chain: list[BlueprintEdge] = []
    previous: str | None = None
    for item in blueprint.actions:
        if item.action.id in fallback_ids:
            continue
        if previous is not None:
            marker = (previous, item.action.id, "before")
            if marker not in seen:
                chain.append(
                    BlueprintEdge(
                        source=previous,
                        target=item.action.id,
                        relation="before",
                        provenance="learned",
                    )
                )
                seen.add(marker)
        previous = item.action.id
    return blueprint.model_copy(
        update={"edges": list(blueprint.edges) + chain, "ordering": "graph"}
    )


def contract_to_blueprint(
    contract: NodeContract,
    *,
    wire_dataflow: bool = False,
    producer_keys: dict[str, str] | None = None,
) -> Blueprint:
    """The public entry point: one contract, one executable blueprint."""
    blueprint, _owners = compile_with_owners(
        contract, wire_dataflow=wire_dataflow, producer_keys=producer_keys
    )
    return blueprint


def compile_with_owners(
    contract: NodeContract,
    *,
    wire_dataflow: bool = False,
    producer_keys: dict[str, str] | None = None,
) -> tuple[Blueprint, dict[str, str]]:
    """Compile, plus a map from action id to the owning top-level child name.

    Both come from ONE compile pass — script bodies mint a fresh action id
    on every compile, so attribution computed from a second pass would not
    match the blueprint that actually runs. Actions the contract contributes
    directly (non-subgraph bodies, fallback repairs) map to the contract's
    own name. This is what lets per-node execution traces accumulate under
    the same keys the assembler scores by.

    ``wire_dataflow=True`` (W0, off by default so existing callers are
    untouched) additionally binds each subgraph child's unbound consumed
    slots to their sibling producer's output port: an ``output://{key}/{s}``
    reference the deterministic binder resolves at run time to the value the
    producer filed IN THIS RUN. ``producer_keys`` maps a child contract id
    to its canonical producer key (the desk node id for registered nodes);
    an unmapped child keys by its contract id. Only script-bodied producers
    are wired — an ActionsBody outcome need not carry a result payload, and
    an edge whose port can never fill is worse than no edge. Wiring is
    DEPTH-1 only, the same depth value filing attributes at (owners map
    actions to immediate children): a nested subgraph's inner children are
    neither wired nor wireable — wiring deeper than filing would mint
    references nothing ever files (W0.1).
    """
    if wire_dataflow and isinstance(contract.body, SubgraphBody):
        wired_nodes = _wire_dataflow_bindings(
            list(contract.body.nodes), producer_keys or {}
        )
        contract = contract.model_copy(
            update={"body": contract.body.model_copy(update={"nodes": wired_nodes})}
        )
    fragment = _compile(contract)
    owners = dict(fragment.owners)
    for item in fragment.actions:
        owners.setdefault(item.action.id, contract.name)
    blueprint = Blueprint(
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
    return blueprint, owners


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
            owners={**fragment.owners, **repair.owners},
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


def _sole_script_action(contract: NodeContract):
    """The single ``script`` action of an ActionsBody child, or None. A
    published function/program node reconstructs (via ``from_skill``) as
    an ActionsBody carrying exactly this one action WITH its provided
    script inline — a fileable producer whose bindings can be wired (W2),
    exactly like a ScriptBody, just a different body shape."""
    body = contract.body
    if not isinstance(body, ActionsBody) or len(body.actions) != 1:
        return None
    action = body.actions[0]
    return action if action.adapter == "script" else None


def _child_bindings(contract: NodeContract) -> dict:
    """A wireable child's current bindings — the ScriptBody's, or the
    sole script action's ``parameters['bindings']``."""
    if isinstance(contract.body, ScriptBody):
        return dict(contract.body.bindings or {})
    action = _sole_script_action(contract)
    if action is not None:
        return dict(action.parameters.get("bindings") or {})
    return {}


def _with_bindings(contract: NodeContract, additions: dict) -> NodeContract:
    """Return the child with ``additions`` merged into its bindings, on
    whichever body shape it has."""
    if isinstance(contract.body, ScriptBody):
        return contract.model_copy(
            update={
                "body": contract.body.model_copy(
                    update={"bindings": {**contract.body.bindings, **additions}}
                )
            }
        )
    action = _sole_script_action(contract)
    if action is None:  # pragma: no cover - callers gate on wireability
        return contract
    merged = {**(action.parameters.get("bindings") or {}), **additions}
    new_action = action.model_copy(
        update={"parameters": {**action.parameters, "bindings": merged}}
    )
    return contract.model_copy(
        update={"body": contract.body.model_copy(update={"actions": [new_action]})}
    )


def _wireable_producer(contract: NodeContract) -> bool:
    """True iff this child FILES PORT VALUES an ``output://`` edge could
    bind — a ScriptBody or a sole-script-action ActionsBody. A
    cli/http/browser ActionsBody never sources a wired edge (its outcome
    need not carry a result), the same rule W1's survey applies."""
    return isinstance(contract.body, ScriptBody) or (
        _sole_script_action(contract) is not None
    )


def _wire_dataflow_bindings(
    nodes: list[NodeContract], producer_keys: dict[str, str]
) -> list[NodeContract]:
    """Bind each wireable child's unbound consumed slots to their sibling
    producer's output port.

    A slot wires only when exactly ONE fileable sibling produces it —
    ambiguity stays unbound, exactly as today (the assembler picks one
    producer per slot, so assembled subgraphs are never ambiguous). A
    cli/http/browser ActionsBody producer never wires: its outcome need
    not carry a result payload, and an ``output://`` edge whose port can
    never fill would refuse honest runs. A producer whose NAME another
    sibling shares never wires either (W0.1): value filing attributes by
    the owners map, which keys actions by child name — a colliding name
    would file one child's answer under another's key.
    """
    name_counts: dict[str, int] = {}
    for child in nodes:
        name_counts[child.name] = name_counts.get(child.name, 0) + 1
    producers_by_name: dict[str, list[NodeContract]] = {}
    for child in nodes:
        if not _wireable_producer(child):
            continue
        if name_counts.get(child.name, 0) > 1:
            continue
        for slot in child.produces:
            producers_by_name.setdefault(slot.name, []).append(child)

    wired: list[NodeContract] = []
    for child in nodes:
        if not _wireable_producer(child):
            wired.append(child)
            continue
        bound = _child_bindings(child)
        additions: dict[str, str] = {}
        for slot in child.consumes:
            if slot.name in bound:
                continue
            candidates = [
                producer
                for producer in producers_by_name.get(slot.name, [])
                if producer.id != child.id
                and any(produced.matches(slot) for produced in producer.produces)
            ]
            if len(candidates) != 1:
                continue
            key = producer_keys.get(candidates[0].id, candidates[0].id)
            additions[slot.name] = f"output://{key}/{slot.name}"
        if additions:
            child = _with_bindings(child, additions)
        wired.append(child)
    return wired


def _compile_subgraph(contract: NodeContract, body: SubgraphBody) -> _Fragment:
    if not body.nodes:
        raise ValueError(f"contract '{contract.name}' has an empty subgraph body")
    nodes = list(body.nodes)
    fragments = {child.id: _compile(child) for child in nodes}

    actions: list[ReservedAction] = []
    edges: list[BlueprintEdge] = []
    for fragment in fragments.values():
        actions.extend(fragment.actions)
        edges.extend(fragment.edges)

    seen = {(e.source, e.target, e.relation) for e in edges}

    def connect(edge: ContractEdge):
        source, target = fragments[edge.source], fragments[edge.target]
        relation = edge.relation
        if relation == "loop":
            # A child-level loop maps to exactly ONE blueprint LoopSpec, so
            # the tail must be single-exit and the head single-entry (G2).
            # Refuse otherwise, loudly — one edge, one loop region.
            if len(source.exits) != 1 or len(target.entries) != 1:
                raise ValueError(
                    f"loop edge {edge.source}->{edge.target} needs a "
                    "single-exit tail and a single-entry head "
                    f"(got {len(source.exits)} exit(s), "
                    f"{len(target.entries)} entry/entries)"
                )
            endpoints = (source.exits, target.entries)
        elif relation in ("before", "guard"):
            endpoints = (source.exits, target.entries)
        else:  # fallback: the whole repair fragment gates the substitution
            endpoints = (source.ids, target.ids)
        for from_id in endpoints[0]:
            for to_id in endpoints[1]:
                marker = (from_id, to_id, relation)
                if marker not in seen:
                    edges.append(
                        BlueprintEdge(
                            source=from_id,
                            target=to_id,
                            relation=relation,  # type: ignore[arg-type]
                            provenance=edge.provenance,  # type: ignore[arg-type]
                            guard=edge.guard,
                            max_iterations=edge.max_iterations,
                        )
                    )
                    seen.add(marker)

    known = set(fragments)
    for edge in body.edges:
        if edge.source not in known or edge.target not in known:
            raise ValueError(
                f"subgraph edge references unknown child: {edge.source}->{edge.target}"
            )
        connect(edge)
    for edge in derive_data_edges(nodes):
        connect(edge)

    # Joins (G2): a child's declared join mode rides its ENTRY actions —
    # the actions its incoming ordering edges land on. An unmapped child
    # keeps the "all" default. Rebuild the actions carrying the join so
    # the boundary/owners below see the same objects.
    if body.joins:
        join_by_action: dict[str, str] = {}
        for child_id, mode in body.joins.items():
            if child_id not in fragments:
                raise ValueError(
                    f"subgraph join names unknown child: {child_id}"
                )
            for entry_id in fragments[child_id].entries:
                join_by_action[entry_id] = mode
        if join_by_action:
            actions = [
                item.model_copy(update={"join": join_by_action[item.action.id]})
                if item.action.id in join_by_action
                else item
                for item in actions
            ]

    # The subgraph's boundary is derived over the STRUCTURAL edges —
    # ``before`` AND ``guard`` (guards ARE ordering edges, G2). A child
    # reached ONLY via a guard edge is entered, not a boundary entry, so
    # nested composition wires fan-in correctly; ``loop`` back-edges and
    # dormant ``fallback`` branches never define the boundary.
    structural = ("before", "guard")
    targeted = {e.target for e in edges if e.relation in structural}
    sourced = {e.source for e in edges if e.relation in structural}
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
    # Every action a child contributed (however deeply nested) belongs to
    # that child at THIS level — trace attribution is per immediate child.
    owners = {
        aid: child.name for child in nodes for aid in fragments[child.id].ids
    }
    return _Fragment(actions, edges, entries, exits, owners=owners)
