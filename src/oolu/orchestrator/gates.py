"""Gate semantics — the admissions that turn edges into logic gates.

Pure ``recorded state -> decision`` functions, no threading and no executor
imports, so every gate is unit testable — the same discipline as
``graph/edges.py``'s conditional routing, one layer up: that module gates the
inner navigation loop, this one gates a blueprint's actions.

THE VOCABULARY
--------------
An ordering edge (``before`` or ``guard``) renders one of four admissions
about its target, judged purely from the source's recorded status and
evidence:

  * ADMIT    — the source verified (and, for a guard, its evidence satisfies
               the predicate): this edge lets the target run.
  * DECLINE  — the source settled in a way that legitimately does not lead
               here: a guard whose predicate failed, or a source that was
               itself not taken. Declining is not failure.
  * VETO     — the source terminally failed: through this edge the target
               must never run, and "not run" means CANCELLED, not SKIPPED.
  * WAIT     — the source has not settled yet.

The target's ``join`` mode combines its edges' admissions into a readiness
verdict (``ready`` / ``wait`` / ``skip`` / ``cancel``). Three ideas are
encoded in that combination:

  * A veto dominates. Under an all-join a single VETO cancels the target
    immediately — no later admission can outvote it.

  * A decline defers to unsettled edges. Under an all-join a DECLINE settles
    the target as skipped only once every edge has settled without a VETO —
    otherwise "skipped vs cancelled" would depend on which source finished
    first, and the route's verdict would ride a race.

  * An any-join's first ADMIT is final. The target runs either way, so
    first-of affects timing, never the outcome set — replay reads the
    record, not the race.

Guards require recorded evidence, by named rule: a source whose status says
SUCCEEDED but for which no outcome was ever recorded (today: a fallback
retired as not-needed) DECLINES its guard edges — a predicate over evidence
nobody wrote is not a decision, and ``before`` edges still admit such a
source. The predicate itself is ``predicates.check`` — deterministic,
model-free, never raises. No model ever routes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

from ..predicates import check, resolve_pointer
from ..skills.models import ExecutionStatus, Postcondition
from .state import Blueprint, BlueprintEdge

__all__ = [
    "Admission",
    "LoopSpec",
    "Readiness",
    "admit",
    "dependency_edges",
    "derive_loops",
    "loop_decision",
    "readiness",
    "route_verdict",
    "structural_edges",
]

# Statuses through which a source can never admit its dependents.
TERMINAL_BAD = frozenset(
    {ExecutionStatus.FAILED, ExecutionStatus.BLOCKED, ExecutionStatus.CANCELLED}
)


class Admission(str, Enum):
    ADMIT = "admit"
    DECLINE = "decline"
    VETO = "veto"
    WAIT = "wait"


def _check_guard(guard: Postcondition, evidence: dict[str, Any]) -> bool:
    """One guard verdict via the one predicate language — the same
    ``predicates.check`` that judges postconditions and project-graph
    constraints, so "the promise a run is judged by" and "the condition an
    edge admits by" can never drift."""
    return check(evidence, guard.pointer, guard.op, guard.value)


def admit(
    edge: BlueprintEdge,
    source_status: ExecutionStatus | None,
    source_evidence: dict[str, Any] | None,
) -> Admission:
    """The admission one ordering edge renders, from recorded state alone."""
    if source_status in TERMINAL_BAD:
        return Admission.VETO
    if source_status is ExecutionStatus.SKIPPED:
        return Admission.DECLINE  # a source not taken leads nowhere, harmlessly
    if source_status is not ExecutionStatus.SUCCEEDED:
        return Admission.WAIT
    if edge.relation != "guard":
        return Admission.ADMIT
    if source_evidence is None:
        # The named no-evidence rule: SUCCEEDED without a recorded outcome
        # (a retired, unfired fallback) declines every guard sourced on it.
        return Admission.DECLINE
    if _check_guard(edge.guard, source_evidence):
        return Admission.ADMIT
    return Admission.DECLINE


def _decline_reason(
    edge: BlueprintEdge,
    source_evidence: dict[str, Any] | None,
    source_status: ExecutionStatus | None,
) -> str:
    """The worded reason one edge declined — the audit reads why a branch
    didn't run, never guesses."""
    if source_status is ExecutionStatus.SKIPPED:
        return f"source {edge.source} was not taken"
    guard = edge.guard
    if source_evidence is None:
        return (
            f"guard '{guard.name}' declined: no recorded evidence "
            f"from {edge.source}"
        )
    exists, found = resolve_pointer(source_evidence, guard.pointer)
    observed = repr(found) if exists else "nothing"
    expected = f"{guard.pointer} {guard.op}" + (
        "" if guard.op == "exists" else f" {guard.value!r}"
    )
    return f"guard '{guard.name}' declined ({expected}, observed {observed})"


