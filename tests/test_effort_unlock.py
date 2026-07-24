"""Phase 1 of the context-harness plan: the model is no longer starved.

The seat-profile table replaces the universal 1024-token ceiling; effort
(output room, temperature, reasoning budgets) rides per PURPOSE and per
provider capability; retries actually wait between attempts; the frozen
prefix carries a prompt-cache breakpoint; and the chat model registry is
env-overridable. Every promise is pinned against the fake transport, so
the wire bodies themselves are the evidence.
"""

from __future__ import annotations

import pytest

from oolu.billing import ModelCallMeter
from oolu.durable.connection import DurableConnection
from oolu.models import ModelTier
from oolu.providers.base import ProviderResponse
from oolu.providers.chatmodel import ChatModelRouter, chat_model_for
from oolu.providers.keyring import ModelKeyring
from oolu.providers.profiles import (
    DEFAULT_PROFILE,
    SEAT_PROFILES,
    resolve_profile,
)
from oolu.providers.tools import ToolSpec
from oolu.routing.gateway import GatewayError, LiteLLMGateway
from oolu.routing.matrix import RoutingDecision
from oolu.routing.prompting import AssembledPrompt


def _anthropic_reply(text="fine."):
    return {
        "model": "claude-sonnet-5",
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def _openai_reply(text="fine."):
    return {
        "model": "gpt-4o-mini",
        "choices": [
            {"message": {"role": "assistant", "content": text},
             "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


class FakeTransport:
    def __init__(self):
        self.responses: dict[str, ProviderResponse] = {}
        self.requests: list[dict] = []

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


MESSAGES = [
    {"role": "system", "content": "You write node functions."},
    {"role": "user", "content": "build the thing"},
]


# --------------------------------------------------------------------------- #
# The profile table itself                                                     #
# --------------------------------------------------------------------------- #
def test_code_writing_seats_are_unstarved():
    for purpose in ("node.build", "node.repair", "plan.rebuild"):
        profile = resolve_profile(purpose)
        assert profile.max_tokens == 16384, purpose
        assert profile.temperature == 0.2
        assert profile.thinking_budget == 4096
        assert profile.reasoning_effort == "medium"


def test_the_bench_audits_the_same_seat_it_measures():
    assert SEAT_PROFILES["bench.node_authoring"] is SEAT_PROFILES["node.build"]


def test_unknown_purposes_get_honest_room_not_starvation():
    profile = resolve_profile("some.future.seat")
    assert profile is DEFAULT_PROFILE
    assert profile.max_tokens == 4096  # never the old universal 1024


# --------------------------------------------------------------------------- #
# What actually rides the wire, per provider                                   #
# --------------------------------------------------------------------------- #
def test_the_authoring_seat_thinks_on_anthropic(rig):
    keyring, transport = rig
    keyring.store("t1", "anthropic", "sk-ant-0123456789")
    transport.script("anthropic.com", 200, _anthropic_reply())

    _router(keyring, transport, purpose="node.build").reply(MESSAGES)

    body = transport.requests[-1]["body"]
    assert body["max_tokens"] == 16384
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 4096}
    # Thinking refuses a sampling temperature beside it.
    assert "temperature" not in body


def test_chat_stays_conversational_no_thinking_default_sampling(rig):
    keyring, transport = rig
    keyring.store("t1", "anthropic", "sk-ant-0123456789")
    transport.script("anthropic.com", 200, _anthropic_reply())

    _router(keyring, transport).reply(MESSAGES)  # purpose defaults to chat.turn

    body = transport.requests[-1]["body"]
    assert body["max_tokens"] == 4096
    assert "thinking" not in body
    assert "temperature" not in body


def test_a_pre_thinking_claude_gets_no_thinking_block(rig, monkeypatch):
    monkeypatch.setenv(
        "OOLU_CHAT_MODEL_ANTHROPIC_FAST", "claude-3-5-sonnet-20241022"
    )
    keyring, transport = rig
    keyring.store("t1", "anthropic", "sk-ant-0123456789")
    transport.script("anthropic.com", 200, _anthropic_reply())

    _router(keyring, transport, purpose="node.build").reply(MESSAGES)

    body = transport.requests[-1]["body"]
    assert body["model"] == "claude-3-5-sonnet-20241022"
    assert "thinking" not in body
    # With thinking off, the seat's temperature rides normally.
    assert body["temperature"] == 0.2


def test_openai_now_carries_ceiling_and_temperature(rig):
    keyring, transport = rig
    keyring.store("t1", "openai", "sk-openai-0123456789")
    transport.script("openai.com", 200, _openai_reply())

    _router(keyring, transport, purpose="node.build").reply(MESSAGES)

    body = transport.requests[-1]["body"]
    assert body["max_tokens"] == 16384
    assert body["temperature"] == 0.2
    assert "reasoning_effort" not in body  # gpt-4o-mini predates the dial


def test_an_openai_reasoning_model_takes_the_modern_knobs(rig, monkeypatch):
    monkeypatch.setenv("OOLU_CHAT_MODEL_OPENAI_FAST", "o3-mini")
    keyring, transport = rig
    keyring.store("t1", "openai", "sk-openai-0123456789")
    transport.script("openai.com", 200, _openai_reply())

    _router(keyring, transport, purpose="node.build").reply(MESSAGES)

    body = transport.requests[-1]["body"]
    assert body["model"] == "o3-mini"
    assert body["max_completion_tokens"] == 16384
    assert body["reasoning_effort"] == "medium"
    # Reasoning models refuse a sampling temperature.
    assert "temperature" not in body
    assert "max_tokens" not in body


def test_tool_consultations_ride_temperature_but_not_thinking_yet(rig):
    keyring, transport = rig
    keyring.store("t1", "anthropic", "sk-ant-0123456789")
    transport.script(
        "anthropic.com",
        200,
        {
            "model": "claude-sonnet-5",
            "content": [{"type": "text", "text": "done"}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    )
    tool = ToolSpec(
        name="finish_node",
        description="deliver",
        parameters={"type": "object", "properties": {}},
    )

    _router(keyring, transport, purpose="node.build").consult(
        MESSAGES, tools=[tool]
    )

    body = transport.requests[-1]["body"]
    assert body["max_tokens"] == 16384
    # The neutral transcript cannot carry thinking blocks back across
    # tool turns yet (plan Phase 2) — so no thinking, and temperature
    # rides in its place.
    assert "thinking" not in body
    assert body["temperature"] == 0.2


def test_an_explicit_constructor_ceiling_still_wins(rig):
    keyring, transport = rig
    keyring.store("t1", "anthropic", "sk-ant-0123456789")
    transport.script("anthropic.com", 200, _anthropic_reply())

    _router(keyring, transport, purpose="node.build", max_tokens=1024).reply(
        MESSAGES
    )

    body = transport.requests[-1]["body"]
    assert body["max_tokens"] == 1024
    # A 1024 ceiling cannot host a 4096 thought: the adapter's own
    # guard keeps the request valid instead of erroring at the wire.
    assert "thinking" not in body


# --------------------------------------------------------------------------- #
# The registry is configuration, not code                                      #
# --------------------------------------------------------------------------- #
def test_chat_models_are_env_overridable(monkeypatch):
    assert chat_model_for("anthropic", "reasoning") == "claude-sonnet-5"
    monkeypatch.setenv("OOLU_CHAT_MODEL_ANTHROPIC_REASONING", "claude-next")
    assert chat_model_for("anthropic", "reasoning") == "claude-next"
    # Unknown tiers fall back to fast; unknown providers stay empty.
    assert chat_model_for("openai", "dreaming") == "gpt-4o-mini"
    assert chat_model_for("nobody", "fast") == ""


# --------------------------------------------------------------------------- #
# Retries wait now                                                             #
# --------------------------------------------------------------------------- #
class FlakyTransport(FakeTransport):
    """First answer 429, then success — the retry-with-backoff shape."""

    def __init__(self, then):
        super().__init__()
        self._then = then
        self._failed = False

    def request(self, method, url, *, headers=None, body=None, timeout=30.0):
        self.requests.append({"url": url, "body": body})
        if not self._failed:
            self._failed = True
            return ProviderResponse(status=429, json={"error": "slow down"})
        return ProviderResponse(status=200, json=self._then)


def test_the_provider_backoff_is_real_and_seamed(rig, monkeypatch):
    waited: list[float] = []
    monkeypatch.setattr(
        "oolu.providers.base._default_backoff", waited.append
    )
    keyring, _ = rig
    keyring.store("t1", "anthropic", "sk-ant-0123456789")
    transport = FlakyTransport(_anthropic_reply("recovered"))

    text = _router(keyring, transport).reply(MESSAGES)

    assert text == "recovered"
    assert waited == [1.0]  # one 429, one real wait, then the answer


class _RateLimitError(Exception):
    pass


def test_the_synthesis_gateway_retries_transient_failures():
    calls: list[int] = []
    waited: list[float] = []

    def completion(messages, **kwargs):
        calls.append(1)
        if len(calls) < 3:
            raise _RateLimitError("busy")
        from types import SimpleNamespace

        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="```python\nx = 1\n```"),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=3, completion_tokens=2, total_tokens=5
            ),
        )

    gateway = LiteLLMGateway(completion_fn=completion, sleep=waited.append)
    decision = RoutingDecision(
        tier=ModelTier.FAST,
        model="openai/test-model",
        api_base=None,
        temperature=0.1,
        top_p=0.8,
        top_k=None,
        max_tokens=64,
    )
    prompt = AssembledPrompt(
        messages=[{"role": "user", "content": "write"}], prefix_len=1
    )

    result = gateway.complete(decision, prompt)

    assert result.script == "x = 1"
    assert len(calls) == 3
    assert waited == [1.0, 2.0]


def test_a_persistent_failure_still_surfaces_as_gateway_error():
    def completion(messages, **kwargs):
        raise _RateLimitError("forever busy")

    gateway = LiteLLMGateway(completion_fn=completion, sleep=lambda _s: None)
    decision = RoutingDecision(
        tier=ModelTier.FAST,
        model="openai/test-model",
        api_base=None,
        temperature=0.1,
        top_p=0.8,
        top_k=None,
        max_tokens=64,
    )
    prompt = AssembledPrompt(
        messages=[{"role": "user", "content": "write"}], prefix_len=1
    )

    with pytest.raises(GatewayError):
        gateway.complete(decision, prompt)


def test_a_non_transient_failure_never_retries():
    calls: list[int] = []

    def completion(messages, **kwargs):
        calls.append(1)
        raise ValueError("bad request shape")

    gateway = LiteLLMGateway(completion_fn=completion, sleep=lambda _s: None)
    decision = RoutingDecision(
        tier=ModelTier.FAST,
        model="openai/test-model",
        api_base=None,
        temperature=0.1,
        top_p=0.8,
        top_k=None,
        max_tokens=64,
    )
    prompt = AssembledPrompt(
        messages=[{"role": "user", "content": "write"}], prefix_len=1
    )

    with pytest.raises(GatewayError):
        gateway.complete(decision, prompt)
    assert len(calls) == 1
