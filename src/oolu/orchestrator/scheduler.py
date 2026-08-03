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
    ):
        self._executors = dict(executors)
        self._max_workers = max(1, max_workers)
        self._timeout = action_timeout_s
        self._traces = trace_store
        self._context = trace_context

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
        if self._has_cycle(blueprint):
            return "blueprint dependency graph has a cycle", None
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

        def key_for(action_id: str) -> str:
            return f"{idempotency_key}:{action_id}"

        def settle(action_id: str, outcome: ExecutionOutcome) -> None:
            nonlocal first_error, first_failed
            status[action_id] = outcome.status
            evidence[action_id] = outcome.evidence
            outcomes.append(outcome)
            bad = (
                outcome.status is not ExecutionStatus.SUCCEEDED
                and outcome.status is not ExecutionStatus.SKIPPED
            )
            if bad and first_error is None:
                first_error = outcome.error or f"action {action_id} failed"
                first_failed = action_id

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
            while pending or running or dormant:
                progressed = resolve_unrunnable()

                ready = sorted(
                    node
                    for node in pending
                    if node_readiness(node).verdict == "ready"
                )
                for node in ready:
                    pending.discard(node)
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
            action = by_key.get(outcome.idempotency_key)
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
