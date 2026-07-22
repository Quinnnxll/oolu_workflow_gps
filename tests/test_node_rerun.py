"""Issue 15: one node per goal, its OWN function on re-run, self-repair,
and a declared interface.

Exit gate: rebuilding the same goal reuses the node (no twin, no second
model spend — executions accumulate in ONE log); a run whose goal the
user built a node for executes that node's stored function through the
script hand instead of re-planning onto some other executor (the URL
fetch of old); a failing function is EDITED by the model — verified by
execution, bounded, cached as the node's healed code; and the authored
node declares what it consumes and produces, in the slot vocabulary the
route assembler chains on.
"""

from __future__ import annotations

from types import SimpleNamespace

from test_http_gateway import (
    _autonomous,
)
from test_node_interact import FakeAuthor, _chat, _rig

from oolu.cache import LocalScriptCache
from oolu.chat import parse_node_io
from oolu.models import ExecutionResult, Phase
from oolu.orchestrator import (
    ActionExecutorRouteRunner,
    BoundedRetryRecovery,
    CapabilityGrounder,
    CollectingFeedbackSink,
    LeastCostRouteOptimizer,
    RiskBasedHumanControl,
    StaticIntaker,
    StatusOutcomeMonitor,
    WorkflowOrchestrator,
)
from oolu.orchestrator.state import PauseKind, ResumeInput, TaskContract
from oolu.runtime import NodeScriptRunner, StubBackend
from oolu.skills.models import ExecutionOutcome, ExecutionStatus

GOAL = "normalize invoice csv files"
IO_ANSWER = (
    "1. Read the rows.\n"
    'IO: {"inputs": [{"name": "invoice_csv", "type": "path"}],'
    ' "outputs": [{"name": "normalized_csv", "type": "path"}]}\n'
    "```python\nfrom _oolu_runtime import emit_result\nemit_result(''.join(['t', 'i', 'd', 'y']))\n```"
)


def _consented(app, conn):
    from oolu.settings_node import SettingsNode, SettingsStore

    settings = SettingsNode(SettingsStore(conn))
    settings.set("t1", "account.autobuild_consent", True)
    app._settings = settings


# --------------------------------------------------------------------------- #
# One node per goal.                                                           #
# --------------------------------------------------------------------------- #
def test_rebuilding_the_same_goal_reuses_the_node(tmp_path):
    app, conn, ident, registry, desk, node_id, pending_id = _rig(tmp_path)
    try:
        _consented(app, conn)
        author = FakeAuthor()
        app._node_function_author = lambda tenant: author

        first = _chat(app, ident, node_id, f"build {GOAL}")
        assert "Built" in first.body["reply"], first.body

        again = _chat(app, ident, node_id, f"build {GOAL}")
        assert "already exists" in again.body["reply"]
        assert "No copy was made" in again.body["reply"]
        # No second node, no second model spend.
        assert len(author.calls) == 1
        mine = desk.overview(principal="noder-export", tenant="t1")
        twins = [e for e in mine if e.title == "Normalize Invoice Csv Files"]
        assert len(twins) == 1
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The declared interface.                                                      #
# --------------------------------------------------------------------------- #
def test_the_io_declaration_parses_and_degrades_honestly():
    io = parse_node_io(IO_ANSWER)
    assert io == {
        "inputs": [{"name": "invoice_csv", "type": "path"}],
        "outputs": [{"name": "normalized_csv", "type": "path"}],
    }
    # No declaration: no inputs, one string result — never a crash.
    assert parse_node_io("```python\npass\n```") == {
        "inputs": [],
        "outputs": [{"name": "result", "type": "str"}],
    }
    # Junk types collapse to str; nameless entries vanish.
    weird = parse_node_io('IO: {"inputs": [{"name": "x", "type": "blob"}, {}]}')
    assert weird["inputs"] == [{"name": "x", "type": "str"}]


