"""Node-granular script caching: hit, miss, drift repair, verification gate."""

from __future__ import annotations

from oolu.cache import LocalScriptCache
from oolu.models import ExecutionResult, Phase
from oolu.orchestrator import (
    Blueprint,
    DagRouteRunner,
    ReservedAction,
    RoutePlan,
)
from oolu.runtime import (
    GraphEngineSynthesizer,
    NodeScriptRunner,
    NodeSynthesis,
    StubBackend,
    render_node_goal,
)
from oolu.skills.models import ActionEvent, ExecutionStatus


def _ok(payload=None):
    return lambda request: ExecutionResult(
        phase=Phase.EXECUTE,
        exit_code=0,
        stdout="",
        contract_ok=True,
        contract_payload=payload or {"answer": 42},
    )


def _boom(request):
    return ExecutionResult(
        phase=Phase.EXECUTE, exit_code=1, stderr="RuntimeError: boom", contract_ok=False
    )


class CountingSynthesizer:
    def __init__(self, script="print('v1')", *, fail=False):
        self.calls: list[str] = []
        self._script = script
        self._fail = fail

    def synthesize(self, goal, *, session_id):
        self.calls.append(goal)
        if self._fail:
            return None
        return NodeSynthesis(script=self._script, dependencies=("pandas",))


def _action(goal="convert the June report", *, bindings=None, node_key=None):
    parameters = {"goal": goal}
    if bindings is not None:
        parameters["bindings"] = bindings
    if node_key is not None:
        parameters["node_key"] = node_key
    return ActionEvent(
        correlation_id="c", adapter="script", operation="run", parameters=parameters
    )


def _runner(backend, cache, synthesizer=None, **kwargs):
    return NodeScriptRunner(
        backend, cache, synthesizer=synthesizer, backend_kind="stub", **kwargs
    )


def test_miss_synthesizes_verifies_and_caches_then_hits_without_synthesis():
    cache = LocalScriptCache(":memory:")
    synthesizer = CountingSynthesizer()
    backend = StubBackend([_ok(), _ok()])
    runner = _runner(backend, cache, synthesizer)

    first = runner.execute(_action(), idempotency_key="k1")
    assert first.status is ExecutionStatus.SUCCEEDED
    assert first.evidence["cache"] == "miss"
    assert first.evidence["result"] == {"answer": 42}
    assert len(synthesizer.calls) == 1
    # The synthesized script was verified through OUR backend before caching.
    assert backend.requests[0].script == "print('v1')"
    assert backend.requests[0].dependencies == ["pandas"]

    second = runner.execute(_action(), idempotency_key="k2")
    assert second.status is ExecutionStatus.SUCCEEDED
    assert second.evidence["cache"] == "hit"
    assert len(synthesizer.calls) == 1  # no synthesis paid on the hit
    assert backend.requests[1].script == "print('v1')"  # the cached script ran


def test_cache_is_node_granular_not_intent_granular():
    """The same node key hits across different parent goals; a changed
    binding or environment is a different key."""
    cache = LocalScriptCache(":memory:")
    synthesizer = CountingSynthesizer()
    backend = StubBackend([_ok()] * 4)
    runner = _runner(backend, cache, synthesizer)

    a = _action("convert report", node_key="convert_csv", bindings={"file": "a.csv"})
    runner.execute(a, idempotency_key="k1")
    # Same node + bindings inside a DIFFERENT workflow goal: still a hit.
    b = _action(
        "monthly close: convert report",
        node_key="convert_csv",
        bindings={"file": "a.csv"},
    )
    hit = runner.execute(b, idempotency_key="k2")
    assert hit.evidence["cache"] == "hit"
    assert len(synthesizer.calls) == 1

    # A different binding is a different artifact.
    c = _action("convert report", node_key="convert_csv", bindings={"file": "b.csv"})
    miss = runner.execute(c, idempotency_key="k3")
    assert miss.evidence["cache"] == "miss"
    assert len(synthesizer.calls) == 2
    # Bindings are rendered into the synthesis goal as exact values.
    assert "'b.csv'" in synthesizer.calls[1]

    # A different environment fingerprint is a different key too.
    other_env = _runner(
        StubBackend([_ok()]), cache, synthesizer, environment_fingerprint="host-2"
    )
    other_env.execute(a, idempotency_key="k4")
    assert len(synthesizer.calls) == 3


def test_environment_drift_triggers_single_node_resynthesis():
    cache = LocalScriptCache(":memory:")
    synthesizer = CountingSynthesizer(script="print('v2')")
    # Pre-cache a script that the world has since broken.
    seed_runner = _runner(
        StubBackend([_ok()]), cache, CountingSynthesizer("print('v1')")
    )
    seed_runner.execute(_action(), idempotency_key="seed")

    backend = StubBackend([_boom, _ok()])  # cached v1 fails, fresh v2 verifies
    runner = _runner(backend, cache, synthesizer)
    outcome = runner.execute(_action(), idempotency_key="k1")
    assert outcome.status is ExecutionStatus.SUCCEEDED
    assert outcome.evidence["cache"] == "resynthesized"
    assert len(synthesizer.calls) == 1  # exactly this node was re-synthesized
    assert backend.requests[0].script == "print('v1')"  # the stale attempt
    assert backend.requests[1].script == "print('v2')"  # the repair

    # The repaired script replaced the stale one for the next run.
    replay = _runner(StubBackend([_ok()]), cache).execute(
        _action(), idempotency_key="k2"
    )
    assert replay.evidence["cache"] == "hit"


