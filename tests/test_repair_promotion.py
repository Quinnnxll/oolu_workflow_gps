"""The in-run repair loop, seated: healed code promotes into the drawer.

The circle the model-seats work left open. Mid-run, a node whose stored
function fails is EDITED by the model, verified by execution, and cached
— but the run touches no files (it must not mutate mid-flight). This
closes it: after a COMPLETED run that healed its own function, the
gateway writes the healed code home to ``src/main.py`` through the
``node.repair`` seat — scope-checked, audited, exactly once per run, and
only for the node's OWN function. From the next run on, the drawer copy
is the healed code, and its cache entry is already warm.
"""

from __future__ import annotations

from test_growth_trigger import _built_first_node, _chat, _rig, _speak_work

from oolu.models import ExecutionResult, Phase
from oolu.runtime import NodeScriptRunner, StubBackend

GOAL = "normalize invoice csv files"


# --------------------------------------------------------------------------- #
# A script hand that fails the stored function once, heals it, then holds.     #
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
        stderr="TypeError: broken by environment drift",
        contract_ok=False,
    )


HEALED = "from _oolu_runtime import emit_result\nemit_result('healed by repair')\n"


class _OneShotRepair:
    """Heals the first failure with HEALED, then never again."""

    def __init__(self):
        self.repair_calls: list[dict] = []

    def synthesize(self, goal, *, session_id):  # pragma: no cover - unused here
        return None

    def repair(self, goal, script, error):
        self.repair_calls.append({"script": script, "error": error})
        return HEALED


def _healing_runner(tmp_path, results):
    from oolu.cache import LocalScriptCache

    return NodeScriptRunner(
        StubBackend(results),
        LocalScriptCache(tmp_path / "scripts.db"),
        synthesizer=_OneShotRepair(),
    )


def _drawer_main(app, node_id):
    files = app._files.list(tenant="t1", node_id=node_id)
    mains = [f for f in files if f.folder == "src" and f.name == "main.py"]
    return mains[0] if mains else None


def _seat_events(app, purpose):
    return [
        r
        for r in app._durable.audit.records()
        if r.event_type == "model.seat" and r.payload.get("purpose") == purpose
    ]


# --------------------------------------------------------------------------- #
# The promotion.                                                               #
# --------------------------------------------------------------------------- #
def test_a_healed_function_is_written_home_and_audited(tmp_path):
    # The stored function boots broken; the run heals it (boom -> ok), and
    # the healed code lands in the drawer after the run completes.
    runner = _healing_runner(tmp_path, [_boom, _ok()])
    app, conn, ident, desk, script_exec = _rig(tmp_path, script_exec=runner)
    try:
        _speak_work(app, ['{"say": "On it!", "task": "' + GOAL + '"}'])
        _built_first_node(app, ident)  # builds the node, first run heals it
        node_id = desk.overview(principal="user-1", tenant="t1")[0].node_id

        main = _drawer_main(app, node_id)
        assert main is not None, "src/main.py should exist after the build"
        # The build wrote the ORIGINAL function; the repair promotion then
        # overwrote it with the healed code — one file, healed content.
        assert main.content == HEALED
        assert main.media_type == "text/x-python"

        # The promotion is on the audit log as a node.repair seat act.
        repairs = _seat_events(app, "node.repair")
        assert len(repairs) == 1
        assert repairs[0].payload["node_id"] == node_id
        assert repairs[0].payload["written"] == ["src/main.py"]
        # The repair model was actually consulted with the real failure.
        assert runner._synthesizer.repair_calls
        assert "TypeError" in runner._synthesizer.repair_calls[0]["error"]
    finally:
        conn.close()


def test_the_promoted_code_is_what_the_next_run_executes(tmp_path):
    # boom+ok heals the build run; the NEXT run must execute the healed
    # drawer copy straight from cache — no second failure, no repair.
    runner = _healing_runner(tmp_path, [_boom, _ok(), _ok({"answer": "again"})])
    app, conn, ident, desk, script_exec = _rig(tmp_path, script_exec=runner)
    try:
        _speak_work(
            app,
            [
                '{"say": "On it!", "task": "' + GOAL + '"}',
                '{"say": "Again!", "task": "' + GOAL + '"}',
            ],
        )
        _built_first_node(app, ident)
        node_id = desk.overview(principal="user-1", tenant="t1")[0].node_id
        assert _drawer_main(app, node_id).content == HEALED

        before = len(runner._synthesizer.repair_calls)
        again = _chat(app, ident, GOAL)
        assert again.status == 200, again.body
        # The second run executed the HEALED code and needed no repair.
        assert runner._backend.requests[-1].script == HEALED
        assert len(runner._synthesizer.repair_calls) == before
        # Still exactly one promotion event — the healthy run promotes
        # nothing (there was nothing to heal).
        assert len(_seat_events(app, "node.repair")) == 1
    finally:
        conn.close()


def test_a_healthy_run_promotes_nothing(tmp_path):
    # The stored function works first try: no repair, no node.repair event,
    # and the drawer keeps the originally-authored function untouched.
    runner = _healing_runner(tmp_path, [_ok()])
    app, conn, ident, desk, script_exec = _rig(tmp_path, script_exec=runner)
    try:
        _speak_work(app, ['{"say": "On it!", "task": "' + GOAL + '"}'])
        _built_first_node(app, ident)
        node_id = desk.overview(principal="user-1", tenant="t1")[0].node_id

        main = _drawer_main(app, node_id)
        assert main is not None
        assert main.content != HEALED  # the authored function, not a heal
        assert _seat_events(app, "node.repair") == []
        assert runner._synthesizer.repair_calls == []
    finally:
        conn.close()
