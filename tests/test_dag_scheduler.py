"""DAG route execution: readiness scheduling, cascades, fallbacks, budgets."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

from oolu.knowledge import TraceStore, route_node_key
from oolu.orchestrator import (
    Blueprint,
    BlueprintEdge,
    DagRouteRunner,
    ReservedAction,
    RoutePlan,
)
from oolu.skills.models import ActionEvent, ExecutionOutcome, ExecutionStatus


class StubExecutor:
    """Deterministic executor: per-operation verdicts, delays, call recording."""

    name = "stub"

    def __init__(
        self,
        capabilities: set[str],
        *,
        fail_operations: set[str] | None = None,
        delays: dict[str, float] | None = None,
    ):
        self._caps = frozenset(capabilities)
        self._fail = set(fail_operations or ())
        self._delays = dict(delays or {})
        self.order: list[str] = []
        self._lock = threading.Lock()

    def capabilities(self) -> frozenset[str]:
        return self._caps

    def execute(self, action: ActionEvent, *, idempotency_key: str) -> ExecutionOutcome:
        time.sleep(self._delays.get(action.operation, 0))
        with self._lock:
            self.order.append(action.operation)
        ok = action.operation not in self._fail
        now = datetime.now(UTC)
        return ExecutionOutcome(
            idempotency_key=idempotency_key,
            skill_id="s",
            status=ExecutionStatus.SUCCEEDED if ok else ExecutionStatus.FAILED,
            error=None if ok else f"{action.operation} exploded",
            started_at=now,
            completed_at=now,
        )

    def cancel(self, idempotency_key: str) -> None:  # pragma: no cover - stub
        pass


def _action(operation: str) -> ReservedAction:
    return ReservedAction(
        action=ActionEvent(correlation_id="c", adapter="stub", operation=operation),
        required_capabilities=frozenset({operation}),
    )


def _route(blueprint: Blueprint) -> RoutePlan:
    return RoutePlan(chosen=blueprint, alternatives=[], total_cost=0.0)


def _ids(blueprint: Blueprint) -> dict[str, str]:
    return {item.action.operation: item.action.id for item in blueprint.actions}


def test_no_edges_runs_sequentially_in_list_order():
    executor = StubExecutor({"a", "b", "c"})
    blueprint = Blueprint(
        name="seq", actions=[_action("a"), _action("b"), _action("c")]
    )
    runner = DagRouteRunner({"stub": executor})
    record = runner.execute(_route(blueprint), idempotency_key="k", attempt=1)
    assert record.status is ExecutionStatus.SUCCEEDED
    assert executor.order == ["a", "b", "c"]


def test_graph_ordering_runs_independent_branches():
    executor = StubExecutor({"a", "b", "join"}, delays={"a": 0.05})
    blueprint = Blueprint(
        name="fan",
        actions=[_action("a"), _action("b"), _action("join")],
        ordering="graph",
    )
    ids = _ids(blueprint)
    blueprint = blueprint.model_copy(
        update={
            "edges": [
                BlueprintEdge(source=ids["a"], target=ids["join"]),
                BlueprintEdge(source=ids["b"], target=ids["join"]),
            ]
        }
    )
    record = DagRouteRunner({"stub": executor}, max_workers=2).execute(
        _route(blueprint), idempotency_key="k", attempt=1
    )
    assert record.status is ExecutionStatus.SUCCEEDED
    # b (no delay) finished before the delayed a; join ran last.
    assert executor.order == ["b", "a", "join"]


def test_failure_cascades_transitively_without_deadlock():
    executor = StubExecutor({"a", "b", "c"}, fail_operations={"a"})
    blueprint = Blueprint(
        name="chain",
        actions=[_action("a"), _action("b"), _action("c")],
        ordering="graph",
    )
    ids = _ids(blueprint)
    blueprint = blueprint.model_copy(
        update={
            "edges": [
                BlueprintEdge(source=ids["a"], target=ids["b"]),
                BlueprintEdge(source=ids["b"], target=ids["c"]),
            ]
        }
    )
    record = DagRouteRunner({"stub": executor}).execute(
        _route(blueprint), idempotency_key="k", attempt=1
    )
    assert record.status is ExecutionStatus.FAILED
    assert "a exploded" in (record.error or "")
    statuses = {
        o.idempotency_key.rsplit(":", 1)[-1]: o.status for o in record.action_outcomes
    }
    assert statuses[ids["a"]] is ExecutionStatus.FAILED
    # Both the child AND the grandchild are cancelled — no deadlock, no hang.
    assert statuses[ids["b"]] is ExecutionStatus.CANCELLED
    assert statuses[ids["c"]] is ExecutionStatus.CANCELLED
    assert executor.order == ["a"]


def test_fallback_repairs_route_and_downstream_waits_for_it():
    executor = StubExecutor({"a", "repair", "after"}, fail_operations={"a"})
    blueprint = Blueprint(
        name="repairable",
        actions=[_action("a"), _action("repair"), _action("after")],
        ordering="graph",
    )
    ids = _ids(blueprint)
    blueprint = blueprint.model_copy(
        update={
            "edges": [
                BlueprintEdge(
                    source=ids["a"], target=ids["repair"], relation="fallback"
                ),
                BlueprintEdge(source=ids["a"], target=ids["after"]),
            ]
        }
    )
    record = DagRouteRunner({"stub": executor}).execute(
        _route(blueprint), idempotency_key="k", attempt=1
    )
    # a failed but its fallback verified, and `after` was substituted onto the
    # fallback rather than cancelled — the route counts as repaired.
    assert executor.order == ["a", "repair", "after"]
    assert record.status is ExecutionStatus.SUCCEEDED
    assert record.error is None


def test_fallback_stays_dormant_when_source_succeeds():
    executor = StubExecutor({"a", "repair"})
    blueprint = Blueprint(
        name="healthy",
        actions=[_action("a"), _action("repair")],
        ordering="graph",
    )
    ids = _ids(blueprint)
    blueprint = blueprint.model_copy(
        update={
            "edges": [
                BlueprintEdge(
                    source=ids["a"], target=ids["repair"], relation="fallback"
                )
            ]
        }
    )
    record = DagRouteRunner({"stub": executor}).execute(
        _route(blueprint), idempotency_key="k", attempt=1
    )
    assert record.status is ExecutionStatus.SUCCEEDED
    assert executor.order == ["a"]
    assert len(record.action_outcomes) == 1  # the unused fallback never ran


def test_cycle_is_blocked_not_hung():
    executor = StubExecutor({"a", "b"})
    blueprint = Blueprint(
        name="cycle", actions=[_action("a"), _action("b")], ordering="graph"
    )
    ids = _ids(blueprint)
    blueprint = blueprint.model_copy(
        update={
            "edges": [
                BlueprintEdge(source=ids["a"], target=ids["b"]),
                BlueprintEdge(source=ids["b"], target=ids["a"]),
            ]
        }
    )
    record = DagRouteRunner({"stub": executor}).execute(
        _route(blueprint), idempotency_key="k", attempt=1
    )
    assert record.status is ExecutionStatus.BLOCKED
    assert "cycle" in (record.error or "")
    assert executor.order == []


def test_sop_edge_conflicting_with_demonstrated_order_surfaces_as_cycle():
    # sequential ordering chains a -> b; an explicit edge b -> a contradicts it.
    executor = StubExecutor({"a", "b"})
    blueprint = Blueprint(name="conflict", actions=[_action("a"), _action("b")])
    ids = _ids(blueprint)
    blueprint = blueprint.model_copy(
        update={
            "edges": [BlueprintEdge(source=ids["b"], target=ids["a"], provenance="sop")]
        }
    )
    record = DagRouteRunner({"stub": executor}).execute(
        _route(blueprint), idempotency_key="k", attempt=1
    )
    assert record.status is ExecutionStatus.BLOCKED


def test_missing_capability_blocks_before_anything_runs():
    executor = StubExecutor({"a"})
    blueprint = Blueprint(name="nocap", actions=[_action("a"), _action("zz")])
    record = DagRouteRunner({"stub": executor}).execute(
        _route(blueprint), idempotency_key="k", attempt=1
    )
    assert record.status is ExecutionStatus.BLOCKED
    assert "zz" in (record.error or "")
    assert executor.order == []


def test_action_timeout_fails_the_action():
    executor = StubExecutor({"slow"}, delays={"slow": 0.5})
    blueprint = Blueprint(name="slowroute", actions=[_action("slow")])
    record = DagRouteRunner({"stub": executor}, action_timeout_s=0.05).execute(
        _route(blueprint), idempotency_key="k", attempt=1
    )
    assert record.status is ExecutionStatus.FAILED
    assert "timed out" in (record.error or "")


def test_runner_records_traces_so_statistics_grow():
    store = TraceStore(":memory:")
    executor = StubExecutor({"a", "b"}, fail_operations={"b"})
    blueprint = Blueprint(name="traced", actions=[_action("a"), _action("b")])
    runner = DagRouteRunner({"stub": executor}, trace_store=store)
    runner.execute(_route(blueprint), idempotency_key="k1", attempt=1)

    route_post = store.posterior(route_node_key("traced"))
    assert (route_post.successes, route_post.failures) == (0, 1)
    a_post = store.posterior("traced:stub/a")
    b_post = store.posterior("traced:stub/b")
    assert (a_post.successes, a_post.failures) == (1, 0)
    assert (b_post.successes, b_post.failures) == (0, 1)
