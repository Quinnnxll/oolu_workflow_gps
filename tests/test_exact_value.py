"""The exact-value rules: real computation only, values from the runtime.

Exit gate (the mock-code hallucination fix): an authored function that
only PRETENDS — emit_result handed a constant the model wrote, or code
naming its own mock/placeholder data — is refused at both authoring
doors with the reason named, so the model corrects instead of shipping
a fabricated success. And the runtime supplies the values: a node's
resolved bindings ride into the sandbox as ./bindings.json on every
run, so the function reads the exact values the runtime bound, never
literals the model retyped.
"""

from __future__ import annotations

from test_node_interact import FakeAuthor

from oolu.author import NodeAuthorAgent
from oolu.chat import author_node_function
from oolu.nodeplace.screening import mock_smells

COMPUTES = (
    "from _oolu_runtime import emit_result\n"
    "import json\n"
    "rows = json.load(open('bindings.json'))\n"
    "emit_result(str(len(rows)))"
)


def test_the_screen_names_every_way_a_function_pretends():
    # A constant answer, in any dress, is a baked-in value.
    assert mock_smells("emit_result('done: 42 rows')")
    assert mock_smells("emit_result({'rows': 3, 'status': 'ok'})")
    assert mock_smells("emit_result(f'processed {5} rows')")
    # Naming the pretending is the other tell.
    assert mock_smells("data = make_mock_data()\nemit_result(compute(data))")
    assert mock_smells("rows = sample_data()  # placeholder\nemit_result(x)")
    # Real computation passes clean.
    assert mock_smells(COMPUTES) == []
    assert mock_smells(
        "r = http_request('https://api.example.com')\nemit_result(r['body'])"
    ) == []
    # Broken syntax stays quiet here — the sandbox speaks to syntax.
    assert mock_smells("emit_result(") == []


def test_the_one_shot_door_refuses_a_pretending_function():
    mocked = FakeAuthor(
        "1. Pretend.\n```python\nfrom _oolu_runtime import emit_result\n"
        "emit_result({'invoices': 12, 'total': '99.50'})\n```"
    )
    script, io, refusal = author_node_function(mocked, "normalize invoices")
    assert script is None
    assert "only pretends" in refusal
    assert "COMPUTED from real inputs" in refusal


def test_the_agent_gate_refuses_then_takes_the_corrected_function():
    from test_node_author import ConsultModel, _finish

    model = ConsultModel([
        _finish(script=(
            "from _oolu_runtime import emit_result\n"
            "emit_result('all 3 invoices normalized')"
        )),
        _finish(script=COMPUTES),
    ])
    authored = NodeAuthorAgent(model).author("normalize invoices")
    assert authored.script == COMPUTES
    refusals = [
        m["content"]
        for turn in model.transcripts
        for m in turn
        if m.get("role") == "tool"
    ]
    assert any("only pretends" in c for c in refusals)


def test_bindings_ride_into_the_sandbox_as_data(tmp_path):
    import json

    from oolu.cache.store import LocalScriptCache
    from oolu.runtime.backend import StubBackend, make_success
    from oolu.runtime.script_node import NodeScriptRunner
    from oolu.skills.models import ActionEvent

    backend = StubBackend([make_success({"ok": True})])
    runner = NodeScriptRunner(backend, LocalScriptCache(":memory:"))
    runner.execute(
        ActionEvent(
            correlation_id="fn",
            adapter="script",
            operation="run",
            parameters={
                "goal": "normalize",
                "script": COMPUTES,
                "bindings": {"invoice_csv": "a.csv", "limit": 3},
            },
        ),
        idempotency_key="run-1",
    )
    (request,) = backend.requests[-1:]
    staged = json.loads(request.files["bindings.json"])
    # The exact values the runtime bound — typed, sorted, verbatim.
    assert staged == {"invoice_csv": "a.csv", "limit": 3}
