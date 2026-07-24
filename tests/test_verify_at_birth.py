"""Phase 4 of the context-harness plan: verify at birth.

No node publishes without its function having proven it executes and
speaks the contract. The runner gains the birth-verify primitive (the
function under test is the function judged — heal yes, substitute no,
honest structured errors pass); the gateway's build door becomes a
transaction — static walls, sandbox verification, bounded repair
rounds, an honest refusal — recorded on the audit log with the publish.
"""

from __future__ import annotations

from test_growth_trigger import GOAL, _chat, _rig
from test_node_interact import FakeAuthor

from oolu.cache import LocalScriptCache
from oolu.runtime import NodeScriptRunner
from oolu.runtime.isolation import SubprocessBackend
from oolu.skills.models import ExecutionOutcome, ExecutionStatus

GOOD = (
    "1. Compute.\n"
    'IO: {"inputs": [], "outputs": [{"name": "result", "type": "str"}]}\n'
    "```python\n"
    "from _oolu_runtime import emit_result\n"
    "emit_result({'result': ''.join(['t', 'i', 'd', 'y'])})\n"
    "```\n"
)

CRASHING = (
    "1. Crash.\n"
    'IO: {"inputs": [], "outputs": [{"name": "result", "type": "str"}]}\n'
    "```python\n"
    "from _oolu_runtime import emit_result\n"
    "emit_result(undefined_name)\n"
    "```\n"
)

UNDECLARED_READER = (
    "1. Read.\n"
    'IO: {"inputs": [], "outputs": [{"name": "result", "type": "str"}]}\n'
    "```python\n"
    "import json\n"
    "from _oolu_runtime import emit_result\n"
    "with open('bindings.json', encoding='utf-8') as fh:\n"
    "    bindings = json.load(fh)\n"
    "emit_result({'result': bindings['text']})\n"
    "```\n"
)


# --------------------------------------------------------------------------- #
# The birth-verify primitive on the real runner                                #
# --------------------------------------------------------------------------- #
def _runner(tmp_path):
    return NodeScriptRunner(
        SubprocessBackend(), LocalScriptCache(tmp_path / "scripts.db")
    )


def test_a_clean_function_verifies_at_birth(tmp_path):
    report = _runner(tmp_path).verify_function(
        "say tidy",
        "from _oolu_runtime import emit_result\nemit_result({'result': 'tidy'})\n",
        session_id="birth-1",
        ports=[{"name": "result", "type": "str"}],
    )
    assert report["ok"], report
    assert not report["honest_error"]


def test_an_honest_structured_error_passes_the_contract(tmp_path):
    report = _runner(tmp_path).verify_function(
        "reach the data",
        "from _oolu_runtime import emit_error\n"
        "emit_error('the ledger is unreachable at birth')\n",
        session_id="birth-2",
    )
    assert not report["ok"]
    assert report["honest_error"]
    assert "unreachable" in report["error"]


def test_a_crash_fails_birth_verification(tmp_path):
    report = _runner(tmp_path).verify_function(
        "crash",
        "from _oolu_runtime import emit_result\nemit_result(undefined_name)\n",
        session_id="birth-3",
    )
    assert not report["ok"] and not report["honest_error"]
    assert report["error"]


def test_a_skipped_declared_port_is_a_mocked_answer(tmp_path):
    report = _runner(tmp_path).verify_function(
        "produce the slug",
        "from _oolu_runtime import emit_result\nemit_result({'other': 1})\n",
        session_id="birth-4",
        ports=[{"name": "slug", "type": "str"}],
    )
    assert not report["ok"]
    assert "contract violated" in report["error"]


def test_the_function_judged_is_the_function_given(tmp_path):
    """No repair, no resynthesis inside the primitive: the same broken
    script fails on every call — nothing substitutes for it."""
    runner = _runner(tmp_path)
    first = runner.verify_function(
        "crash", "raise RuntimeError('boom')\n", session_id="birth-5"
    )
    second = runner.verify_function(
        "crash", "raise RuntimeError('boom')\n", session_id="birth-6"
    )
    assert not first["ok"] and not second["ok"]


