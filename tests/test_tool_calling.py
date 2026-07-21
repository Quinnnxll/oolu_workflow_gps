"""Native tool-calling: one schema, both wires, validated before dispatch.

Everything runs offline. The wire shapes are asserted against a scripted
transport, the validator against plain data, and the loop against a fake
consult — the same posture as the rest of the provider suite: logic
contract-tested here, the live network the only remaining seam.
"""

from __future__ import annotations

import json

import pytest

from oolu.billing import ModelCallMeter
from oolu.durable.connection import DurableConnection
from oolu.providers import (
    ToolCall,
    ToolLoopLimit,
    ToolReply,
    ToolRouter,
    ToolSpec,
    run_tool_loop,
    validate_arguments,
)
from oolu.providers.base import ProviderResponse
from oolu.providers.chatmodel import ChatModelRouter
from oolu.providers.keyring import ModelKeyring
from oolu.providers.tools import (
    parse_anthropic_tool_reply,
    parse_openai_tool_reply,
    to_anthropic_messages,
    to_openai_messages,
)

READ_UPSTREAM = ToolSpec(
    name="read_upstream_output",
    description="Read the previous node's latest output payload.",
    parameters={
        "type": "object",
        "properties": {
            "node_id": {"type": "string", "minLength": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "required": ["node_id"],
        "additionalProperties": False,
    },
)


# --------------------------------------------------------------------------- #
# The spec renders onto both wires.                                           #
# --------------------------------------------------------------------------- #
def test_one_spec_renders_both_wire_dialects():
    openai = READ_UPSTREAM.as_openai()
    assert openai["type"] == "function"
    assert openai["function"]["name"] == "read_upstream_output"
    assert openai["function"]["parameters"] == READ_UPSTREAM.parameters

    anthropic = READ_UPSTREAM.as_anthropic()
    assert anthropic["name"] == "read_upstream_output"
    assert anthropic["input_schema"] == READ_UPSTREAM.parameters


# --------------------------------------------------------------------------- #
# The validator enforces the declared schema.                                 #
# --------------------------------------------------------------------------- #
def test_valid_arguments_pass_clean():
    assert validate_arguments(
        READ_UPSTREAM.parameters, {"node_id": "n-1", "limit": 3}
    ) == []


def test_missing_required_and_wrong_type_are_named():
    problems = validate_arguments(READ_UPSTREAM.parameters, {"limit": "three"})
    assert any("missing required property 'node_id'" in p for p in problems)
    assert any("expected integer" in p for p in problems)


def test_undeclared_properties_are_refused_when_the_schema_closes_the_door():
    problems = validate_arguments(
        READ_UPSTREAM.parameters, {"node_id": "n-1", "surprise": True}
    )
    assert any("unexpected property 'surprise'" in p for p in problems)


def test_a_boolean_is_not_an_integer():
    # bool subclasses int in Python; the schema contract must not care.
    problems = validate_arguments(READ_UPSTREAM.parameters, {
        "node_id": "n-1", "limit": True,
    })
    assert any("expected integer, got bool" in p for p in problems)


def test_bounds_enum_pattern_and_nesting_hold():
    schema = {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["fast", "careful"]},
            "retries": {"type": "integer", "minimum": 0, "maximum": 3},
            "tag": {"type": "string", "pattern": "^[a-z]+$"},
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
            },
        },
    }
    assert validate_arguments(
        schema,
        {"mode": "fast", "retries": 2, "tag": "abc", "steps": [{"name": "s"}]},
    ) == []
    problems = validate_arguments(
        schema,
        {"mode": "wild", "retries": 9, "tag": "UPPER", "steps": [{}]},
    )
    assert any("not one of" in p for p in problems)
    assert any("above maximum 3" in p for p in problems)
    assert any("does not match pattern" in p for p in problems)
    assert any("steps[0]: missing required property 'name'" in p for p in problems)


