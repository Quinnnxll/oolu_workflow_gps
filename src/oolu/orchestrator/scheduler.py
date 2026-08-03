"""DAG route execution — the readiness scheduler for blueprint partial orders.

``DagRouteRunner`` is a ``WorkflowExecutor`` (drop-in for
``ActionExecutorRouteRunner``) that executes a blueprint's actions as a
dependency DAG instead of a fixed sequence:

- actions whose ordering edges all admit run concurrently (``gates.py`` holds
  the pure admission semantics: ``before`` edges admit on success, ``guard``
  edges admit on success plus a passing evidence predicate, and a target's
  ``join`` mode combines them — ``"all"`` or first-of ``"any"``);
- a branch legitimately not taken settles SKIPPED (a guard declined) and the
  route still succeeds; a failure cascades: every transitive dependent is
  CANCELLED, never deadlocked (pending nodes whose ancestors can no longer
  verify are resolved eagerly, so the ready-set is never silently empty);
- ``fallback`` edges are dormant routes: the target runs only if its source
  failed, giving a plan a repair branch without a re-synthesis round-trip;
- an optional per-action timeout kills the action through the executor's
  ``cancel`` hook rather than hanging the whole route.

When a ``TraceStore`` is attached, every completed route is recorded as an
execution trace (completion order, per-action verdicts, measured cost), which
is how the planner's statistics grow with use — no separate training step.
SKIPPED actions leave no observation: a branch not taken is not evidence
about the node.
"""

from __future__ import annotations

import concurrent.futures
import logging
import re
import time
from datetime import UTC, datetime

from ..knowledge.traces import NodeObservation, TraceStore
from ..skills.models import (
    ActionEvent,
    ExecutionOutcome,
    ExecutionStatus,
    verify_postconditions,
)
from ..skills.ports import ActionExecutor
from . import gates
from .state import Blueprint, ExecutionRecord, RoutePlan

logger = logging.getLogger(__name__)

_TERMINAL_BAD = gates.TERMINAL_BAD


def action_node_key(blueprint_name: str, action: ActionEvent) -> str:
    """The stable per-action key used for trace statistics.

    Keyed by route name + adapter/operation (not the volatile ``ActionEvent.id``)
    so statistics accumulate across runs of the same route.
    """
    return f"{blueprint_name}:{action.adapter}/{action.operation}"


_ITERATION_MARKER = re.compile(r"#i\d+$")


def strip_iteration_marker(key: str) -> str:
    """Normalize a per-action idempotency key to its iteration-0 form.

    Loop passes key as ``{run}#i{n}:{action_id}`` so every pass settles its
    own outcome; joins over outcomes must fold the passes back onto the one
    action — a loop that ran three times is three observations of the SAME
    node, not one observation each of three strangers. The marker rides the
    run segment, so ``rsplit(":", 1)`` consumers never see it.
    """
    prefix, sep, action_id = key.rpartition(":")
    if not sep:
        return key
    return f"{_ITERATION_MARKER.sub('', prefix)}{sep}{action_id}"


def _action_label(blueprint: Blueprint, action_id: str | None) -> str | None:
    """The human-readable name of one blueprint action: ``adapter/operation``."""
    if action_id is None:
        return None
    for item in blueprint.actions:
        if item.action.id == action_id:
            return f"{item.action.adapter}/{item.action.operation}"
    return None


def _unrun_outcome(
    action: ActionEvent, key: str, status: ExecutionStatus, reason: str
) -> ExecutionOutcome:
    """The settled record of an action that never ran — CANCELLED when a
    dependency failed, SKIPPED when a branch was legitimately not taken."""
    now = datetime.now(UTC)
    return ExecutionOutcome(
        idempotency_key=key,
        skill_id=str(action.parameters.get("skill_id", "uncompiled")),
        status=status,
        error=reason,
        started_at=now,
        completed_at=now,
    )


