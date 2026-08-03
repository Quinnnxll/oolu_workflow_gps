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
    "Readiness",
    "admit",
    "dependency_edges",
    "readiness",
    "route_verdict",
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