# --------------------------------------------------------------------------- #
# One neutral transcript, two dialects.                                       #
# --------------------------------------------------------------------------- #
NEUTRAL = [
    {"role": "user", "content": "read the upstream node"},
    {
        "role": "assistant",
        "content": "Reading it now.",
        "tool_calls": [
            {"id": "c1", "name": "read_upstream_output",
             "arguments": {"node_id": "n-1"}},
        ],
    },
    {"role": "tool", "tool_call_id": "c1", "name": "read_upstream_output",
     "content": '{"rows": 3}'},
]


def test_openai_dialect_carries_arguments_as_json_text():
    wire = to_openai_messages(NEUTRAL)
    call = wire[1]["tool_calls"][0]
    assert call["type"] == "function"
    assert json.loads(call["function"]["arguments"]) == {"node_id": "n-1"}
    assert wire[2] == {
        "role": "tool", "tool_call_id": "c1", "content": '{"rows": 3}'
    }


def test_anthropic_dialect_speaks_blocks_and_merges_batched_results():
    two_results = NEUTRAL + [
        {"role": "tool", "tool_call_id": "c2", "name": "other",
         "content": "second answer"},
    ]
    wire = to_anthropic_messages(two_results)
    blocks = wire[1]["content"]
    assert blocks[0] == {"type": "text", "text": "Reading it now."}
    assert blocks[1]["type"] == "tool_use"
    assert blocks[1]["input"] == {"node_id": "n-1"}
    # Both tool answers ride ONE user turn — they answer one batch of calls.
    results = wire[2]
    assert results["role"] == "user"
    assert [b["tool_use_id"] for b in results["content"]] == ["c1", "c2"]


def test_openai_replies_parse_including_malformed_argument_json():
    reply = parse_openai_tool_reply({
        "choices": [{"message": {
            "content": None,
            "tool_calls": [
                {"id": "a", "function": {
                    "name": "read_upstream_output",
                    "arguments": '{"node_id": "n-1"}'}},
                {"id": "b", "function": {"name": "broken",
                                         "arguments": "{not json"}},
            ],
        }}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 5},
    })
    good, bad = reply.tool_calls
    assert good.arguments == {"node_id": "n-1"} and good.malformed is None
    assert bad.arguments == {} and "not valid JSON" in bad.malformed
    assert (reply.prompt_tokens, reply.completion_tokens) == (7, 5)


def test_anthropic_replies_parse_text_and_tool_use_blocks():
    reply = parse_anthropic_tool_reply({
        "content": [
            {"type": "text", "text": "Let me check."},
            {"type": "tool_use", "id": "t1", "name": "read_upstream_output",
             "input": {"node_id": "n-1"}},
        ],
        "usage": {"input_tokens": 11, "output_tokens": 4},
    })
    assert reply.text == "Let me check."
    assert reply.tool_calls[0].id == "t1"
    assert reply.tool_calls[0].arguments == {"node_id": "n-1"}
    assert (reply.prompt_tokens, reply.completion_tokens) == (11, 4)


# --------------------------------------------------------------------------- #
# The router: nothing unvalidated reaches a handler.                          #
# --------------------------------------------------------------------------- #
def _router_with_handler(outcome=None):
    seen: list[dict] = []

    def handler(arguments: dict):
        seen.append(arguments)
        return outcome if outcome is not None else {"rows": 3}

    router = ToolRouter()
    router.register(READ_UPSTREAM, handler)
    return router, seen


def test_a_valid_call_reaches_the_handler_and_json_travels_back():
    router, seen = _router_with_handler()
    result = router.dispatch(
        ToolCall(id="c1", name="read_upstream_output",
                 arguments={"node_id": "n-1"})
    )
    assert seen == [{"node_id": "n-1"}]
    assert result.success and json.loads(result.content) == {"rows": 3}
    assert result.as_message()["role"] == "tool"