class DagRouteRunner:
    """Execute a route's actions as a dependency DAG through ``ActionExecutor``s.

    ``max_workers`` bounds concurrency; ``action_timeout_s`` (optional) bounds
    each action. ``trace_store`` (optional) receives one execution trace per
    route so planning statistics grow with every run.
    """

    def __init__(
        self,
        executors: dict[str, ActionExecutor],
        *,
        max_workers: int = 4,
        action_timeout_s: float | None = None,
        trace_store: TraceStore | None = None,
        trace_context: str = "",
        max_total_actions: int = 1000,
    ):
        self._executors = dict(executors)
        self._max_workers = max(1, max_workers)
        self._timeout = action_timeout_s
        self._traces = trace_store
        self._context = trace_context
        # The hard backstop behind the per-loop budgets — the same
        # graceful-ceiling-inside-hard-backstop pattern as the inner loop's
        # max_recalcs inside LangGraph's recursion_limit. Loop budgets halt
        # gracefully with a worded reason long before this trips.
        self._max_total_actions = max(1, max_total_actions)

    def capabilities(self) -> frozenset[str]:
        caps: set[str] = set()
        for executor in self._executors.values():
            caps |= set(executor.capabilities())
        return frozenset(caps)

    # ------------------------------------------------------------------ #
    # WorkflowExecutor.execute                                            #
    # ------------------------------------------------------------------ #
    def execute(
        self, route: RoutePlan, *, idempotency_key: str, attempt: int
    ) -> ExecutionRecord:
        blueprint = route.chosen
        started = datetime.now(UTC)

        blocked, blocked_action = self._preflight(blueprint)
        if blocked is not None:
            return ExecutionRecord(
                idempotency_key=idempotency_key,
                attempt=attempt,
                status=ExecutionStatus.BLOCKED,
                error=blocked,
                failed_action_id=blocked_action,
                failed_action_label=_action_label(blueprint, blocked_action),
                started_at=started,
                completed_at=datetime.now(UTC),
            )

        outcomes, error, failed_id, succeeded = self._run_dag(
            blueprint, idempotency_key
        )
        record = ExecutionRecord(
            idempotency_key=idempotency_key,
            attempt=attempt,
            status=ExecutionStatus.SUCCEEDED if succeeded else ExecutionStatus.FAILED,
            action_outcomes=outcomes,
            error=None if succeeded else error,
            failed_action_id=None if succeeded else failed_id,
            failed_action_label=(
                None if succeeded else _action_label(blueprint, failed_id)
            ),
            started_at=started,
            completed_at=datetime.now(UTC),
        )
        self._record_trace(blueprint, record)
        return record

    # ------------------------------------------------------------------ #
    # Graph derivation + validation.                                      #
    # ------------------------------------------------------------------ #
    def _preflight(self, blueprint: Blueprint) -> tuple[str | None, str | None]:
        """Capability + graph-shape checks; ``(reason, action_id)`` — a reason
        string means BLOCKED, and the action id (when one node is to blame)
        labels the exact node."""
        for item in blueprint.actions:
            executor = self._executors.get(item.action.adapter)
            if executor is None or item.action.operation not in executor.capabilities():
                return (
                    "missing executor capability: "
                    f"{item.action.adapter}/{item.action.operation}",
                    item.action.id,
                )
        ids = {item.action.id for item in blueprint.actions}
        for edge in blueprint.edges:
            if edge.source not in ids or edge.target not in ids:
                return (
                    f"edge references unknown action: {edge.source}->{edge.target}",
                    None,
                )
        if blueprint.ordering == "sequential":
            for edge in blueprint.edges:
                if edge.relation == "guard":
                    return (
                        "guard edges require ordering='graph': guard "
                        f"'{edge.guard.name}' on {edge.source}->{edge.target} "
                        "cannot ride the sequential chain",
                        None,
                    )
                if edge.relation == "loop":
                    return (
                        "loop edges require ordering='graph': loop "
                        f"{edge.source}->{edge.target} cannot ride the "
                        "sequential chain",
                        None,
                    )
        if self._has_cycle(blueprint):
            return "blueprint dependency graph has a cycle", None
        _, loop_problem = gates.derive_loops(blueprint)
        if loop_problem is not None:
            return loop_problem, None
        return None, None

    @staticmethod
    def _fallbacks(blueprint: Blueprint) -> dict[str, set[str]]:
        """fallback-target id -> the source ids whose failure activates it."""
        triggers: dict[str, set[str]] = {}
        for edge in blueprint.edges:
            if edge.relation == "fallback":
                triggers.setdefault(edge.target, set()).add(edge.source)
        return triggers

    def _has_cycle(self, blueprint: Blueprint) -> bool:
        deps = {
            target: {edge.source for edge in edges}
            for target, edges in gates.dependency_edges(blueprint).items()
        }
        resolved: set[str] = set()
        while True:
            ready = [n for n, d in deps.items() if n not in resolved and d <= resolved]
            if not ready:
                return len(resolved) != len(deps)
            resolved.update(ready)

    # ------------------------------------------------------------------ #
    # The readiness loop.                                                 #
    # ------------------------------------------------------------------ #
    def _run_dag(
        self, blueprint: Blueprint, idempotency_key: str
    ) -> tuple[list[ExecutionOutcome], str | None, str | None, bool]:
        actions = {item.action.id: item.action for item in blueprint.actions}
        joins = {item.action.id: item.join for item in blueprint.actions}
        in_edges = gates.dependency_edges(blueprint)
        fallback_triggers = self._fallbacks(blueprint)
        fallback_ids = set(fallback_triggers)
        fallbacks_of: dict[str, set[str]] = {}
        for target, triggers in fallback_triggers.items():
            for trigger in triggers:
                fallbacks_of.setdefault(trigger, set()).add(target)
        # Preflight already refused malformed loops; innermost-first order so
        # a nested loop settles its own passes before its encloser looks.
        loops, _ = gates.derive_loops(blueprint)
        loop_order = sorted(range(len(loops)), key=lambda i: len(loops[i].region))

        status: dict[str, ExecutionStatus] = {}
        # Recorded evidence per settled action — what guards read. A node
        # whose status was written without an outcome (a retired fallback)
        # has no entry here, which is exactly the no-evidence decline rule.
        evidence: dict[str, dict] = {}
        pending: set[str] = set(actions) - fallback_ids
        dormant: set[str] = set(fallback_ids)
        outcomes: list[ExecutionOutcome] = []
        first_error: str | None = None
        first_failed: str | None = None
        deadlines: dict[concurrent.futures.Future, float] = {}
        running: dict[concurrent.futures.Future, str] = {}
        # Loop bookkeeping. iteration: a monotone per-node reset counter that
        # keys each pass's outcomes uniquely (nested resets keep bumping it —
        # it is an idempotency counter, not a pass number). loop_passes: the
        # per-spec count of completed passes, judged against the budget.
        iteration: dict[str, int] = {}
        loop_passes: dict[int, int] = {i: 0 for i in range(len(loops))}
        loops_done: set[int] = set()
        tail_events: set[int] = set()
        submitted = 0

        def key_for(action_id: str) -> str:
            n = iteration.get(action_id, 0)
            if n == 0:
                return f"{idempotency_key}:{action_id}"
            # The marker rides the run segment, so rsplit(":", 1) still
            # yields the action id — the plan view's parse holds unmodified.
            return f"{idempotency_key}#i{n}:{action_id}"

        def settle(action_id: str, outcome: ExecutionOutcome) -> None:
            nonlocal first_error, first_failed
            status[action_id] = outcome.status
            evidence[action_id] = outcome.evidence
            outcomes.append(outcome)
            if outcome.status is ExecutionStatus.SUCCEEDED:
                for i, spec in enumerate(loops):
                    if spec.tail == action_id and i not in loops_done:
                        tail_events.add(i)
            bad = (
                outcome.status is not ExecutionStatus.SUCCEEDED
                and outcome.status is not ExecutionStatus.SKIPPED
            )
            if bad and first_error is None:
                first_error = outcome.error or f"action {action_id} failed"
                first_failed = action_id

        def resolve_loops() -> bool:
            """Judge every loop whose tail just verified — innermost first.

            A continue clears the region's statuses and evidence, re-adds it
            to pending with fresh iteration keys, and resets any loop nested
            inside (a fresh enclosing pass grants the inner loop a fresh
            budget). Exhaustion settles the TAIL as FAILED with the worded
            reason — loud, never a silent pass — and prior iterations'
            outcomes stay in the append-only record either way.
            """
            nonlocal first_error, first_failed
            progressed = False
            for i in loop_order:
                if i not in tail_events:
                    continue
                tail_events.discard(i)
                spec = loops[i]
                loop_passes[i] += 1
                verdict = gates.loop_decision(
                    spec, evidence.get(spec.tail), loop_passes[i]
                )
                progressed = True
                if verdict == "continue":
                    for node in sorted(spec.region):
                        status.pop(node, None)
                        evidence.pop(node, None)
                        pending.add(node)
                        iteration[node] = iteration.get(node, 0) + 1
                    for j, other in enumerate(loops):
                        if j == i:
                            continue
                        if other.region <= spec.region:
                            loop_passes[j] = 0
                            loops_done.discard(j)
                        if other.tail in spec.region:
                            tail_events.discard(j)
                elif verdict == "exhausted":
                    loops_done.add(i)
                    status[spec.tail] = ExecutionStatus.FAILED
                    reason = (
                        "loop budget exhausted after "
                        f"{spec.budget} iterations"
                    )
                    if first_error is None:
                        first_error = reason
                        first_failed = spec.tail
                else:  # exit — clean, the region stands as it settled
                    loops_done.add(i)
            return progressed

        def node_readiness(node: str) -> gates.Readiness:
            return gates.readiness(
                node, in_edges[node], joins[node], status, evidence
            )

        def activate_fallbacks() -> bool:
            """Resolve dormant fallback targets whose triggers have settled.

            A trigger that terminally failed activates its fallback branch,
            and every ordering edge sourced on the failed trigger is rewritten
            onto ALL of that trigger's fallback targets (substitution: the
            branch downstream of a failure waits for the *whole* repair — a
            multi-step repair gates dependents on its last step, not its
            first). A trigger set that fully verified — or was legitimately
            never taken — retires its fallback as satisfied, with no outcome
            recorded: a guard sourced on a retired fallback declines by the
            no-evidence rule.
            """
            progressed = False
            substitutions: dict[str, set[str]] = {}
            for target in sorted(dormant):
                triggers = fallback_triggers[target]
                failed = {t for t in triggers if status.get(t) in _TERMINAL_BAD}
                if failed:
                    dormant.discard(target)
                    pending.add(target)
                    for trigger in failed:
                        substitutions.setdefault(trigger, set()).add(target)
                    progressed = True
                elif all(
                    status.get(t)
                    in (ExecutionStatus.SUCCEEDED, ExecutionStatus.SKIPPED)
                    for t in triggers
                ):
                    dormant.discard(target)
                    status[target] = ExecutionStatus.SUCCEEDED
                    progressed = True
            if substitutions:
                for node, node_edges in in_edges.items():
                    if not any(e.source in substitutions for e in node_edges):
                        continue
                    rewritten: list = []
                    for edge in node_edges:
                        repair_targets = substitutions.get(edge.source)
                        if repair_targets is None:
                            rewritten.append(edge)
                            continue
                        for repair in sorted(repair_targets):
                            rewritten.append(
                                edge.model_copy(update={"source": repair})
                            )
                    in_edges[node] = rewritten
            return progressed

        def resolve_unrunnable() -> bool:
            """Settle every pending node whose gates have decided it will
            never run — CANCELLED on a veto, SKIPPED on an all-declined.

            Fallback activation runs first, so a failed dependency with a
            repair branch is substituted rather than cancelled. Runs to a
            fixed point, so cascades are transitive — a grandchild of a
            failed node cancels, a consumer of a skipped branch skips,
            instead of deadlocking the loop.
            """
            progressed = False
            changed = True
            while changed:
                changed = activate_fallbacks()
                for node in sorted(pending):
                    verdict = node_readiness(node)
                    if verdict.verdict == "cancel":
                        pending.discard(node)
                        settle(
                            node,
                            _unrun_outcome(
                                actions[node],
                                key_for(node),
                                ExecutionStatus.CANCELLED,
                                verdict.reason,
                            ),
                        )
                        changed = True
                    elif verdict.verdict == "skip":
                        pending.discard(node)
                        settle(
                            node,
                            _unrun_outcome(
                                actions[node],
                                key_for(node),
                                ExecutionStatus.SKIPPED,
                                verdict.reason,
                            ),
                        )
                        changed = True
                progressed = progressed or changed
            return progressed

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=self._max_workers
        ) as pool:
            # tail_events keeps the loop alive: a tail that settles on the
            # last in-flight future must still get its continue/exit verdict.
            while pending or running or dormant or tail_events:
                # Loops first: a continue must clear its region BEFORE any
                # readiness pass could hand the tail's success to a node
                # downstream of the loop — the main loop is single-threaded,
                # so nothing observes the tail between its settle and here.
                looped = resolve_loops()
                progressed = resolve_unrunnable() or looped

                ready = sorted(
                    node
                    for node in pending
                    if node_readiness(node).verdict == "ready"
                )
                for node in ready:
                    pending.discard(node)
                    if submitted >= self._max_total_actions:
                        # The hard backstop tripped: refuse to run more,
                        # loudly — dependents cascade-cancel from here.
                        settle(
                            node,
                            _unrun_outcome(
                                actions[node],
                                key_for(node),
                                ExecutionStatus.CANCELLED,
                                "route budget exhausted: max_total_actions="
                                f"{self._max_total_actions}",
                            ),
                        )
                        continue
                    submitted += 1
                    status[node] = ExecutionStatus.PLANNED
                    future = pool.submit(self._run_action, actions[node], key_for(node))
                    running[future] = node
                    if self._timeout is not None:
                        deadlines[future] = time.monotonic() + self._timeout

                if not running:
                    if progressed or ready:
                        continue
                    # No node ran, settled, or resolved this pass: whatever is
                    # left waits on something that can never settle (e.g. a
                    # mutual wait between a node and its own fallback trigger).
                    for node in sorted(pending | dormant):
                        pending.discard(node)
                        dormant.discard(node)
                        settle(
                            node,
                            _unrun_outcome(
                                actions[node],
                                key_for(node),
                                ExecutionStatus.CANCELLED,
                                "unsatisfiable dependencies",
                            ),
                        )
                    break

                wait_timeout = None
                if deadlines:
                    wait_timeout = max(0.0, min(deadlines.values()) - time.monotonic())
                done, _ = concurrent.futures.wait(
                    running.keys(),
                    timeout=wait_timeout,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )

                now = time.monotonic()
                expired = [
                    future
                    for future in list(running)
                    if future not in done and deadlines.get(future, now + 1) <= now
                ]
                for future in expired:
                    node = running.pop(future)
                    deadlines.pop(future, None)
                    executor = self._executors[actions[node].adapter]
                    executor.cancel(key_for(node))
                    now_dt = datetime.now(UTC)
                    settle(
                        node,
                        ExecutionOutcome(
                            idempotency_key=key_for(node),
                            skill_id=str(
                                actions[node].parameters.get("skill_id", "uncompiled")
                            ),
                            status=ExecutionStatus.FAILED,
                            error=f"action timed out after {self._timeout}s",
                            started_at=now_dt,
                            completed_at=now_dt,
                        ),
                    )

                for future in done:
                    node = running.pop(future)
                    deadlines.pop(future, None)
                    settle(node, future.result())

        succeeded = gates.route_verdict(status, fallbacks_of)
        return outcomes, first_error, first_failed, succeeded

    def _run_action(self, action: ActionEvent, key: str) -> ExecutionOutcome:
        executor = self._executors[action.adapter]
        try:
            # The evaluator rides every hand: a success that breaks the
            # action's declared postconditions is demoted to a failure
            # with the exact broken promise in words.
            return verify_postconditions(
                action, executor.execute(action, idempotency_key=key)
            )
        except Exception as exc:  # an executor bug must not wedge the route
            logger.exception(
                "executor raised for %s/%s", action.adapter, action.operation
            )
            now = datetime.now(UTC)
            return ExecutionOutcome(
                idempotency_key=key,
                skill_id=str(action.parameters.get("skill_id", "uncompiled")),
                status=ExecutionStatus.FAILED,
                error=f"executor raised: {exc}",
                started_at=now,
                completed_at=now,
            )

    # ------------------------------------------------------------------ #
    # Trace recording — the growth loop.                                  #
    # ------------------------------------------------------------------ #
    def _record_trace(self, blueprint: Blueprint, record: ExecutionRecord) -> None:
        if self._traces is None:
            return
        by_key = {
            f"{record.idempotency_key}:{item.action.id}": item.action
            for item in blueprint.actions
        }
        steps: list[NodeObservation] = []
        for outcome in record.action_outcomes:  # completion order
            action = by_key.get(strip_iteration_marker(outcome.idempotency_key))
            if action is None:
                continue
            if outcome.status is ExecutionStatus.SKIPPED:
                # A branch not taken is not an observation of the node —
                # recording it would poison the posterior the planner picks by.
                continue
            cost = None
            if outcome.completed_at is not None:
                cost = (outcome.completed_at - outcome.started_at).total_seconds()
            steps.append(
                NodeObservation(
                    node_key=action_node_key(blueprint.name, action),
                    ok=outcome.status is ExecutionStatus.SUCCEEDED,
                    cost=cost,
                )
            )
        self._traces.record_run(
            goal=blueprint.name,
            steps=steps,
            success=record.status is ExecutionStatus.SUCCEEDED,
            context=self._context,
        )