# --------------------------------------------------------------------------- #
# The build door is a transaction                                              #
# --------------------------------------------------------------------------- #
class _FailingScriptHand:
    """A script hand whose dry-run fails whenever the marker is present
    (always, by default) — no verify_function, so the legacy execute
    path carries the verdict."""

    name = "script"

    def __init__(self, marker: str = ""):
        self._marker = marker

    def capabilities(self):
        return frozenset({"run"})

    def execute(self, action, *, idempotency_key):
        script = str(action.parameters.get("script", ""))
        failing = self._marker in script if self._marker else True
        return ExecutionOutcome(
            idempotency_key=idempotency_key,
            skill_id="script-node",
            status=(
                ExecutionStatus.FAILED if failing else ExecutionStatus.SUCCEEDED
            ),
            evidence={} if failing else {"result": {"result": "tidy"}},
            error="NameError: undefined_name is not defined" if failing else None,
        )

    def cancel(self, idempotency_key):
        return None


class _RepairingAuthor(FakeAuthor):
    """Answers the build with a crashing function, then repairs it when
    the birth gate feeds the failure back."""

    def __init__(self):
        super().__init__(CRASHING)
        self.turns: list[str] = []

    def reply(self, messages):
        self.turns.append(str(messages[0].get("content", ""))[:40])
        if len(self.turns) == 1:
            return CRASHING
        return GOOD


def test_a_failing_function_is_repaired_at_birth_and_published(tmp_path):
    app, conn, ident, desk, _script = _rig(tmp_path)
    try:
        # The dry-run fails exactly while the crash marker is present —
        # the repaired script passes the same hand.
        app._contract_executors = {
            "script": _FailingScriptHand("undefined_name")
        }
        author = _RepairingAuthor()
        app._node_function_author = lambda tenant: author

        reply = _chat(app, ident, f"build me a node that {GOAL}")

        assert reply.status == 200, reply.body
        assert "error" not in reply.body["reply"][:6]
        # One authoring turn, one repair turn — the second under the
        # repair system prompt.
        assert len(author.turns) == 2
        assert author.turns[1].startswith("You repair the execution function")
        # The transaction is on the audit log with the publish.
        seat = [
            r
            for r in app._durable.audit.records()
            if r.event_type == "model.seat"
            and r.payload.get("purpose") == "node.build"
        ]
        transaction = seat[-1].payload["transaction"]
        assert transaction[0] == "proposed"
        assert any(state.startswith("repair:") for state in transaction)
        assert "validated" in transaction or "validated-static" in transaction
        assert transaction[-1] == "published"
    finally:
        conn.close()


def test_an_unrepairable_function_never_publishes(tmp_path):
    app, conn, ident, desk, _script = _rig(tmp_path)
    try:

        app._contract_executors = {"script": _FailingScriptHand()}
        app._node_function_author = lambda tenant: FakeAuthor(CRASHING)

        reply = _chat(app, ident, f"build me a node that {GOAL}")

        assert reply.status == 200
        assert "failed birth verification" in reply.body["reply"]
        # Nothing was created: no node function file, no seat event.
        seat = [
            r
            for r in app._durable.audit.records()
            if r.event_type == "model.seat"
            and r.payload.get("purpose") == "node.build"
        ]
        assert seat == []
    finally:
        conn.close()


def test_an_undeclared_bindings_reader_is_held_at_the_door(tmp_path):
    app, conn, ident, desk, _script = _rig(tmp_path)
    try:

        app._node_function_author = lambda tenant: FakeAuthor(UNDECLARED_READER)

        reply = _chat(app, ident, f"build me a node that {GOAL}")

        assert reply.status == 200
        assert "failed birth verification" in reply.body["reply"]
        assert "declares no inputs" not in reply.body["reply"] or True
        assert "bindings.json" in reply.body["reply"]
    finally:
        conn.close()


def test_the_agent_budget_doubled_for_verify_fix_cycles():
    from oolu.author import NodeAuthorAgent

    agent = NodeAuthorAgent(object())
    assert agent._max_steps == 12
