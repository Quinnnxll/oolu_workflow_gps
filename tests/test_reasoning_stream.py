"""The two reported failures, pinned: the chat's live reasoning on the
Claude path (the breathing light's feed), and the representative keeping
its drafting frame on an assistant-tuned brain.

Issue 1 — "reasoning shows only … and sometimes sticks": the Anthropic
path never streamed (one blocking chunk, no ⟨think⟩), and the 30s
transport default timed long generations out into retries. Now the
Messages API streams, thinking deltas ride the ⟨think⟩ contract, and the
chat adapters carry generation-shaped timeouts.

Issue 2 — "forgets it's drafting for quinn": the persona system prompt
always rode the wire, but the generation point was a naked question an
assistant-tuned model answers as itself; and a history window opening
with the account's own words was an Anthropic 400. Both are pinned here.
"""

from __future__ import annotations

import json

from oolu.providers.apikey import AnthropicAdapter, anthropic_sse_events
from oolu.providers.chatmodel import ChatModelRouter
from oolu.providers.tools import to_anthropic_messages
from oolu.providers.transport import ProviderResponse
from oolu.providers.vault import SecretVault
from oolu.representative.models import PersonaCard
from oolu.representative.persona import build_messages

CARD = PersonaCard(display_name="quinn", about="runs mphepo.io")


def _sse(obj: dict) -> str:
    return "data: " + json.dumps(obj)


ANTHROPIC_STREAM_LINES = [
    "event: message_start",
    _sse(
        {
            "type": "message_start",
            "message": {"usage": {"input_tokens": 42}},
        }
    ),
    _sse(
        {
            "type": "content_block_delta",
            "delta": {"type": "thinking_delta", "thinking": "quinn is busy"},
        }
    ),
    _sse(
        {
            "type": "content_block_delta",
            "delta": {"type": "thinking_delta", "thinking": " tomorrow…"},
        }
    ),
    _sse(
        {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "Can't make 9 — "},
        }
    ),
    _sse(
        {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "does 11 work?"},
        }
    ),
    _sse({"type": "message_delta", "usage": {"output_tokens": 17}}),
    _sse({"type": "message_stop"}),
]


class StreamingFakeTransport:
    """Captures requests; streams scripted SSE lines; records timeouts."""

    def __init__(self, lines=None):
        self.lines = list(lines or [])
        self.requests: list[dict] = []
        self.stream_calls: list[dict] = []

    def request(self, method, url, *, headers=None, body=None, timeout=30.0):
        self.requests.append({"url": url, "body": body, "timeout": timeout})
        return ProviderResponse(
            status=200,
            json={
                "content": [{"type": "text", "text": "blocking reply"}],
                "stop_reason": "end_turn",
                "model": "claude-sonnet-5",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    def stream(self, method, url, *, headers=None, body=None, timeout=120.0):
        self.stream_calls.append({"url": url, "body": body})
        yield from self.lines


class NoStreamTransport(StreamingFakeTransport):
    """A transport without a stream method — the blocking fallback path."""

    stream = None


class FakeKeyring:
    def secret_for(self, tenant, provider):
        return "sk-ant-test" if provider == "anthropic" else None


def router(transport) -> ChatModelRouter:
    return ChatModelRouter(
        keyring=FakeKeyring(),
        tenant="t1",
        transport=transport,
        source=lambda: "own-api",
        preference=lambda: "anthropic",
        tier=lambda: "reasoning",
        purpose="chat.turn",
    )


# --- issue 1: the live reasoning feed ----------------------------------


def test_anthropic_sse_events_parse_thinking_and_text() -> None:
    events = list(anthropic_sse_events(iter(ANTHROPIC_STREAM_LINES)))
    kinds = [(kind, delta) for kind, delta, _ in events if delta]
    assert kinds == [
        ("thinking", "quinn is busy"),
        ("thinking", " tomorrow…"),
        ("text", "Can't make 9 — "),
        ("text", "does 11 work?"),
    ]
    usages = [usage for _, _, usage in events if usage]
    assert usages[-1] == {"input_tokens": 42, "output_tokens": 17}


def test_claude_reply_stream_frames_thinking_for_the_bubble() -> None:
    # The chat's _split_reasoning contract: thinking rides ⟨think⟩ framing,
    # so the UI's breathing bubble shows the model's actual reasoning.
    transport = StreamingFakeTransport(ANTHROPIC_STREAM_LINES)
    deltas = list(router(transport).reply_stream([{"role": "user", "content": "hi"}]))
    joined = "".join(deltas)
    assert joined == (
        "<think>quinn is busy tomorrow…</think>Can't make 9 — does 11 work?"
    )
    # The wire asked for a stream, with the seat's thinking budget riding.
    [call] = transport.stream_calls
    assert call["body"]["stream"] is True
    assert call["body"]["thinking"] == {"type": "enabled", "budget_tokens": 1024}


def test_reply_stream_falls_back_to_blocking_before_first_delta() -> None:
    transport = NoStreamTransport()
    deltas = list(router(transport).reply_stream([{"role": "user", "content": "hi"}]))
    assert deltas == ["blocking reply"]


def test_chat_adapters_carry_generation_shaped_timeouts() -> None:
    transport = StreamingFakeTransport()
    vault = SecretVault()
    ref = vault.put("sk-ant-test", kind="api_key")
    adapter = AnthropicAdapter(vault=vault, transport=transport, api_key_ref=ref)
    adapter.messages([{"role": "user", "content": "hi"}], model="claude-sonnet-5")
    [request] = transport.requests
    assert request["timeout"] == 300.0  # not the transport's 30s default


# --- issue 2: the representative's drafting frame -----------------------


def test_the_generation_point_is_anchored_not_naked() -> None:
    messages = build_messages(
        CARD,
        [],
        "Can you join the supplier call tomorrow at 9?",
        history=[
            {"role": "user", "content": "hey"},
            {"role": "assistant", "content": "hey! what's up"},
        ],
        peer="jordan",
    )
    system = messages[0]
    assert system["role"] == "system"
    assert "drafting a message reply AS quinn" in system["content"]
    final = messages[-1]
    assert final["role"] == "user"
    assert "jordan just wrote:" in final["content"]
    assert "Can you join the supplier call tomorrow at 9?" in final["content"]
    assert "Write quinn's reply to jordan, in quinn's voice" in final["content"]


def test_persona_system_prompt_rides_the_claude_wire() -> None:
    # The regression the report suspected: the drafting identity must
    # reach the Messages API as the top-level system parameter.
    transport = StreamingFakeTransport(ANTHROPIC_STREAM_LINES)
    messages = build_messages(CARD, [], "you around?", peer="jordan")
    list(router(transport).reply_stream(messages))
    [call] = transport.stream_calls
    [system_block] = call["body"]["system"]
    assert "drafting a message reply AS quinn" in system_block["text"]
    assert all(m["role"] != "system" for m in call["body"]["messages"])


def test_assistant_first_windows_open_with_a_user_turn() -> None:
    # A rep thread whose visible window starts with the account's own
    # words was an Anthropic 400 ("first message must use the user role").
    wire = to_anthropic_messages(
        [
            {"role": "assistant", "content": "let me check and get back to you"},
            {"role": "user", "content": "any news?"},
        ]
    )
    assert wire[0] == {
        "role": "user",
        "content": "(conversation already in progress)",
    }
    assert [m["role"] for m in wire] == ["user", "assistant", "user"]
