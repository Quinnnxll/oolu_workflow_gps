"""Phase 2 of the context-harness plan: one canonical model interface.

The manifest registry answers what a model can do (routing asks it, not
``hasattr``); the chat stack constructs every provider request through
one ``_call_*`` path; extended thinking survives tool turns via the
verbatim annex and is shed cleanly on a provider switch; structured
output is schema-forced and validated with a correction round; the
one-shot author refuses a broken interface declaration instead of
silently publishing a guess; and token counting exists as a seam.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from oolu.billing import ModelCallMeter
from oolu.chat import author_node_function, parse_node_io_checked
from oolu.durable.connection import DurableConnection
from oolu.providers.base import ProviderResponse
from oolu.providers.chatmodel import ChatModelRouter
from oolu.providers.keyring import ModelKeyring
from oolu.providers.registry import manifest_for
from oolu.providers.tokens import count_request_tokens, estimate_tokens
from oolu.providers.tools import (
    StructuredOutputError,
    ToolRouter,
    ToolSpec,
    parse_anthropic_tool_reply,
    to_anthropic_messages,
    to_openai_messages,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmarks"))


# --------------------------------------------------------------------------- #
# The manifest registry                                                        #
# --------------------------------------------------------------------------- #
def test_declared_models_carry_their_powers():
    sonnet = manifest_for("claude-sonnet-5")
    assert sonnet.tool_calling and sonnet.thinking and sonnet.prompt_caching
    mini = manifest_for("gpt-4o-mini")
    assert mini.tool_calling and not mini.reasoning_effort
    o3 = manifest_for("o3-mini")
    assert o3.tool_calling and o3.reasoning_effort


def test_unknown_local_ids_are_inferred_conservatively():
    qwen = manifest_for("qwen3:4b", provider="local")
    assert not qwen.tool_calling  # junk tool JSON is their known failure
    assert qwen.provider == "local"
    elder = manifest_for("claude-3-5-sonnet-20241022")
    assert elder.tool_calling and not elder.thinking


def test_the_operator_overlay_wins_without_a_code_change(monkeypatch):
    monkeypatch.setenv(
        "OOLU_MODEL_MANIFESTS", '{"qwen3:32b": {"tool_calling": true}}'
    )
    assert manifest_for("qwen3:32b").tool_calling
    monkeypatch.setenv("OOLU_MODEL_MANIFESTS", "not json at all")
    assert not manifest_for("qwen3:32b").tool_calling  # broken overlay = base


# --------------------------------------------------------------------------- #
# The router answers from the manifest, not the object shape                   #
# --------------------------------------------------------------------------- #
class FakeTransport:
    def __init__(self):
        self.responses = {}
        self.requests = []

    def script(self, host, status, body):
        self.responses[host] = ProviderResponse(status=status, json=body)

    def request(self, method, url, *, headers=None, body=None, timeout=30.0):
        self.requests.append({"url": url, "body": body})
        for host, response in self.responses.items():
            if host in url:
                return response
        return ProviderResponse(status=500, json={"error": "unscripted"})


@pytest.fixture()
def rig(tmp_path):
    conn = DurableConnection(tmp_path / "durable.db")
    keyring = ModelKeyring(conn, key_path=tmp_path / "machine.key")
    transport = FakeTransport()
    yield keyring, transport
    conn.close()


def _router(keyring, transport, **kwargs):
    return ChatModelRouter(
        keyring, "t1", transport=transport, meter=ModelCallMeter(), **kwargs
    )


def test_consult_ready_reflects_the_answering_models_manifest(rig):
    keyring, transport = rig
    keyring.store("t1", "anthropic", "sk-ant-0123456789")
    keyed = _router(keyring, transport)
    assert keyed.answering_model() == ("anthropic", "claude-haiku-4-5-20251001")
    assert keyed.consult_ready()

    local = _router(
        keyring,
        transport,
        source=lambda: "local",
        local_url=lambda: "http://127.0.0.1:11434/v1",
        local_model=lambda: "qwen3:4b",
    )
    assert local.answering_model() == ("local", "qwen3:4b")
    # Every router HAS consult; the manifest says this model shouldn't
    # be trusted with it — the exact distinction hasattr never made.
    assert hasattr(local, "consult") and not local.consult_ready()


def test_a_keyless_router_reports_nothing_would_answer(rig):
    keyring, transport = rig
    router = _router(keyring, transport, source=lambda: "own-api")
    assert router.answering_model() == ("", "")
    assert not router.consult_ready()


# --------------------------------------------------------------------------- #
# Thinking survives tool turns, and provider switches shed it cleanly          #
# --------------------------------------------------------------------------- #
THINKING_REPLY = {
    "model": "claude-sonnet-5",
    "content": [
        {"type": "thinking", "thinking": "slots first...", "signature": "sig-1"},
        {"type": "text", "text": "checking the desk"},
        {
            "type": "tool_use",
            "id": "call-1",
            "name": "list_nodes",
            "input": {},
        },
    ],
    "usage": {"input_tokens": 20, "output_tokens": 10},
}


def test_the_annex_round_trips_to_anthropic_verbatim():
    reply = parse_anthropic_tool_reply(THINKING_REPLY)
    assert reply.thinking_blocks[0]["signature"] == "sig-1"

    transcript = [reply.as_message()]
    wire = to_anthropic_messages(transcript)
    blocks = wire[0]["content"]
    # Thoughts lead, verbatim — signature intact — then text, then the call.
    assert blocks[0] == {
        "type": "thinking",
        "thinking": "slots first...",
        "signature": "sig-1",
    }
    assert [b["type"] for b in blocks] == ["thinking", "text", "tool_use"]


def test_a_provider_switch_sheds_thoughts_and_keeps_the_task():
    reply = parse_anthropic_tool_reply(THINKING_REPLY)
    transcript = [
        {"role": "user", "content": "build it"},
        reply.as_message(),
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "list_nodes",
            "content": "[]",
        },
    ]
    wire = to_openai_messages(transcript)
    # The OpenAI dialect never sees the annex, but the task survives:
    # the same assistant turn, the same tool call, the same answer.
    assert "thinking_blocks" not in wire[1]
    assert wire[1]["tool_calls"][0]["function"]["name"] == "list_nodes"
    assert wire[2]["role"] == "tool"


# --------------------------------------------------------------------------- #
# Structured output: schema-forced, validated, corrected                       #
# --------------------------------------------------------------------------- #
SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "score": {"type": "integer"},
    },
    "required": ["name", "score"],
    "additionalProperties": False,
}


def _delivery(arguments):
    return {
        "model": "claude-sonnet-5",
        "content": [
            {
                "type": "tool_use",
                "id": "d-1",
                "name": "deliver_result",
                "input": arguments,
            }
        ],
        "usage": {"input_tokens": 5, "output_tokens": 5},
    }


class SequenceTransport(FakeTransport):
    """Answers each request with the next scripted response."""

    def __init__(self, bodies):
        super().__init__()
        self._bodies = list(bodies)

    def request(self, method, url, *, headers=None, body=None, timeout=30.0):
        self.requests.append({"url": url, "body": body})
        return ProviderResponse(status=200, json=self._bodies.pop(0))


def test_structured_returns_validated_arguments(rig):
    keyring, _ = rig
    keyring.store("t1", "anthropic", "sk-ant-0123456789")
    transport = SequenceTransport([_delivery({"name": "route", "score": 9})])

    result = _router(keyring, transport).structured(
        [{"role": "user", "content": "score it"}], schema=SCHEMA
    )

    assert result == {"name": "route", "score": 9}
    # The synthetic tool was FORCED on the wire, schema riding as-is.
    body = transport.requests[-1]["body"]
    assert body["tool_choice"] == {"type": "tool", "name": "deliver_result"}
    assert body["tools"][0]["input_schema"] == SCHEMA


def test_a_schema_violation_costs_a_correction_round_not_a_default(rig):
    keyring, _ = rig
    keyring.store("t1", "anthropic", "sk-ant-0123456789")
    transport = SequenceTransport(
        [
            _delivery({"name": "route"}),  # missing score
            _delivery({"name": "route", "score": 7}),
        ]
    )

    result = _router(keyring, transport).structured(
        [{"role": "user", "content": "score it"}], schema=SCHEMA
    )

    assert result == {"name": "route", "score": 7}
    # The second request carried the validation failure back to the
    # model as a tool result it could read.
    retry_body = transport.requests[-1]["body"]
    flattened = str(retry_body["messages"])
    assert "missing required property" in flattened


def test_running_out_of_correction_rounds_raises_not_defaults(rig):
    keyring, _ = rig
    keyring.store("t1", "anthropic", "sk-ant-0123456789")
    transport = SequenceTransport(
        [_delivery({"name": "route"}), _delivery({"name": "route"})]
    )

    with pytest.raises(StructuredOutputError):
        _router(keyring, transport).structured(
            [{"role": "user", "content": "score it"}], schema=SCHEMA
        )


def test_an_undeclared_tool_call_is_refused_before_any_handler():
    router = ToolRouter()
    router.register(
        ToolSpec(name="known", description="d", parameters={"type": "object"}),
        lambda args: "ok",
    )
    from oolu.providers.tools import ToolCall

    result = router.dispatch(ToolCall(id="x", name="not_a_tool"))
    assert not result.success
    assert "unknown tool" in result.content


# --------------------------------------------------------------------------- #
# The one-shot author refuses a broken interface                               #
# --------------------------------------------------------------------------- #
GOOD_SCRIPT = (
    "```python\n"
    "import json\n"
    "from _oolu_runtime import emit_result\n"
    "with open('bindings.json', encoding='utf-8') as fh:\n"
    "    bindings = json.load(fh)\n"
    "emit_result({'slug': bindings['title'].lower()})\n"
    "```\n"
)


class _BrokenIoAuthor:
    def reply(self, messages):
        return "1. Do it.\nIO: {not valid json]\n" + GOOD_SCRIPT


class _NoIoAuthor:
    def reply(self, messages):
        return "1. Do it.\n" + GOOD_SCRIPT


def test_a_malformed_io_line_refuses_the_build():
    script, io, refusal = author_node_function(_BrokenIoAuthor(), "slugify it")
    assert script is None
    assert "broken interface declaration" in refusal


def test_an_absent_io_line_stays_lenient_for_the_prose_channel():
    script, io, refusal = author_node_function(_NoIoAuthor(), "slugify it")
    assert script is not None
    assert io == {"inputs": [], "outputs": [{"name": "result", "type": "str"}]}


def test_parse_node_io_checked_names_the_problem():
    io, problem = parse_node_io_checked('IO: ["a", "list"]')
    assert "must declare a JSON object" in problem
    io, problem = parse_node_io_checked("no declaration here")
    assert problem == ""


# --------------------------------------------------------------------------- #
# The bench dispatch honors the manifest port                                  #
# --------------------------------------------------------------------------- #
def test_the_bench_routes_a_manifestless_tool_speaker_one_shot():
    from node_authoring import BenchScriptRunner, GOALS, author_goal

    class OneShotByManifest:
        """Has consult (as every router does) but the manifest port says
        the seated model cannot be trusted with tools."""

        def consult(self, *a, **k):  # pragma: no cover - must not be called
            raise AssertionError("manifest said no tools — consult used anyway")

        def consult_ready(self):
            return False

        def reply(self, messages):
            return "NO_TASK"

    goal = next(g for g in GOALS if g.kind == "conversation")
    authored = author_goal(OneShotByManifest(), goal, BenchScriptRunner())
    assert authored.script is None  # declined through the one-shot gates


# --------------------------------------------------------------------------- #
# Token accounting exists, and errs on the safe side                           #
# --------------------------------------------------------------------------- #
def test_token_estimates_are_positive_and_monotonic():
    assert estimate_tokens("") == 0
    short = estimate_tokens("emit_result({'a': 1})")
    long = estimate_tokens("emit_result({'a': 1})" * 50)
    assert 0 < short < long


def test_request_counting_includes_tools_and_overhead():
    messages = [
        {"role": "system", "content": "You write node functions."},
        {"role": "user", "content": "build the thing"},
    ]
    bare = count_request_tokens(messages)
    tool = ToolSpec(
        name="finish_node",
        description="Deliver the finished node function.",
        parameters={
            "type": "object",
            "properties": {"script": {"type": "string"}},
        },
    )
    with_tools = count_request_tokens(messages, tools=[tool])
    assert bare > estimate_tokens(messages[0]["content"])  # overhead counted
    assert with_tools > bare
