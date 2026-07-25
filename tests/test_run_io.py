"""B3: the self-contained node — its run io lives in its own drawer.

Exit gate: a verified run lands two files in the node's drawer under the
run's id — the resolved bindings it executed with and the outputs it
emitted, scrubbed by the corpus discipline — the filing is idempotent
(a retry files the same run once), and the interact window answers
"what did you produce last" from the drawer alone, deterministically:
the standing result is a projection over the newest outputs, never a
model's memory of the conversation.
"""

from __future__ import annotations

import json

from test_chat_assistant import _FakeModel
from test_growth_trigger import GOAL, TASK_TURN, _chat, _rig
from test_http_gateway import _req
from test_node_interact import FakeAuthor

from oolu.cache import LocalScriptCache
from oolu.runtime import NodeScriptRunner, StubBackend
from oolu.runtime.backend import make_success


def _real_hand(tmp_path, payload):
    """The growth rig with a REAL script hand: the run executes through
    ``NodeScriptRunner`` so its outcome carries the runner's own
    evidence — result, bindings — exactly as production runs do."""
    backend = StubBackend([make_success(payload)])
    runner = NodeScriptRunner(backend, LocalScriptCache(":memory:"))
    return _rig(tmp_path, script_exec=runner)


def _built_and_ran(app, ident, payload_hint="please "):
    """Build the GOAL node through the real door, then run it once."""
    app._node_function_author = lambda tenant: FakeAuthor()
    built = _chat(app, ident, "build me a node that " + GOAL)
    assert "Built a NEW node" in built.body["reply"]
    (node,) = app._nodeplace.all_nodes()
    model = _FakeModel([TASK_TURN])
    app._tenant_model = lambda tenant: model
    ran = _chat(app, ident, payload_hint + GOAL)
    assert ran.status == 200, ran.body
    assert ran.body["run_id"], ran.body
    return node, ran.body["run_id"], model


def _run_files(app, node_id):
    return {
        (f.folder, f.name): f
        for f in app._files.list(tenant="t1", node_id=node_id)
        if f.folder.startswith("runs/")
    }


def _interact(app, ident, node_id, message):
    return app.handle(
        _req(
            "POST",
            "/v1/chat",
            token=ident.token("user-1", "t1"),
            body={"message": message, "history": [], "node_id": node_id},
        )
    )


# --------------------------------------------------------------------------- #
# The landing: a verified run's io lives in the node's drawer.                  #
# --------------------------------------------------------------------------- #
def test_a_verified_run_lands_its_io_in_the_nodes_drawer(tmp_path):
    app, conn, ident, desk, runner = _real_hand(tmp_path, {"result": "tidy"})
    try:
        node, run_id, _model = _built_and_ran(app, ident)

        files = _run_files(app, node.node_id)
        folder = f"runs/{run_id}"
        assert set(files) == {(folder, "inputs.json"), (folder, "outputs.json")}
        outputs = json.loads(files[(folder, "outputs.json")].content)
        assert outputs["run_id"] == run_id
        assert outputs["verified"] is True
        assert outputs["result"] == {"result": "tidy"}
        assert outputs["at"]
        inputs = json.loads(files[(folder, "inputs.json")].content)
        assert inputs["run_id"] == run_id
        assert inputs["bindings"] == {}  # this run bound nothing — honest

        # Idempotent: re-filing the same terminal run lands nothing new.
        state = next(
            s for s in app._durable.runs.list() if s.run_id == run_id
        )
        before = len(app._files.list(tenant="t1", node_id=node.node_id))
        app._land_run_io(state)
        assert len(app._files.list(tenant="t1", node_id=node.node_id)) == before
    finally:
        conn.close()


def test_the_landed_outputs_are_scrubbed(tmp_path):
    # The function's answer carries a secret shape (assembled at runtime
    # so the tree's own secret scan never trips on this fixture): the
    # drawer copy is scrubbed by the same discipline every stored
    # knowledge passes.
    fake_key = "sk-" + "a" * 24
    leaky = {"result": "the key is " + fake_key}
    app, conn, ident, desk, runner = _real_hand(tmp_path, leaky)
    try:
        node, run_id, _model = _built_and_ran(app, ident)
        files = _run_files(app, node.node_id)
        content = files[(f"runs/{run_id}", "outputs.json")].content
        assert fake_key not in content
        assert "<API_KEY>" in content
    finally:
        conn.close()


def test_the_runners_outcome_carries_the_resolved_bindings(tmp_path):
    # What went in rides the outcome (the seam the landing reads): the
    # exact values the runtime bound, on the success evidence.
    from oolu.skills.models import ActionEvent

    backend = StubBackend([make_success({"ok": True})])
    runner = NodeScriptRunner(backend, LocalScriptCache(":memory:"))
    outcome = runner.execute(
        ActionEvent(
            correlation_id="fn",
            adapter="script",
            operation="run",
            parameters={
                "goal": "normalize",
                "script": (
                    "from _oolu_runtime import emit_result\n"
                    "import json\n"
                    "rows = json.load(open('bindings.json'))\n"
                    "emit_result(str(len(rows)))"
                ),
                "bindings": {"invoice_csv": "a.csv", "limit": 3},
            },
        ),
        idempotency_key="run-1",
    )
    assert outcome.evidence["bindings"] == {"invoice_csv": "a.csv", "limit": 3}


# --------------------------------------------------------------------------- #
# The standing result: the interact window answers from the drawer alone.      #
# --------------------------------------------------------------------------- #
def test_the_interact_window_answers_from_the_drawer_alone(tmp_path):
    app, conn, ident, desk, runner = _real_hand(tmp_path, {"result": "tidy"})
    try:
        # Before any run: a plain answer, not an error — a new node
        # simply has no story yet.
        app._node_function_author = lambda tenant: FakeAuthor()
        built = _chat(app, ident, "build me a node that " + GOAL)
        assert "Built a NEW node" in built.body["reply"]
        (node,) = app._nodeplace.all_nodes()
        empty = _interact(app, ident, node.node_id, "what did you produce last")
        assert empty.status == 200, empty.body
        assert "has not produced a verified result yet" in empty.body["reply"]
        assert empty.body["actions"] == [{"tool": "last_result"}]

        # Run once, then ask again: the answer is the drawer's newest
        # verified outputs, named with the run — and the chat model was
        # never consulted for either ask (source stays "tool").
        model = _FakeModel([TASK_TURN])
        app._tenant_model = lambda tenant: model
        ran = _chat(app, ident, "please " + GOAL)
        assert ran.status == 200, ran.body
        run_id = ran.body["run_id"]
        calls_after_run = len(model.calls)

        asked = _interact(app, ident, node.node_id, "What did you produce last?")
        assert asked.status == 200, asked.body
        assert "tidy" in asked.body["reply"]
        assert run_id[:8] in asked.body["reply"]
        assert asked.body["source"] == "tool"
        assert asked.body["actions"] == [{"tool": "last_result"}]
        assert len(model.calls) == calls_after_run
    finally:
        conn.close()
