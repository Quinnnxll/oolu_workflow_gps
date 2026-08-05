"""The agreement wall (V2, 7a): contract and code proven to agree at birth.

Exit gate (node-vitality-plan, phase V2): the reported repro — an
interface that promises ``source_file`` while the code reads settings —
refuses at birth NAMING the unused input; birth verify stages typed
sample bindings so an input-reading function passes against its real
contract and an honest error against those samples FAILS the gate (only
the web-grant gap, which birth cannot stage, still passes honestly);
the agent's finish gate verifies with the declared contract; and the
receipt names its residue instead of reading like a clean birth.
"""

from __future__ import annotations

import json

from test_growth_trigger import GOAL, _chat, _rig
from test_node_interact import FakeAuthor
from test_verify_at_birth import _FailingScriptHand, _RepairingAuthor, _runner

from oolu.gateway import GatewayApp

# The reported repro: the interface promises `source_file`; the code
# reads OTHER things from bindings.json and never touches it.
PROMISES_UNREAD = (
    "1. Read settings.\n"
    'IO: {"inputs": [{"name": "source_file", "type": "path", '
    '"label": "Which file?", "example": "in.txt"}], '
    '"outputs": [{"name": "result", "type": "str"}]}\n'
    "```python\n"
    "import json\n"
    "from _oolu_runtime import emit_result\n"
    "with open('bindings.json', encoding='utf-8') as fh:\n"
    "    settings = json.load(fh).get('settings', {})\n"
    "emit_result({'result': str(settings)})\n"
    "```\n"
)

# Promises an input and never opens bindings.json at all.
NEVER_READS = (
    "1. Compute.\n"
    'IO: {"inputs": [{"name": "text", "type": "str", '
    '"label": "What should it say?"}], '
    '"outputs": [{"name": "result", "type": "str"}]}\n'
    "```python\n"
    "from _oolu_runtime import emit_result\n"
    "emit_result({'result': ''.join(['f', 'i', 'x'])})\n"
    "```\n"
)

# Reads its declared input — the function typed samples let pass.
READS_ITS_INPUT = (
    "1. Read the text and answer.\n"
    'IO: {"inputs": [{"name": "text", "type": "str", '
    '"label": "What should it process?", "example": "hello"}], '
    '"outputs": [{"name": "result", "type": "str"}]}\n'
    "```python\n"
    "import json\n"
    "from _oolu_runtime import emit_result\n"
    "with open('bindings.json', encoding='utf-8') as fh:\n"
    "    text = json.load(fh)['text']\n"
    "emit_result({'result': text.upper()})\n"
    "```\n"
)

# Reads its input, then reports an error anyway — against REAL staged
# samples that is a function proving it cannot consume its contract.
HONEST_AGAINST_SAMPLES = (
    "1. Try, then give up.\n"
    'IO: {"inputs": [{"name": "text", "type": "str", '
    '"label": "What should it process?"}], '
    '"outputs": [{"name": "result", "type": "str"}]}\n'
    "```python\n"
    "import json\n"
    "from _oolu_runtime import emit_error\n"
    "with open('bindings.json', encoding='utf-8') as fh:\n"
    "    text = json.load(fh)['text']\n"
    "emit_error('cannot process ' + text)\n"
    "```\n"
)

# Honestly names the ONE gap birth cannot stage: the web.
WEB_HONEST = (
    "1. Fetch the page.\n"
    'IO: {"inputs": [], "outputs": [{"name": "result", "type": "str"}]}\n'
    "```python\n"
    "from _oolu_runtime import emit_error, emit_result, http_request\n"
    "try:\n"
    "    page = http_request('https://api.example.com/v1/things')\n"
    "except Exception as exc:\n"
    "    emit_error(str(exc))\n"
    "else:\n"
    "    emit_result({'result': str(page.get('status'))})\n"
    "```\n"
)


# --------------------------------------------------------------------------- #
# The forward static wall: promised means consumed.                            #
# --------------------------------------------------------------------------- #
def test_the_reported_repro_refuses_naming_the_unused_input(tmp_path):
    app, conn, ident, desk, _script = _rig(tmp_path)
    try:
        app._node_function_author = lambda tenant: FakeAuthor(PROMISES_UNREAD)
        reply = _chat(app, ident, f"build me a node that {GOAL}")
        assert reply.status == 200
        assert "failed birth verification" in reply.body["reply"]
        assert "the interface promises `source_file`" in reply.body["reply"]
        assert "never reads it" in reply.body["reply"]
    finally:
        conn.close()


def test_promised_inputs_with_no_bindings_read_refuse(tmp_path):
    app, conn, ident, desk, _script = _rig(tmp_path)
    try:
        app._node_function_author = lambda tenant: FakeAuthor(NEVER_READS)
        reply = _chat(app, ident, f"build me a node that {GOAL}")
        assert reply.status == 200
        assert "failed birth verification" in reply.body["reply"]
        assert "never reads ./bindings.json" in reply.body["reply"]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Typed samples: the contract is exercised, not imagined.                      #
