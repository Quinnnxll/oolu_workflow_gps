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
from oolu.skills.models import (
    ActionEvent,
    ExecutionOutcome,
    ExecutionStatus,
    Postcondition,
)


class StubExecutor:
    """Deterministic executor: per-operation verdicts, delays, evidence,
    call recording."""

    name = "stub"

    def __init__(
        self,
        capabilities: set[str],
        *,
        fail_operations: set[str] | None = None,
        delays: dict[str, float] | None = None,
        evidence: dict[str, dict] | None = None,
    ):
        self._caps = frozenset(capabilities)
        self._fail = set(fail_operations or ())
        self._delays = dict(delays or {})
        self._evidence = dict(evidence or {})
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
            evidence=self._evidence.get(action.operation, {}),
            error=None if ok else f"{action.operation} exploded",
            started_at=now,
            completed_at=now,
        )

    def cancel(self, idempotency_key: str) -> None:  # pragma: no cover - stub
        pass


def _action(operation: str, join: str = "all") -> ReservedAction:
    return ReservedAction(
        action=ActionEvent(correlation_id="c", adapter="stub", operation=operation),
        required_capabilities=frozenset({operation}),
        join=join,
    )


def _guard(pointer: str, op: str, value=None, name: str = "gate") -> Postcondition:
    return Postcondition(name=name, pointer=pointer, op=op, value=value)


def _statuses(record) -> dict[str, ExecutionStatus]:
    return {
        o.idempotency_key.rsplit(":", 1)[-1]: o.status for o in record.action_outcomes
    }


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


# --------------------------------------------------------------------------- #
# G0 — gate edges: guards, joins, SKIPPED.                                     #
# --------------------------------------------------------------------------- #
def test_guard_admits_and_runs_target():
    executor = StubExecutor({"a", "b"}, evidence={"a": {"rows": 3}})
    blueprint = Blueprint(
        name="guarded", actions=[_action("a"), _action("b")], ordering="graph"
    )
    ids = _ids(blueprint)
    blueprint = blueprint.model_copy(
        update={
            "edges": [
                BlueprintEdge(
                    source=ids["a"],
                    target=ids["b"],
                    relation="guard",
                    guard=_guard("rows", ">", 0, name="has-rows"),
                )
            ]
        }
    )
    record = DagRouteRunner({"stub": executor}).execute(
        _route(blueprint), idempotency_key="k", attempt=1
    )
    assert record.status is ExecutionStatus.SUCCEEDED
    assert executor.order == ["a", "b"]


def test_guard_diamond_takes_exactly_one_branch_and_route_succeeds():
    # a -> (b if rows>0 | c if rows==0) -> d(join="any"): one branch runs.
    executor = StubExecutor({"a", "b", "c", "d"}, evidence={"a": {"rows": 3}})
    blueprint = Blueprint(
        name="diamond",
        actions=[_action("a"), _action("b"), _action("c"), _action("d", join="any")],
        ordering="graph",
    )
    ids = _ids(blueprint)
    blueprint = blueprint.model_copy(
        update={
            "edges": [
                BlueprintEdge(
                    source=ids["a"],
                    target=ids["b"],
                    relation="guard",
                    guard=_guard("rows", ">", 0, name="has-rows"),
                ),
                BlueprintEdge(
                    source=ids["a"],
                    target=ids["c"],
                    relation="guard",
                    guard=_guard("rows", "==", 0, name="empty"),
                ),
                BlueprintEdge(source=ids["b"], target=ids["d"]),
                BlueprintEdge(source=ids["c"], target=ids["d"]),
            ]
        }
    )
    record = DagRouteRunner({"stub": executor}).execute(
        _route(blueprint), idempotency_key="k", attempt=1
    )
    assert record.status is ExecutionStatus.SUCCEEDED
    assert record.error is None
    statuses = _statuses(record)
    assert statuses[ids["c"]] is ExecutionStatus.SKIPPED
    assert executor.order == ["a", "b", "d"]  # c never ran


def test_all_declined_propagates_skip_through_all_join():
    executor = StubExecutor({"a", "b", "c"}, evidence={"a": {"rows": 0}})
    blueprint = Blueprint(
        name="skipchain",
        actions=[_action("a"), _action("b"), _action("c")],
        ordering="graph",
    )
    ids = _ids(blueprint)
    blueprint = blueprint.model_copy(
        update={
            "edges": [
                BlueprintEdge(
                    source=ids["a"],
                    target=ids["b"],
                    relation="guard",
                    guard=_guard("rows", ">", 0, name="has-rows"),
                ),
                BlueprintEdge(source=ids["b"], target=ids["c"]),
            ]
        }
    )
    record = DagRouteRunner({"stub": executor}).execute(
        _route(blueprint), idempotency_key="k", attempt=1
    )
    # The whole branch was legitimately not taken; the route still succeeds.
    assert record.status is ExecutionStatus.SUCCEEDED
    assert record.error is None
    statuses = _statuses(record)
    assert statuses[ids["b"]] is ExecutionStatus.SKIPPED
    assert statuses[ids["c"]] is ExecutionStatus.SKIPPED
    assert executor.order == ["a"]
    # The audit reads WHY each branch didn't run, in words.
    by_id = {
        o.idempotency_key.rsplit(":", 1)[-1]: o.error for o in record.action_outcomes
    }
    assert "not taken: guard 'has-rows' declined" in (by_id[ids["b"]] or "")
    assert "observed 0" in (by_id[ids["b"]] or "")
    assert "not taken" in (by_id[ids["c"]] or "")
    assert "was not taken" in (by_id[ids["c"]] or "")