@dataclass(frozen=True)
class Readiness:
    """One target's combined verdict, with the worded reason for skip/cancel."""

    verdict: Literal["ready", "wait", "skip", "cancel"]
    reason: str = ""


def readiness(
    node_id: str,
    in_edges: list[BlueprintEdge],
    join: Literal["all", "any"],
    statuses: dict[str, ExecutionStatus],
    evidence: dict[str, dict[str, Any]],
) -> Readiness:
    """Combine a target's incoming ordering edges under its join mode."""
    if not in_edges:
        return Readiness("ready")

    admissions = [
        admit(edge, statuses.get(edge.source), evidence.get(edge.source))
        for edge in in_edges
    ]
    vetoed = sorted(
        {e.source for e, a in zip(in_edges, admissions) if a is Admission.VETO}
    )
    declines = [
        _decline_reason(e, evidence.get(e.source), statuses.get(e.source))
        for e, a in zip(in_edges, admissions)
        if a is Admission.DECLINE
    ]
    waiting = any(a is Admission.WAIT for a in admissions)

    if join == "any":
        if any(a is Admission.ADMIT for a in admissions):
            return Readiness("ready")
        if waiting:
            return Readiness("wait")
        if vetoed:
            return Readiness("cancel", f"dependency failed: {', '.join(vetoed)}")
        return Readiness("skip", "not taken: " + "; ".join(declines))

    # join == "all"
    if vetoed:
        # Dominant: no later admission can outvote a veto, so cancel now.
        return Readiness("cancel", f"dependency failed: {', '.join(vetoed)}")
    if waiting:
        # A decline must NOT settle the skip while an edge could still veto —
        # the skipped-vs-cancelled verdict would ride a race.
        return Readiness("wait")
    if declines:
        return Readiness("skip", "not taken: " + "; ".join(declines))
    return Readiness("ready")


def dependency_edges(blueprint: Blueprint) -> dict[str, list[BlueprintEdge]]:
    """Every action's incoming ordering edges (``before`` + ``guard``).

    ``ordering="sequential"`` chains actions in list order as synthesized
    plain ``before`` edges and layers the explicit edges on top (a
    contradicting SOP edge then surfaces as a cycle). ``ordering="graph"``
    uses exactly the edges. Fallback edges never join the ordering — they
    are dormant repair branches, not steps.
    """
    edges: dict[str, list[BlueprintEdge]] = {
        item.action.id: [] for item in blueprint.actions
    }
    for edge in blueprint.edges:
        if edge.relation in ("before", "guard"):
            edges[edge.target].append(edge)
    if blueprint.ordering == "sequential":
        fallback_ids = {
            edge.target for edge in blueprint.edges if edge.relation == "fallback"
        }
        previous: str | None = None
        for item in blueprint.actions:
            if item.action.id in fallback_ids:
                continue
            if previous is not None:
                edges[item.action.id].append(
                    BlueprintEdge(source=previous, target=item.action.id)
                )
            previous = item.action.id
    return edges


def structural_edges(blueprint: Blueprint) -> list[BlueprintEdge]:
    """The ordering skeleton: every ``before`` and ``guard`` edge, including
    the synthesized sequential chain. Guards ARE ordering edges; ``loop``
    back-edges and dormant ``fallback`` branches are not — the cycle
    preflight and loop-region derivation both walk exactly this skeleton."""
    return [edge for edges in dependency_edges(blueprint).values() for edge in edges]


@dataclass(frozen=True)
class LoopSpec:
    """One validated loop: a back-edge tail→head over a bounded region.

    ``region`` is every node on a structural path head→tail (head and tail
    included) — the nodes that re-enter on a continue. ``guard`` is the
    CONTINUE condition over the tail's recorded evidence (``None`` = run to
    budget); ``budget`` is the mandatory iteration ceiling."""

    head: str
    tail: str
    region: frozenset[str]
    guard: Postcondition | None
    budget: int


def _reachability(edges: list[BlueprintEdge]) -> dict[str, set[str]]:
    """node -> every node reachable from it over the structural skeleton."""
    children: dict[str, set[str]] = {}
    for edge in edges:
        children.setdefault(edge.source, set()).add(edge.target)
    reach: dict[str, set[str]] = {}

    def walk(node: str) -> set[str]:
        if node in reach:
            return reach[node]
        reach[node] = set()  # cycle backstop; preflight rejects real cycles
        found: set[str] = set()
        for child in children.get(node, ()):
            found.add(child)
            found |= walk(child)
        reach[node] = found
        return found

    for edge in edges:
        walk(edge.source)
    return reach