# --------------------------------------------------------------------------- #
def test_typed_samples_let_an_input_reading_function_pass(tmp_path):
    app, conn, ident, desk, _script = _rig(tmp_path)
    try:
        app._contract_executors = {"script": _runner(tmp_path)}
        app._node_function_author = lambda tenant: FakeAuthor(READS_ITS_INPUT)
        reply = _chat(app, ident, f"build me a node that {GOAL}")
        assert reply.status == 200
        assert "Built a NEW node" in reply.body["reply"]
    finally:
        conn.close()


def test_an_honest_error_against_staged_samples_fails_birth(tmp_path):
    app, conn, ident, desk, _script = _rig(tmp_path)
    try:
        app._contract_executors = {"script": _runner(tmp_path)}
        app._node_function_author = lambda tenant: FakeAuthor(
            HONEST_AGAINST_SAMPLES
        )
        reply = _chat(app, ident, f"build me a node that {GOAL}")
        assert reply.status == 200
        assert "failed birth verification" in reply.body["reply"]
        assert "staged sample inputs" in reply.body["reply"]
    finally:
        conn.close()


def test_the_web_grant_gap_still_passes_and_the_receipt_says_so(tmp_path):
    app, conn, ident, desk, _script = _rig(tmp_path)
    try:
        app._contract_executors = {"script": _runner(tmp_path)}
        app._node_function_author = lambda tenant: FakeAuthor(WEB_HONEST)
        reply = _chat(app, ident, f"build me a node that {GOAL}")
        assert reply.status == 200
        assert "Built a NEW node" in reply.body["reply"]
        # The residue is in the user's words, not only on the ledger.
        assert "said so honestly" in reply.body["reply"]
    finally:
        conn.close()


def test_the_receipt_names_its_repair_rounds(tmp_path):
    app, conn, ident, desk, _script = _rig(tmp_path)
    try:
        app._contract_executors = {
            "script": _FailingScriptHand("undefined_name")
        }
        author = _RepairingAuthor()
        app._node_function_author = lambda tenant: author
        reply = _chat(app, ident, f"build me a node that {GOAL}")
        assert reply.status == 200
        assert "Built a NEW node" in reply.body["reply"]
        assert "repair round" in reply.body["reply"]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The sample generator and the io-aware finish gate.                           #
# --------------------------------------------------------------------------- #
def test_sample_bindings_are_typed_and_path_slots_stage_files():
    bindings, extra = GatewayApp._sample_bindings(
        [
            {"name": "text", "type": "str", "example": "hello"},
            {"name": "count", "type": "number"},
            {"name": "rate", "type": "number", "example": "2.5"},
            {"name": "source_file", "type": "path"},
        ]
    )
    assert bindings["text"] == "hello"
    assert bindings["count"] == 2
    assert bindings["rate"] == 2.5
    # A path slot stages a real file and points the binding at it.
    assert bindings["source_file"] in extra
    assert "sample content" in extra[bindings["source_file"]]
    # Every declared slot has a value: nothing verifies against a void.
    assert set(bindings) == {"text", "count", "rate", "source_file"}


def test_the_finish_gate_hands_the_declared_contract_to_verify():
    from oolu.author import NodeAuthorAgent

    seen: list = []

    def io_aware(script, io=None):
        seen.append(io)
        return {"ok": True}

    agent = NodeAuthorAgent(object(), verify=io_aware)
    io = {
        "inputs": [{"name": "text", "type": "str"}],
        "outputs": [{"name": "result", "type": "str"}],
    }
    script = (
        "import json\n"
        "from _oolu_runtime import emit_result\n"
        "with open('bindings.json', encoding='utf-8') as fh:\n"
        "    text = json.load(fh)['text']\n"
        "emit_result({'result': text})\n"
    )
    assert agent._script_problem(script, io) is None
    assert seen == [io]

    # A one-argument hand (test doubles, custom seams) keeps working.
    def one_arg(script):
        return {"ok": True}

    legacy = NodeAuthorAgent(object(), verify=one_arg)
    assert legacy._script_problem(script, io) is None


def test_the_prose_channel_declares_secrets(tmp_path):
    from oolu.chat import parse_node_secrets

    declared = parse_node_secrets(
        "1. Plan.\n"
        'SECRETS: [{"name": "api_key", "label": "Your key", '
        '"host": "API.Example.com"}]\n'
        "```python\npass\n```\n"
    )
    assert declared == [
        {"name": "api_key", "label": "Your key", "host": "api.example.com"}
    ]
    assert parse_node_secrets("no line at all") == []
    assert parse_node_secrets("SECRETS: not json") == []
    assert json.dumps(declared)  # plain data, serializable into io