def test_decline_plus_veto_cancels_under_all_join():
    # d waits on a declined guard AND a failed dependency: the veto wins —
    # d is CANCELLED (a failure upstream), never SKIPPED (a branch not taken).
    executor = StubExecutor(
        {"a", "f", "d"}, fail_operations={"f"}, evidence={"a": {"rows": 0}}
    )
    blueprint = Blueprint(
        name="vetowins",
        actions=[_action("a"), _action("f"), _action("d")],
        ordering="graph",
    )
    ids = _ids(blueprint)
    blueprint = blueprint.model_copy(
        update={
            "edges": [
                BlueprintEdge(
                    source=ids["a"],
                    target=ids["d"],
                    relation="guard",
                    guard=_guard("rows", ">", 0, name="has-rows"),
                ),
                BlueprintEdge(source=ids["f"], target=ids["d"]),
            ]
        }
    )
    record = DagRouteRunner({"stub": executor}).execute(
        _route(blueprint), idempotency_key="k", attempt=1
    )
    assert record.status is ExecutionStatus.FAILED
    statuses = _statuses(record)
    assert statuses[ids["d"]] is ExecutionStatus.CANCELLED


def test_any_join_all_declined_skips_the_target():
    executor = StubExecutor({"a", "d"}, evidence={"a": {"rows": 0}})
    blueprint = Blueprint(
        name="anyskip",
        actions=[_action("a"), _action("d", join="any")],
        ordering="graph",
    )
    ids = _ids(blueprint)
    blueprint = blueprint.model_copy(
        update={
            "edges": [
                BlueprintEdge(
                    source=ids["a"],
                    target=ids["d"],
                    relation="guard",
                    guard=_guard("rows", ">", 0, name="has-rows"),
                ),
                BlueprintEdge(
                    source=ids["a"],
                    target=ids["d"],
                    relation="guard",
                    guard=_guard("rows", "<", 0, name="negative"),
                ),
            ]
        }
    )
    record = DagRouteRunner({"stub": executor}).execute(
        _route(blueprint), idempotency_key="k", attempt=1
    )
    assert record.status is ExecutionStatus.SUCCEEDED
    assert _statuses(record)[ids["d"]] is ExecutionStatus.SKIPPED


def test_guard_on_retired_fallback_declines_by_no_evidence_rule():
    # a succeeds, so its fallback `repair` retires satisfied WITHOUT an
    # outcome. A guard sourced on the retired repair has no evidence to
    # read — it declines by the named rule, and the route still succeeds.
    executor = StubExecutor({"a", "repair", "g"})
    blueprint = Blueprint(
        name="noevidence",
        actions=[_action("a"), _action("repair"), _action("g")],
        ordering="graph",
    )
    ids = _ids(blueprint)
    blueprint = blueprint.model_copy(
        update={
            "edges": [
                BlueprintEdge(
                    source=ids["a"], target=ids["repair"], relation="fallback"
                ),
                BlueprintEdge(
                    source=ids["repair"],
                    target=ids["g"],
                    relation="guard",
                    guard=_guard("anything", "exists", name="ran"),
                ),
            ]
        }
    )
    record = DagRouteRunner({"stub": executor}).execute(
        _route(blueprint), idempotency_key="k", attempt=1
    )
    assert record.status is ExecutionStatus.SUCCEEDED
    assert executor.order == ["a"]
    statuses = _statuses(record)
    assert statuses[ids["g"]] is ExecutionStatus.SKIPPED
    reason = next(
        o.error
        for o in record.action_outcomes
        if o.idempotency_key.rsplit(":", 1)[-1] == ids["g"]
    )
    assert "no recorded evidence" in (reason or "")


def test_guard_requires_graph_ordering():
    executor = StubExecutor({"a", "b"})
    blueprint = Blueprint(name="seqguard", actions=[_action("a"), _action("b")])
    ids = _ids(blueprint)
    blueprint = blueprint.model_copy(
        update={
            "edges": [
                BlueprintEdge(
                    source=ids["a"],
                    target=ids["b"],
                    relation="guard",
                    guard=_guard("rows", ">", 0, name="has-rows"),
                )
            ]
        }
    )
    record = DagRouteRunner({"stub": executor}).execute(
        _route(blueprint), idempotency_key="k", attempt=1
    )
    assert record.status is ExecutionStatus.BLOCKED
    assert "guard edges require ordering='graph'" in (record.error or "")
    assert executor.order == []


def test_skipped_actions_leave_no_trace_observation():
    store = TraceStore(":memory:")
    executor = StubExecutor({"a", "b"}, evidence={"a": {"rows": 0}})
    blueprint = Blueprint(
        name="skiptrace", actions=[_action("a"), _action("b")], ordering="graph"
    )
    ids = _ids(blueprint)
    blueprint = blueprint.model_copy(
        update={
            "edges": [
                BlueprintEdge(
                    source=ids["a"],
                    target=ids["b"],
                    relation="guard",
                    guard=_guard("rows", ">", 0, name="has-rows"),
                )
            ]
        }
    )
    runner = DagRouteRunner({"stub": executor}, trace_store=store)
    record = runner.execute(_route(blueprint), idempotency_key="k1", attempt=1)
    assert record.status is ExecutionStatus.SUCCEEDED

    # The route succeeded and a's run counts; the skipped b left NOTHING —
    # a branch not taken must never move the posterior the planner picks by.
    route_post = store.posterior(route_node_key("skiptrace"))
    assert (route_post.successes, route_post.failures) == (1, 0)
    a_post = store.posterior("skiptrace:stub/a")
    b_post = store.posterior("skiptrace:stub/b")
    assert (a_post.successes, a_post.failures) == (1, 0)
    assert (b_post.successes, b_post.failures) == (0, 0)