def test_schema_violations_never_reach_the_handler():
    router, seen = _router_with_handler()
    result = router.dispatch(
        ToolCall(id="c1", name="read_upstream_output", arguments={"limit": 1})
    )
    assert seen == []
    assert not result.success
    assert "missing required property 'node_id'" in result.content


def test_unknown_tools_and_malformed_json_are_answered_not_raised():
    router, _seen = _router_with_handler()
    unknown = router.dispatch(ToolCall(id="x", name="nope", arguments={}))
    assert not unknown.success and "unknown tool 'nope'" in unknown.content
    assert "read_upstream_output" in unknown.content  # names the real doors
    broken = router.dispatch(
        ToolCall(id="y", name="read_upstream_output", malformed="bad JSON")
    )
    assert not broken.success and broken.content == "bad JSON"


def test_a_handler_crash_becomes_an_answer_the_model_can_read():
    router = ToolRouter()

    def handler(_arguments):
        raise RuntimeError("store is closed")

    router.register(READ_UPSTREAM, handler)
    result = router.dispatch(
        ToolCall(id="c1", name="read_upstream_output",
                 arguments={"node_id": "n-1"})
    )
    assert not result.success
    assert "RuntimeError: store is closed" in result.content


def test_a_name_registers_once():
    router, _seen = _router_with_handler()
    with pytest.raises(ValueError):
        router.register(READ_UPSTREAM, lambda a: a)


# --------------------------------------------------------------------------- #
# The bounded loop.                                                           #
# --------------------------------------------------------------------------- #
def test_the_loop_dispatches_feeds_back_and_returns_the_final_text():
    router, seen = _router_with_handler()
    replies = iter([
        ToolReply(text="", tool_calls=(
            ToolCall(id="c1", name="read_upstream_output",
                     arguments={"node_id": "n-1"}),
        )),
        ToolReply(text="The upstream node produced 3 rows."),
    ])
    consults: list[list[dict]] = []

    def consult(messages):
        consults.append(list(messages))
        return next(replies)

    text, transcript = run_tool_loop(
        consult, [{"role": "user", "content": "check upstream"}], router
    )
    assert text == "The upstream node produced 3 rows."
    assert seen == [{"node_id": "n-1"}]
    # The second consultation saw the tool's answer.
    assert any(
        m.get("role") == "tool" and json.loads(m["content"]) == {"rows": 3}
        for m in consults[1]
    )
    # The transcript keeps the whole exchange for audit or follow-up.
    assert [m["role"] for m in transcript] == [
        "user", "assistant", "tool", "assistant"
    ]


def test_the_loop_stops_at_its_ceiling():
    router, _seen = _router_with_handler()

    def consult(_messages):
        return ToolReply(text="", tool_calls=(
            ToolCall(id="c", name="read_upstream_output",
                     arguments={"node_id": "n-1"}),
        ))

    with pytest.raises(ToolLoopLimit):
        run_tool_loop(
            consult, [{"role": "user", "content": "go"}], router, max_steps=2
        )


# --------------------------------------------------------------------------- #
# consult(): the router speaks both provider wires, offline.                  #
# --------------------------------------------------------------------------- #
class FakeTransport:
    """Scripted per-host responses; records every request it carries."""

    def __init__(self):
        self.responses: dict[str, ProviderResponse] = {}
        self.requests: list[dict] = []

    def script(self, host: str, status: int, body: dict):
        self.responses[host] = ProviderResponse(status=status, json=body)

    def request(self, method, url, *, headers=None, body=None, timeout=30.0):
        self.requests.append(
            {"method": method, "url": url, "headers": dict(headers or {}),
             "body": body}
        )
        for host, response in self.responses.items():
            if host in url:
                return response
        return ProviderResponse(status=500, json={"error": "unscripted"})


@pytest.fixture()
def rig(tmp_path):
    conn = DurableConnection(tmp_path / "durable.db")
    keyring = ModelKeyring(conn, key_path=tmp_path / "machine.key")
    transport = FakeTransport()
    meter = ModelCallMeter()
    yield keyring, transport, meter
    conn.close()