def test_the_built_node_carries_its_interface_on_the_listing(tmp_path):
    app, conn, ident, registry, desk, node_id, pending_id = _rig(tmp_path)
    try:
        _consented(app, conn)
        app._node_function_author = lambda tenant: FakeAuthor(IO_ANSWER)

        built = _chat(app, ident, node_id, f"build {GOAL}")
        assert "consumes invoice_csv:path" in built.body["reply"]
        assert "produces normalized_csv:path" in built.body["reply"]

        mine = desk.overview(principal="noder-export", tenant="t1")
        new = next(e for e in mine if e.title == "Normalize Invoice Csv Files")
        [version] = registry.list_versions(new.node_id)
        listing = registry.listing_for_version(version.version_id)
        assert [(s.name, s.value_type) for s in listing.consumes] == [
            ("invoice_csv", "path")
        ]
        assert [(s.name, s.value_type) for s in listing.produces] == [
            ("normalized_csv", "path")
        ]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The re-run executes the node's OWN function.                                 #
# --------------------------------------------------------------------------- #
def test_the_gateway_resolves_a_built_goal_to_its_function(tmp_path):
    app, conn, ident, registry, desk, node_id, pending_id = _rig(tmp_path)
    try:
        _consented(app, conn)
        app._node_function_author = lambda tenant: FakeAuthor()
        _chat(app, ident, node_id, f"build {GOAL}")

        session = SimpleNamespace(tenant_id="t1", principal_id="noder-export")
        function = app._resolve_node_function(session, GOAL)
        assert function is not None
        assert function["skill_id"].startswith("fn-")
        assert "emit_result" in function["script"]
        # Same words, different spacing/case: still the same goal.
        assert app._resolve_node_function(
            session, "  Normalize   INVOICE csv files "
        ) == function
        # A goal nobody built stays a plain plan.
        assert app._resolve_node_function(session, "something else") is None
    finally:
        conn.close()


class _ScriptExec:
    """Records every script action it executes; always succeeds."""

    name = "script"

    def __init__(self):
        self.actions = []

    def capabilities(self):
        return frozenset({"run"})

    def execute(self, action, *, idempotency_key):
        self.actions.append(action)
        return ExecutionOutcome(
            idempotency_key=idempotency_key,
            skill_id=action.correlation_id,
            status=ExecutionStatus.SUCCEEDED,
        )

    def cancel(self, idempotency_key):
        return None


def test_a_run_with_a_node_function_executes_that_function(tmp_path):
    from oolu.durable import DurableConnection, DurableWorkflowService

    brief, blueprint, executor, grounding = _autonomous()
    script_exec = _ScriptExec()

    def build(events):
        return WorkflowOrchestrator(
            intaker=StaticIntaker(brief),
            grounder=CapabilityGrounder(grounding),
            optimizer=LeastCostRouteOptimizer([blueprint]),
            human_control=RiskBasedHumanControl(),
            executor=ActionExecutorRouteRunner(
                {"test": executor, "script": script_exec}
            ),
            monitor=StatusOutcomeMonitor(),
            recovery=BoundedRetryRecovery(),
            feedback=CollectingFeedbackSink(),
            events=events,
        )

    conn = DurableConnection(tmp_path / "durable.db")
    durable = DurableWorkflowService(conn, build)
    state = durable.submit(
        TaskContract(
            intent=GOAL,
            submitted_by="noder-export",
            metadata={
                "tenant_id": "t1",
                "node_function": {
                    "node_id": "n1",
                    "skill_id": "fn-abc",
                    "title": "Normalize Invoice Csv Files",
                    "goal": GOAL,
                    "script": "from _oolu_runtime import emit_result\nemit_result(''.join(['t', 'i', 'd', 'y']))",
                    "node_key": "node:fn-abc",
                },
            },
        )
    )
    # Stored model-written code still re-earns the human's confirmation.
    if state.pause is not None and state.pause.kind is PauseKind.CONFIRMATION:
        state = durable.resume(
            state.run_id,
            ResumeInput(kind=PauseKind.CONFIRMATION, confirmed=True),
        )
    assert state.route is not None
    assert state.route.chosen.origin == "node_function"
    # The node's OWN code ran through the script hand — not some other
    # executor, and certainly not a URL fetch.
    [action] = script_exec.actions
    assert action.adapter == "script"
    assert action.parameters["node_key"] == "node:fn-abc"
    assert "emit_result" in action.parameters["script"]
    assert executor.calls == 0  # the generic hand never fired
    conn.close()


