"""The node author agent: the library in hand, the finish schema-checked.

Exit gate: a tool-calling model authors a node's function by WORKING —
reading the desk's contracts and a named node's recent outputs — and can
only deliver through ``finish_node`` (emit_result enforced, verification
a hard gate when wired); every refusal is words the builder passes on.
A model without ``consult`` keeps the one-shot path untouched, and the
gateway seats the agent with the real desk behind the hands.
"""

from __future__ import annotations

from test_growth_trigger import GOAL, _chat, _rig
from test_node_interact import FakeAuthor

from oolu.author import NodeAuthorAgent
from oolu.providers.tools import ToolCall, ToolReply
from oolu.seats import SEATS, DeskFiles

SCRIPT = "from _oolu_runtime import emit_result\nemit_result('tidy')"


class ConsultModel:
    """A scripted tool-calling brain: replies in order, records what it
    was shown and which hands it held."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.transcripts: list[list[dict]] = []
        self.tool_sets: list[list[str]] = []

    def consult(self, messages, *, tools, tool_choice="auto"):
        self.transcripts.append([dict(m) for m in messages])
        self.tool_sets.append(sorted(spec.name for spec in tools))
        return self._replies.pop(0)


def _call(name, arguments, id="c1"):
    return ToolReply(
        text="", tool_calls=(ToolCall(id=id, name=name, arguments=arguments),)
    )


def _finish(script=SCRIPT, **extra):
    return _call("finish_node", {"script": script, **extra}, id="fin")


# --------------------------------------------------------------------------- #
# The working loop.                                                            #
# --------------------------------------------------------------------------- #
def test_the_author_reads_the_library_then_finishes():
    model = ConsultModel([
        ToolReply(text="", tool_calls=(
            ToolCall(id="a", name="list_nodes", arguments={}),
            ToolCall(id="b", name="read_node_output",
                     arguments={"node_id": "n-up"}),
        )),
        _finish(inputs=[{"name": "invoice_rows"}],
                outputs=[{"name": "tidy_rows", "type": "str"}]),
    ])
    agent = NodeAuthorAgent(
        model,
        catalog=lambda: [{
            "node_id": "n-up", "title": "Fetch invoices", "goal": "fetch",
            "consumes": [], "produces": [{"name": "invoice_rows",
                                          "type": "str"}],
        }],
        outputs=lambda node_id: [{
            "run_id": "r1", "status": "succeeded",
            "outputs": [{"rows": 3, "for": node_id}],
        }],
    )

    authored = agent.author("normalize the fetched invoices")

    assert authored.script == SCRIPT
    # The interface arrived as validated arguments — type defaulted, never
    # regexed out of prose.
    assert authored.io == {
        "inputs": [{"name": "invoice_rows", "type": "str"}],
        "outputs": [{"name": "tidy_rows", "type": "str"}],
    }
    assert authored.refusal == ""
    assert authored.consultations == 2
    # The second consultation SAW what the hands brought back.
    second = model.transcripts[1]
    tool_answers = [m["content"] for m in second if m.get("role") == "tool"]
    assert any("invoice_rows" in c for c in tool_answers)
    assert any('"rows": 3' in c for c in tool_answers)
    # Every registered hand was on the table.
    assert model.tool_sets[0] == [
        "decline", "finish_node", "list_nodes", "read_node_output",
    ]


def test_a_script_without_emit_result_is_refused_then_corrected():
    model = ConsultModel([
        _finish(script="print('no contract')"),
        _finish(),
    ])
    agent = NodeAuthorAgent(model)

    authored = agent.author(GOAL)

    assert authored.script == SCRIPT
    # The refusal traveled back as a correctable answer, not an exception.
    refusals = [
        m["content"]
        for turn in model.transcripts
        for m in turn
        if m.get("role") == "tool"
    ]
    assert any("never calls emit_result" in c for c in refusals)


def test_verification_is_a_hard_gate_on_the_finish():
    reports = [{"ok": False, "error": "NameError: rows"}, {"ok": True}]
    verified: list[str] = []

    def verify(script):
        verified.append(script)
        return reports.pop(0)

    model = ConsultModel([_finish(), _finish()])
    agent = NodeAuthorAgent(model, verify=verify)

    authored = agent.author(GOAL)

    assert authored.script == SCRIPT
    assert verified == [SCRIPT, SCRIPT]
    refusals = [
        m["content"]
        for turn in model.transcripts
        for m in turn
        if m.get("role") == "tool"
    ]
    assert any("verification failed: NameError: rows" in c for c in refusals)


def test_decline_is_a_refusal_in_words():
    model = ConsultModel([
        _call("decline", {"reason": "that's a greeting, not work"}),
    ])
    authored = NodeAuthorAgent(model).author("hello there")
    assert authored.script is None
    assert authored.refusal == "that's a greeting, not work"


def test_the_one_shot_protocol_still_lands_when_it_leaks_through():
    # A model that ignores the hands and answers in the old prose shape
    # loses nothing: the same gates as author_node_function apply.
    prose = (
        "1. Tidy the rows.\n"
        'IO: {"inputs": [{"name": "invoice_rows", "type": "str"}], '
        '"outputs": [{"name": "result", "type": "str"}]}\n'
        f"```python\n{SCRIPT}\n```"
    )
    authored = NodeAuthorAgent(ConsultModel([ToolReply(text=prose)])).author(GOAL)
    assert authored.script == SCRIPT
    assert authored.io["inputs"] == [{"name": "invoice_rows", "type": "str"}]


def test_no_task_text_refuses_as_conversation():
    authored = NodeAuthorAgent(
        ConsultModel([ToolReply(text="NO_TASK")])
    ).author("how are you?")
    assert authored.script is None
    assert "conversation" in authored.refusal


def test_running_out_of_steps_is_an_honest_refusal():
    browsing = _call("list_nodes", {})
    model = ConsultModel([browsing, browsing])
    agent = NodeAuthorAgent(model, catalog=lambda: [], max_steps=2)
    authored = agent.author(GOAL)
    assert authored.script is None
    assert "ran out of authoring steps" in authored.refusal
    assert authored.consultations == 2


def test_a_dead_model_builds_nothing():
    class Dead:
        def consult(self, messages, *, tools, tool_choice="auto"):
            raise RuntimeError("socket closed")

    authored = NodeAuthorAgent(Dead()).author(GOAL)
    assert authored.script is None
    assert "could not be reached" in authored.refusal


# --------------------------------------------------------------------------- #
# The gateway seats the agent with the real desk behind the hands.             #
# --------------------------------------------------------------------------- #
def test_the_gateway_seats_a_tool_calling_author_with_the_desk_in_hand(tmp_path):
    app, conn, ident, desk, script_exec = _rig(tmp_path)
    try:
        model = ConsultModel([
            _call("list_nodes", {}),
            _finish(outputs=[{"name": "result", "type": "str"}]),
        ])
        app._node_function_author = lambda tenant: model

        response = _chat(app, ident, "build me a node that " + GOAL)

        assert response.status == 200, response.body
        assert "Built a NEW node" in response.body["reply"]
        # The agent held the desk-backed hands, not just the terminals.
        assert "list_nodes" in model.tool_sets[0]
        assert "read_node_output" in model.tool_sets[0]
        # A real node landed, and its drawer holds the agent's script.
        nodes = app._nodeplace.list_own_nodes(
            noder_principal="user-1", tenant_id="t1"
        )
        assert len(nodes) == 1
        drawer = DeskFiles(
            app._files,
            tenant="t1",
            node_id=nodes[0].node_id,
            seat=SEATS["node.build"],
            consented=True,
        )
        assert drawer.read("src/main.py") == SCRIPT
    finally:
        conn.close()


def test_a_reply_only_author_keeps_the_one_shot_path(tmp_path):
    app, conn, ident, desk, script_exec = _rig(tmp_path)
    try:
        author = FakeAuthor()
        app._node_function_author = lambda tenant: author

        response = _chat(app, ident, "build me a node that " + GOAL)

        assert response.status == 200, response.body
        assert "Built a NEW node" in response.body["reply"]
        # The one-shot author was consulted exactly as before.
        assert len(author.calls) == 1
    finally:
        conn.close()