def derive_loops(blueprint: Blueprint) -> tuple[list[LoopSpec], str | None]:
    """Validate every loop edge and derive its region — ``(loops, problem)``
    in the refusal style: a problem string means the blueprint must not run,
    with the reason by name.

    Refusals: a loop edge whose head does not reach its tail through the
    structural skeleton (not a back-edge); two loop regions that overlap
    without proper nesting (one must contain the other whole, or they must
    share nothing); a fallback edge whose source or target lies inside any
    loop region — the substitution's in-place rewiring cannot be soundly
    rewound by a region reset; and an ordering edge that leaves a region
    from any node but its tail — whether a mid-region exit fires would
    depend on settle-vs-reset timing, and an outcome set must never ride a
    race. A region exits only through its tail."""
    loop_edges = [e for e in blueprint.edges if e.relation == "loop"]
    if not loop_edges:
        return [], None

    skeleton = structural_edges(blueprint)
    reach = _reachability(skeleton)
    loops: list[LoopSpec] = []
    for edge in loop_edges:
        tail, head = edge.source, edge.target
        if head == tail:
            region = frozenset({head})
        elif tail in reach.get(head, set()):
            between = {
                node
                for node in reach.get(head, set())
                if node == tail or tail in reach.get(node, set())
            }
            region = frozenset(between | {head, tail})
        else:
            return [], (
                f"loop edge {tail}->{head} is not a back-edge: "
                f"{head} does not reach {tail}"
            )
        loops.append(
            LoopSpec(
                head=head,
                tail=tail,
                region=region,
                guard=edge.guard,
                budget=edge.max_iterations or 1,
            )
        )

    for i, a in enumerate(loops):
        for b in loops[i + 1 :]:
            shared = a.region & b.region
            if shared and not (a.region < b.region or b.region < a.region):
                return [], (
                    f"loop regions overlap without nesting: "
                    f"{a.tail}->{a.head} and {b.tail}->{b.head} share "
                    f"{len(shared)} node(s) and neither contains the other"
                )

    in_regions: set[str] = set().union(*(spec.region for spec in loops))
    for edge in blueprint.edges:
        if edge.relation != "fallback":
            continue
        if edge.source in in_regions or edge.target in in_regions:
            return [], (
                f"fallback edge {edge.source}->{edge.target} touches a loop "
                "region — fallbacks inside loop regions are refused: a "
                "repair's rewiring cannot be rewound by a region reset"
            )
    for spec in loops:
        for edge in skeleton:
            if (
                edge.source in spec.region
                and edge.source != spec.tail
                and edge.target not in spec.region
            ):
                return [], (
                    f"edge {edge.source}->{edge.target} leaves the loop "
                    f"region of {spec.tail}->{spec.head} from a mid-region "
                    "node — a region exits only through its tail"
                )
    return loops, None


def loop_decision(
    spec: LoopSpec,
    tail_evidence: dict[str, Any] | None,
    completed_passes: int,
) -> Literal["continue", "exit", "exhausted"]:
    """One loop's verdict after its tail verified, from recorded state alone.

    Guard ``None`` means the budget IS the plan: run ``budget`` passes, then
    exit clean. A guard is the CONTINUE condition: when it stops holding the
    loop exits clean; when it still holds with no budget left, the loop is
    EXHAUSTED — a loud failure, never a silent pass."""
    if spec.guard is None:
        return "continue" if completed_passes < spec.budget else "exit"
    if not _check_guard(spec.guard, tail_evidence or {}):
        return "exit"
    return "continue" if completed_passes < spec.budget else "exhausted"


def route_verdict(
    statuses: dict[str, ExecutionStatus], fallbacks_of: dict[str, set[str]]
) -> bool:
    """The route's success: every node verified, was legitimately not taken,
    or had its ENTIRE fallback branch verify — a half-finished repair is not
    a repair, and a skipped branch is not a failure."""

    def effective_ok(node: str, seen: frozenset[str] = frozenset()) -> bool:
        state = statuses.get(node)
        if state is ExecutionStatus.SUCCEEDED or state is ExecutionStatus.SKIPPED:
            return True
        if node in seen:
            return False
        targets = fallbacks_of.get(node)
        if not targets:
            return False
        return all(effective_ok(target, seen | {node}) for target in targets)

    return all(effective_ok(node) for node in statuses)