def test_failed_verification_never_enters_the_cache():
    cache = LocalScriptCache(":memory:")
    synthesizer = CountingSynthesizer()
    runner = _runner(StubBackend([_boom]), cache, synthesizer)
    outcome = runner.execute(_action(), idempotency_key="k1")
    assert outcome.status is ExecutionStatus.FAILED
    assert "failed verification" in (outcome.error or "")
    assert cache.get(runner.cache_key("convert the June report", {})) is None


def test_no_cache_and_no_synthesizer_fails_loudly_and_bad_action_blocks():
    cache = LocalScriptCache(":memory:")
    runner = _runner(StubBackend([]), cache)
    outcome = runner.execute(_action(), idempotency_key="k1")
    assert outcome.status is ExecutionStatus.FAILED
    assert "no synthesizer" in (outcome.error or "")

    missing_goal = ActionEvent(
        correlation_id="c", adapter="script", operation="run", parameters={}
    )
    blocked = runner.execute(missing_goal, idempotency_key="k2")
    assert blocked.status is ExecutionStatus.BLOCKED


def test_script_nodes_run_inside_a_dag_blueprint(tmp_path):
    """The third body kind: a synthesized node next to replayed actions."""
    cache = LocalScriptCache(tmp_path / "node-cache.db")
    synthesizer = CountingSynthesizer()
    script_runner = _runner(StubBackend([_ok(), _ok()]), cache, synthesizer)

    from test_dag_scheduler import StubExecutor  # deterministic cli double

    cli = StubExecutor({"export"})
    blueprint = Blueprint(
        name="monthly-close",
        actions=[
            ReservedAction(
                action=ActionEvent(
                    correlation_id="c", adapter="stub", operation="export"
                ),
                required_capabilities=frozenset({"export"}),
            ),
            ReservedAction(
                action=_action("summarize the export", node_key="summarize"),
                required_capabilities=frozenset({"run"}),
            ),
        ],
    )
    dag = DagRouteRunner({"stub": cli, "script": script_runner})
    record = dag.execute(
        RoutePlan(chosen=blueprint, alternatives=[], total_cost=0.0),
        idempotency_key="run-1",
        attempt=1,
    )
    assert record.status is ExecutionStatus.SUCCEEDED
    assert cli.order == ["export"]
    assert len(synthesizer.calls) == 1

    # A second workflow reuses the cached node — even from a fresh runner
    # process, because the cache is a file.
    fresh = _runner(StubBackend([_ok()]), LocalScriptCache(tmp_path / "node-cache.db"))
    replay = fresh.execute(
        _action("summarize the export", node_key="summarize"), idempotency_key="run-2"
    )
    assert replay.evidence["cache"] == "hit"


def test_graph_engine_synthesizer_reads_the_winning_script_back():
    class FakeEngineResult:
        def __init__(self, success, cache_key):
            self.success = success
            self.cache_key = cache_key

    class FakeEngine:
        def __init__(self, result):
            self._result = result
            self.goals: list[str] = []

        def run(self, intent, *, session_id=None):
            self.goals.append(intent)
            return self._result

    engine_cache = LocalScriptCache(":memory:")
    engine_cache.store_success(
        "engine-key", script="print('won')", dependencies=[], tier="fast", model="m"
    )

    engine = FakeEngine(FakeEngineResult(True, "engine-key"))
    synthesizer = GraphEngineSynthesizer(engine, engine_cache)
    synthesis = synthesizer.synthesize("do the thing", session_id="s1")
    assert synthesis is not None and synthesis.script == "print('won')"
    assert engine.goals == ["do the thing"]

    # A failed engine run, or a run with nothing cached, synthesizes nothing.
    assert (
        GraphEngineSynthesizer(
            FakeEngine(FakeEngineResult(False, "engine-key")), engine_cache
        ).synthesize("g", session_id="s2")
        is None
    )
    assert (
        GraphEngineSynthesizer(
            FakeEngine(FakeEngineResult(True, "unknown-key")), engine_cache
        ).synthesize("g", session_id="s3")
        is None
    )


def test_render_node_goal_is_deterministic():
    rendered = render_node_goal("convert", {"b": 2, "a": 1})
    assert rendered == render_node_goal("convert", {"a": 1, "b": 2})
    assert "- a = 1" in rendered and "- b = 2" in rendered
    assert render_node_goal("convert", {}) == "convert"
