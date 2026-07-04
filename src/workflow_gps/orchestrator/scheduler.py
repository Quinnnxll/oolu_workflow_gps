"""DAG route execution — the readiness scheduler for blueprint partial orders.

``DagRouteRunner`` is a ``WorkflowExecutor`` (drop-in for
``ActionExecutorRouteRunner``) that executes a blueprint's actions as a
dependency DAG instead of a fixed sequence:

- actions whose ``before`` dependencies have all verified run concurrently;
- a failure cascades: every transitive dependent is cancelled, never
  deadlocked (pending nodes whose ancestors can no longer verify are resolved
  eagerly, so the ready-set is never silently empty);
- ``fallback`` edges are dormant routes: the target runs only if its source
  failed, giving a plan a repair branch without a re-synthesis round-trip;
- an optional per-action timeout kills the action through the executor's
  ``cancel`` hook rather than hanging the whole route.

When a ``TraceStore`` is attached, every completed route is recorded as an
execution trace (completion order, per-action verdicts, measured cost), which
is how the planner's statistics grow with use — no separate training step.
"""

from __future__ import annotations

import concurrent.futures
import logging
import time
from datetime import UTC, datetime

from ..knowledge.traces import NodeObservation, TraceStore
from ..skills.models import ActionEvent, ExecutionOutcome, ExecutionStatus
from ..skills.ports import ActionExecutor
from .state import Blueprint, ExecutionRecord, RoutePlan

logger = logging.getLogger(__name__)

_TERMINAL_BAD = frozenset(
    {ExecutionStatus.FAILED, ExecutionStatus.BLOCKED, ExecutionStatus.CANCELLED}
)


def action_node_key(blueprint_name: str, action: ActionEvent) -> str:
    """The stable per-action key used for trace statistics.

    Keyed by route name + adapter/operation (not the volatile ``ActionEvent.id``)
    so statistics accumulate across runs of the same route.
    """
    return f"{blueprint_name}:{action.adapter}/{action.operation}"


