"""The chat brain: routing, fallback, metering, budget, degradation.

A fake transport plays both providers, so every path is exercised offline:
the provider wire shapes, preference order, failover, the spending cap's
polite refusal, and — through ``ChatAssistant`` — the promise that a dead
model degrades to the model-less path instead of killing the conversation.
"""

from __future__ import annotations

import pytest

from oolu.billing import ModelCallMeter
from oolu.chat import ChatAssistant, ModelBudgetExceeded, ModelUnavailable
from oolu.durable.connection import DurableConnection
from oolu.providers.base import ProviderResponse
from oolu.providers.chatmodel import ChatModelRouter
from oolu.providers.keyring import ModelKeyring


def _anthropic_reply(text):
    return {
        "model": "claude-sonnet-5",
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 40, "output_tokens": 12},
    }


def _openai_reply(text):
    return {
        "model": "gpt-4o-mini",
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 40, "completion_tokens": 12},
    }


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


def _router(keyring, transport, meter, **kwargs):
    return ChatModelRouter(
        keyring, "t1", transport=transport, meter=meter, **kwargs
    )


MESSAGES = [
    {"role": "system", "content": "You are OoLu."},
    {"role": "user", "content": "hello"},
]


def test_anthropic_answers_and_the_system_prompt_rides_apart(rig):
    keyring, transport, meter = rig
    keyring.store("t1", "anthropic", "sk-ant-0123456789")
    transport.script("anthropic.com", 200, _anthropic_reply("Hi there."))

    text = _router(keyring, transport, meter).reply(MESSAGES)

    assert text == "Hi there."
    call = transport.requests[-1]
    assert "/messages" in call["url"]
    # Anthropic's wire shape: system as a parameter, never a message role —
    # carried as a block with the prompt-cache breakpoint on it, so the
    # frozen prefix stops being re-paid every turn.
    assert call["body"]["system"] == [
        {
            "type": "text",
            "text": "You are OoLu.",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    assert all(m["role"] != "system" for m in call["body"]["messages"])
    # The key rode the right header, and only there.
    assert call["headers"]["x-api-key"] == "sk-ant-0123456789"


def test_openai_answers_on_its_own_wire_shape(rig):
    keyring, transport, meter = rig
    keyring.store("t1", "openai", "sk-openai-0123456789")
    transport.script("openai.com", 200, _openai_reply("Hello!"))

    text = _router(keyring, transport, meter).reply(MESSAGES)

    assert text == "Hello!"
    call = transport.requests[-1]
    assert "/chat/completions" in call["url"]
    assert call["headers"]["Authorization"] == "Bearer sk-openai-0123456789"


def test_every_consultation_enters_the_books(rig):
    keyring, transport, meter = rig
    keyring.store("t1", "anthropic", "sk-ant-0123456789")
    transport.script("anthropic.com", 200, _anthropic_reply("Hi."))

    _router(keyring, transport, meter).reply(MESSAGES)

    (record,) = meter.charges("chat.turn")
    assert record.prompt_tokens == 40
    assert record.completion_tokens == 12
    assert record.cost > 0  # never quietly free


def test_failover_to_the_next_configured_provider(rig):
    keyring, transport, meter = rig
    keyring.store("t1", "anthropic", "sk-ant-0123456789")
    keyring.store("t1", "openai", "sk-openai-0123456789")
    transport.script("anthropic.com", 500, {"error": "down"})
    transport.script("openai.com", 200, _openai_reply("Backup here."))

    text = _router(keyring, transport, meter).reply(MESSAGES)
    assert text == "Backup here."


def test_preference_reorders_the_line_under_own_api(rig):
    keyring, transport, meter = rig
    keyring.store("t1", "anthropic", "sk-ant-0123456789")
    keyring.store("t1", "openai", "sk-openai-0123456789")
    transport.script("anthropic.com", 200, _anthropic_reply("A"))
    transport.script("openai.com", 200, _openai_reply("O"))

    # The provider preference is the own-api dial; under the default
    # subscription source the plan's order rules (tested further down).
    router = _router(
        keyring, transport, meter,
        source=lambda: "own-api",
        preference=lambda: "openai",
    )
    assert router.reply(MESSAGES) == "O"


def test_all_dead_raises_unavailable_without_leaking_the_key(rig):
    keyring, transport, meter = rig
    keyring.store("t1", "anthropic", "sk-ant-0123456789")
    transport.script("anthropic.com", 500, {"error": "down"})

    with pytest.raises(ModelUnavailable) as excinfo:
        _router(keyring, transport, meter).reply(MESSAGES)
    assert "sk-ant-0123456789" not in str(excinfo.value)


def test_no_key_raises_unavailable(rig):
    keyring, transport, meter = rig
    with pytest.raises(ModelUnavailable):
        _router(keyring, transport, meter).reply(MESSAGES)


def test_the_spending_cap_refuses_in_words(rig):
    keyring, transport, meter = rig
    keyring.store("t1", "anthropic", "sk-ant-0123456789")
    transport.script("anthropic.com", 200, _anthropic_reply("Hi."))
    router = _router(keyring, transport, meter, budget=lambda: 0.000001)

    router.reply(MESSAGES)  # first turn spends past the tiny cap
    with pytest.raises(ModelBudgetExceeded) as excinfo:
        router.reply(MESSAGES)
    assert "spending cap" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Where the default brain lives: subscription / own-api / local.               #
# --------------------------------------------------------------------------- #
def test_subscription_stays_claude_first_whatever_the_preference(rig):
    keyring, transport, meter = rig
    keyring.store("t1", "anthropic", "sk-ant-0123456789")
    keyring.store("t1", "openai", "sk-openai-0123456789")
    transport.script("anthropic.com", 200, _anthropic_reply("A"))
    transport.script("openai.com", 200, _openai_reply("O"))

    router = _router(
        keyring, transport, meter,
        source=lambda: "subscription",
        preference=lambda: "openai",
    )
    # The plan's brain is Claude first; a key preference doesn't reorder it.
    assert router.reply(MESSAGES) == "A"


def test_own_api_lets_the_users_key_override_the_plan(rig):
    keyring, transport, meter = rig
    keyring.store("t1", "anthropic", "sk-ant-0123456789")
    keyring.store("t1", "openai", "sk-openai-0123456789")
    transport.script("anthropic.com", 200, _anthropic_reply("A"))
    transport.script("openai.com", 200, _openai_reply("O"))

    router = _router(
        keyring, transport, meter,
        source=lambda: "own-api",
        preference=lambda: "openai",
    )
    assert router.reply(MESSAGES) == "O"


def test_local_asks_the_machine_with_no_key_at_all(rig):
    keyring, transport, meter = rig  # note: the keyring stays EMPTY
    transport.script(
        "127.0.0.1:11434",
        200,
        {
            "model": "llama3.2",
            "choices": [{"message": {"role": "assistant", "content": "Hi."}}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 3},
        },
    )
    router = _router(
        keyring, transport, meter,
        source=lambda: "local",
        local_url=lambda: "http://127.0.0.1:11434/v1",
        local_model=lambda: "llama3.2",
    )

    assert router.reply(MESSAGES) == "Hi."
    call = transport.requests[-1]
    assert call["url"].startswith("http://127.0.0.1:11434/v1")
    assert call["body"]["model"] == "llama3.2"
    # And it still enters the books, tagged as the machine's own tier.
    (record,) = meter.charges("chat.turn")
    assert record.tier == "local"


def test_local_never_falls_back_into_the_cloud(rig):
    keyring, transport, meter = rig
    keyring.store("t1", "anthropic", "sk-ant-0123456789")
    transport.script("127.0.0.1:11434", 500, {"error": "dead"})
    transport.script("anthropic.com", 200, _anthropic_reply("A"))

    router = _router(
        keyring, transport, meter,
        source=lambda: "local",
        local_url=lambda: "http://127.0.0.1:11434/v1",
        local_model=lambda: "llama3.2",
    )
    # Choosing local means local: a dead server degrades, it doesn't
    # quietly phone a provider the user pointed away from.
    with pytest.raises(ModelUnavailable):
        router.reply(MESSAGES)
    assert all("anthropic" not in r["url"] for r in transport.requests)


def test_local_unconfigured_says_what_to_set(rig):
    keyring, transport, meter = rig
    router = _router(
        keyring, transport, meter,
        source=lambda: "local",
        local_model=lambda: "",
    )
    with pytest.raises(ModelUnavailable, match="Settings"):
        router.reply(MESSAGES)


# --------------------------------------------------------------------------- #
# Through the assistant: degradation is the contract.                          #
# --------------------------------------------------------------------------- #
class _DeadModel:
    def reply(self, messages):
        raise ModelUnavailable("network is gone")


class _CappedModel:
    def reply(self, messages):
        raise ModelBudgetExceeded("I've reached the model spending cap you set.")


def test_a_dead_model_degrades_to_the_intent_path():
    turn = ChatAssistant().respond("convert the report", model=_DeadModel())
    assert turn.source == "intent"
    assert turn.task == "convert the report"


def test_a_reached_cap_is_said_out_loud_not_skipped():
    turn = ChatAssistant().respond("convert the report", model=_CappedModel())
    assert turn.source == "model"
    assert "spending cap" in turn.say
    assert turn.task is None