# --------------------------------------------------------------------------- #
# Self-repair: the model edits the failing function.                           #
# --------------------------------------------------------------------------- #
def _ok(payload=None):
    return lambda request: ExecutionResult(
        phase=Phase.EXECUTE,
        exit_code=0,
        stdout="",
        contract_ok=True,
        contract_payload=payload or {"answer": "tidy"},
    )


def _boom(request):
    return ExecutionResult(
        phase=Phase.EXECUTE,
        exit_code=1,
        stderr="TypeError: broken",
        contract_ok=False,
    )


class _RepairingSynthesizer:
    """Scripted repairs, in order; records what it was asked to fix."""

    def __init__(self, fixes):
        self._fixes = list(fixes)
        self.repair_calls = []

    def synthesize(self, goal, *, session_id):
        raise AssertionError("repair must not fall through to re-synthesis")

    def repair(self, goal, script, error):
        self.repair_calls.append({"script": script, "error": error})
        return self._fixes.pop(0) if self._fixes else None

    def _action(self, script):
        from oolu.skills.models import ActionEvent

        return ActionEvent(
            correlation_id="function",
            adapter="script",
            operation="run",
            parameters={
                "goal": GOAL,
                "script": script,
                "node_key": "node:fn-abc",
            },
        )


def test_a_failing_function_is_edited_verified_and_cached(tmp_path):
    fixed = "from _oolu_runtime import emit_result\nemit_result('healed')"
    synthesizer = _RepairingSynthesizer([fixed])
    backend = StubBackend([_boom, _ok()])
    runner = NodeScriptRunner(
        backend,
        LocalScriptCache(tmp_path / "scripts.db"),
        synthesizer=synthesizer,
    )
    outcome = runner.execute(
        synthesizer._action("broken script"), idempotency_key="run-1"
    )
    assert outcome.status is ExecutionStatus.SUCCEEDED
    assert outcome.evidence["cache"] == "repaired"
    assert outcome.evidence["repair_rounds"] == 1
    # The model saw the exact failure it had to close.
    [ask] = synthesizer.repair_calls
    assert ask["script"] == "broken script"
    assert "TypeError: broken" in ask["error"]
    # The healed code IS the node's function now: the next run replays it
    # from the cache without any model.
    backend2 = StubBackend([_ok()])
    runner2 = NodeScriptRunner(
        backend2,
        LocalScriptCache(tmp_path / "scripts.db"),
        synthesizer=None,
    )
    replay = runner2.execute(
        synthesizer._action("broken script"), idempotency_key="run-2"
    )
    assert replay.status is ExecutionStatus.SUCCEEDED
    assert replay.evidence["cache"] == "hit"
    assert backend2.requests[0].script == fixed


def test_repair_is_bounded_and_fails_honestly(tmp_path):
    synthesizer = _RepairingSynthesizer(["still broken", "even more broken"])
    backend = StubBackend([_boom, _boom, _boom])
    runner = NodeScriptRunner(
        backend,
        LocalScriptCache(tmp_path / "scripts.db"),
        synthesizer=synthesizer,
    )

    class _NoResynthesis(_RepairingSynthesizer):
        def synthesize(self, goal, *, session_id):
            return None  # the bounded edit loop already said no

    runner._synthesizer = _NoResynthesis(["still broken", "even more broken"])
    outcome = runner.execute(
        synthesizer._action("broken script"), idempotency_key="run-1"
    )
    assert outcome.status is ExecutionStatus.FAILED
    assert len(runner._synthesizer.repair_calls) == 2  # two rounds, no more