def _skipped_outcome(action: ActionEvent, key: str, reason: str) -> ExecutionOutcome:
    now = datetime.now(UTC)
    return ExecutionOutcome(
        idempotency_key=key,
        skill_id=str(action.parameters.get("skill_id", "uncompiled")),
        status=ExecutionStatus.CANCELLED,
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

        blocked = self._preflight(blueprint)
        if blocked is not None:
            return ExecutionRecord(
                idempotency_key=idempotency_key,
                attempt=attempt,
                status=ExecutionStatus.BLOCKED,
                error=blocked,
                started_at=started,
                completed_at=datetime.now(UTC),
            )

        outcomes, error, succeeded = self._run_dag(blueprint, idempotency_key)
        record = ExecutionRecord(
            idempotency_key=idempotency_key,
            attempt=attempt,
            status=ExecutionStatus.SUCCEEDED if succeeded else ExecutionStatus.FAILED,
            action_outcomes=outcomes,
            error=None if succeeded else error,
            started_at=started,
            completed_at=datetime.now(UTC),
        )
        self._record_trace(blueprint, record)
        return record

    # ------------------------------------------------------------------ #
    # Graph derivation + validation.                                      #
    # ------------------------------------------------------------------ #
    def _preflight(self, blueprint: Blueprint) -> str | None:
        """Capability + graph-shape checks; a reason string means BLOCKED."""
        for item in blueprint.actions:
            executor = self._executors.get(item.action.adapter)
            if executor is None or item.action.operation not in executor.capabilities():
                return (
                    "missing executor capability: "
                    f"{item.action.adapter}/{item.action.operation}"
                )
        ids = {item.action.id for item in blueprint.actions}
        for edge in blueprint.edges:
            if edge.source not in ids or edge.target not in ids:
                return f"edge references unknown action: {edge.source}->{edge.target}"
        if self._has_cycle(blueprint):
            return "blueprint dependency graph has a cycle"
        return None

    @staticmethod
    def _dependencies(blueprint: Blueprint) -> dict[str, set[str]]:
        """``before`` dependencies per action id.

        ``ordering="sequential"`` chains actions in list order and layers any
        explicit edges on top (a contradicting SOP edge then surfaces as a
        cycle). ``ordering="graph"`` uses exactly the edges: unrelated actions
        are independent. Fallback targets never join the sequential chain —
        they are dormant repair branches, not steps.
        """
        deps: dict[str, set[str]] = {
            item.action.id: set() for item in blueprint.actions
        }
        for edge in blueprint.edges:
            if edge.relation == "before":
                deps[edge.target].add(edge.source)
        if blueprint.ordering == "sequential":
            fallback_ids = {
                edge.target for edge in blueprint.edges if edge.relation == "fallback"
            }
            previous: str | None = None
            for item in blueprint.actions:
                if item.action.id in fallback_ids:
                    continue
                if previous is not None:
                    deps[item.action.id].add(previous)
                previous = item.action.id
        return deps

    @staticmethod
    def _fallbacks(blueprint: Blueprint) -> dict[str, set[str]]:
        """fallback-target id -> the source ids whose failure activates it."""
        triggers: dict[str, set[str]] = {}
        for edge in blueprint.edges:
            if edge.relation == "fallback":
                triggers.setdefault(edge.target, set()).add(edge.source)
        return triggers

    def _has_cycle(self, blueprint: Blueprint) -> bool:
        deps = self._dependencies(blueprint)
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
    ) -> tuple[list[ExecutionOutcome], str | None, bool]:
        actions = {item.action.id: item.action for item in blueprint.actions}
        deps = self._dependencies(blueprint)
        fallback_triggers = self._fallbacks(blueprint)
        fallback_ids = set(fallback_triggers)
        fallbacks_of: dict[str, set[str]] = {}
        for target, triggers in fallback_triggers.items():
            for trigger in triggers:
                fallbacks_of.setdefault(trigger, set()).add(target)

        status: dict[str, ExecutionStatus] = {}
        pending: set[str] = set(actions) - fallback_ids
        dormant: set[str] = set(fallback_ids)
        outcomes: list[ExecutionOutcome] = []
        first_error: str | None = None
        deadlines: dict[concurrent.futures.Future, float] = {}
        running: dict[concurrent.futures.Future, str] = {}

        def key_for(action_id: str) -> str:
            return f"{idempotency_key}:{action_id}"

        def settle(action_id: str, outcome: ExecutionOutcome) -> None:
            nonlocal first_error
            status[action_id] = outcome.status
            outcomes.append(outcome)
            if outcome.status is not ExecutionStatus.SUCCEEDED and first_error is None:
                first_error = outcome.error or f"action {action_id} failed"

        def activate_fallbacks() -> bool:
            """Resolve dormant fallback targets whose triggers have settled.

            A trigger that terminally failed activates its fallback, and every
            node that depended on the failed trigger is rewritten to depend on
            the fallback instead (substitution: the branch downstream of a
            failure waits for the repair rather than being cancelled). A
            trigger set that fully verified retires its fallback as satisfied.
            """
            progressed = False
            for target in sorted(dormant):
                triggers = fallback_triggers[target]
                failed = {t for t in triggers if status.get(t) in _TERMINAL_BAD}
                if failed:
                    dormant.discard(target)
                    pending.add(target)
                    for node_deps in deps.values():
                        if node_deps & failed:
                            node_deps -= failed
                            node_deps.add(target)
                    progressed = True
                elif all(status.get(t) is ExecutionStatus.SUCCEEDED for t in triggers):
                    # Every trigger verified: the fallback is not needed.
                    dormant.discard(target)
                    status[target] = ExecutionStatus.SUCCEEDED
                    progressed = True
            return progressed

        def cascade_skips() -> bool:
            """Cancel every pending node with a terminally-failed dependency.

            Fallback activation runs first, so a failed dependency with a
            repair branch is substituted rather than cancelled. Runs to a
            fixed point, so the cascade is transitive — a grandchild of a
            failed node is cancelled instead of deadlocking the loop.
            """
            progressed = False
            changed = True
            while changed:
                changed = activate_fallbacks()
                for node in sorted(pending):
                    bad = [d for d in deps[node] if status.get(d) in _TERMINAL_BAD]
                    if bad:
                        pending.discard(node)
                        outcome = _skipped_outcome(
                            actions[node],
                            key_for(node),
                            f"dependency failed: {', '.join(sorted(bad))}",
                        )
                        settle(node, outcome)
                        changed = True
                progressed = progressed or changed
            return progressed

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=self._max_workers
        ) as pool:
            while pending or running or dormant:
                progressed = cascade_skips()

                ready = sorted(
                    node
                    for node in pending
                    if all(
                        status.get(d) is ExecutionStatus.SUCCEEDED for d in deps[node]
                    )
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
                            _skipped_outcome(
                                actions[node],
                                key_for(node),
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

        def effective_ok(node: str, seen: frozenset[str] = frozenset()) -> bool:
            """A node counts as ok if it verified, or a fallback repaired it."""
            if status.get(node) is ExecutionStatus.SUCCEEDED:
                return True
            if node in seen:
                return False
            return any(
                effective_ok(target, seen | {node})
                for target in fallbacks_of.get(node, ())
            )

        succeeded = all(effective_ok(node) for node in status)
        return outcomes, first_error, succeeded

    def _run_action(self, action: ActionEvent, key: str) -> ExecutionOutcome:
        executor = self._executors[action.adapter]
        try:
            return executor.execute(action, idempotency_key=key)
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