MESSAGES = [
    {"role": "system", "content": "You are OoLu."},
    {"role": "user", "content": "read the upstream node"},
]


def test_consult_rides_anthropic_natively_and_parses_tool_use(rig):
    keyring, transport, meter = rig
    keyring.store("t1", "anthropic", "sk-ant-0123456789")
    transport.script("anthropic.com", 200, {
        "model": "claude-sonnet-5",
        "content": [
            {"type": "tool_use", "id": "t1", "name": "read_upstream_output",
             "input": {"node_id": "n-1"}},
        ],
        "usage": {"input_tokens": 30, "output_tokens": 9},
    })
    router = ChatModelRouter(keyring, "t1", transport=transport, meter=meter)

    reply = router.consult(MESSAGES, tools=[READ_UPSTREAM])

    assert reply.tool_calls[0].name == "read_upstream_output"
    assert reply.tool_calls[0].arguments == {"node_id": "n-1"}
    body = transport.requests[-1]["body"]
    assert body["tools"] == [READ_UPSTREAM.as_anthropic()]
    assert body["system"] == "You are OoLu."
    # A pure tool-call turn (no text) is a live answer, and it enters the
    # books like any other consultation.
    (charge,) = meter.charges()
    assert (charge.prompt_tokens, charge.completion_tokens) == (30, 9)


def test_consult_rides_the_openai_wire_and_the_choice_maps(rig):
    keyring, transport, meter = rig
    keyring.store("t1", "openai", "sk-openai-0123456789")
    transport.script("openai.com", 200, {
        "model": "gpt-4o-mini",
        "choices": [{"message": {
            "content": None,
            "tool_calls": [{"id": "c1", "function": {
                "name": "read_upstream_output",
                "arguments": '{"node_id": "n-1"}'}}],
        }}],
        "usage": {"prompt_tokens": 30, "completion_tokens": 9},
    })
    router = ChatModelRouter(keyring, "t1", transport=transport, meter=meter)

    reply = router.consult(
        MESSAGES, tools=[READ_UPSTREAM], tool_choice="required"
    )

    assert reply.tool_calls[0].arguments == {"node_id": "n-1"}
    body = transport.requests[-1]["body"]
    assert body["tools"] == [READ_UPSTREAM.as_openai()]
    assert body["tool_choice"] == "required"


def test_consult_converts_a_transcript_with_tool_answers_per_dialect(rig):
    keyring, transport, meter = rig
    keyring.store("t1", "anthropic", "sk-ant-0123456789")
    transport.script("anthropic.com", 200, {
        "model": "claude-sonnet-5",
        "content": [{"type": "text", "text": "3 rows upstream."}],
        "usage": {"input_tokens": 50, "output_tokens": 8},
    })
    router = ChatModelRouter(keyring, "t1", transport=transport, meter=meter)

    reply = router.consult(MESSAGES + NEUTRAL[1:], tools=[READ_UPSTREAM])

    assert reply.text == "3 rows upstream."
    wire = transport.requests[-1]["body"]["messages"]
    assert wire[-2]["content"][-1]["type"] == "tool_use"
    assert wire[-1]["content"][0]["type"] == "tool_result"


def test_anthropic_carries_declared_tools_and_web_search_side_by_side(rig):
    keyring, transport, meter = rig
    keyring.store("t1", "anthropic", "sk-ant-0123456789")
    transport.script("anthropic.com", 200, {
        "model": "claude-sonnet-5",
        "content": [{"type": "text", "text": "done"}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    })
    router = ChatModelRouter(
        keyring, "t1", transport=transport, meter=meter,
        web_search=lambda: True,
    )

    router.consult(MESSAGES, tools=[READ_UPSTREAM])

    names = [t["name"] for t in transport.requests[-1]["body"]["tools"]]
    assert names == ["read_upstream_output", "web_search"]
