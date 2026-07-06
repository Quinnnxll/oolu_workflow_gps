"""Offline tests for deterministic replies and the Telegram adapter."""

from __future__ import annotations

import io
import urllib.error

import pytest

from oolu.replies import (
    DeterministicReplyEngine,
    FileOffsetStore,
    LocalLearnedReplyStore,
    MessageEnvelope,
    ReplyBot,
    ReplyRule,
)
from oolu.replies.channels import (
    ChannelError,
    PollBatch,
    TelegramAdapter,
    TelegramBotApiTransport,
)


def _message(text: str) -> MessageEnvelope:
    return MessageEnvelope(
        channel="telegram",
        conversation_id="100",
        sender_id="200",
        text=text,
    )


class CountingFallback:
    def __init__(self):
        self.calls = 0

    def reply(self, message, context):
        self.calls += 1
        return "fallback"


def test_arrival_rule_skips_fallback_model_port():
    fallback = CountingFallback()
    engine = DeterministicReplyEngine(
        [
            ReplyRule(
                id="arrived",
                phrases=["Have you arrived?"],
                reply="I have arrived at {location}.",
                requires={"driver_status": "arrived"},
            )
        ],
        fallback,
    )

    decision = engine.decide(
        _message("  HAVE you arrived?! "),
        {"driver_status": "arrived", "location": "the pickup point"},
    )

    assert decision.text == "I have arrived at the pickup point."
    assert decision.source == "rule" and fallback.calls == 0


def test_rule_requires_truthful_context_before_replying():
    fallback = CountingFallback()
    engine = DeterministicReplyEngine(
        [
            ReplyRule(
                id="arrived",
                phrases=["Have you arrived?"],
                reply="I have arrived.",
                requires={"driver_status": "arrived"},
            )
        ],
        fallback,
    )

    decision = engine.decide(
        _message("Have you arrived?"), {"driver_status": "driving"}
    )

    assert decision.source == "fallback" and fallback.calls == 1


class FakeTelegramTransport:
    def __init__(self, updates):
        self.updates = updates
        self.calls = []

    def call(self, method, payload):
        self.calls.append((method, payload))
        if method == "getUpdates":
            return {"ok": True, "result": self.updates}
        return {"ok": True, "result": {}}


def test_telegram_adapter_accepts_only_private_text_messages():
    transport = FakeTelegramTransport(
        [
            {
                "update_id": 10,
                "message": {
                    "message_id": 3,
                    "text": "Have you arrived?",
                    "chat": {"id": 100, "type": "private"},
                    "from": {"id": 200},
                },
            },
            {
                "update_id": 11,
                "message": {
                    "message_id": 4,
                    "text": "group message",
                    "chat": {"id": -1, "type": "group"},
                },
            },
        ]
    )
    adapter = TelegramAdapter(transport)

    batch = adapter.poll(timeout_s=1)
    adapter.send(batch.messages[0], "I have arrived.")

    assert len(batch.messages) == 1 and batch.next_offset == 12
    assert batch.messages[0].conversation_id == "100"
    assert transport.calls[-1] == (
        "sendMessage",
        {"chat_id": "100", "text": "I have arrived."},
    )


class FakeAdapter:
    name = "telegram"

    def __init__(self):
        self.sent = []

    def poll(self, *, offset=None, timeout_s=25):
        return PollBatch(messages=(_message("Are you here?"),), next_offset=7)

    def send(self, message, text):
        self.sent.append((message.conversation_id, text))


def test_reply_bot_sends_deterministic_decision():
    adapter = FakeAdapter()
    engine = DeterministicReplyEngine(
        [ReplyRule(id="here", phrases=["Are you here?"], reply="I have arrived.")]
    )
    stats, offset = ReplyBot(adapter, engine).run_once()

    assert adapter.sent == [("100", "I have arrived.")]
    assert stats.sent == 1 and stats.rule_replies == 1 and offset == 7


def test_telegram_business_reply_preserves_connection_id():
    transport = FakeTelegramTransport(
        [
            {
                "update_id": 20,
                "business_message": {
                    "message_id": 8,
                    "text": "Are you here?",
                    "business_connection_id": "connection-1",
                    "chat": {"id": 300, "type": "private"},
                    "from": {"id": 400},
                },
            }
        ]
    )
    adapter = TelegramAdapter(transport)

    message = adapter.poll().messages[0]
    adapter.send(message, "I have arrived.")

    assert transport.calls[-1] == (
        "sendMessage",
        {
            "chat_id": "300",
            "text": "I have arrived.",
            "business_connection_id": "connection-1",
        },
    )


def test_telegram_business_message_direction_is_account_aware():
    transport = FakeTelegramTransport(
        [
            {
                "update_id": 30,
                "business_connection": {
                    "id": "connection-1",
                    "user": {"id": 900},
                    "is_enabled": True,
                },
            },
            {
                "update_id": 31,
                "business_message": {
                    "message_id": 9,
                    "text": "Have you arrived?",
                    "business_connection_id": "connection-1",
                    "chat": {"id": 400, "type": "private"},
                    "from": {"id": 400},
                },
            },
            {
                "update_id": 32,
                "business_message": {
                    "message_id": 10,
                    "text": "I have arrived.",
                    "business_connection_id": "connection-1",
                    "chat": {"id": 400, "type": "private"},
                    "from": {"id": 900},
                },
            },
        ]
    )

    messages = TelegramAdapter(transport).poll().messages

    assert [message.direction for message in messages] == ["inbound", "outbound"]


class SequencedAdapter:
    name = "telegram"

    def __init__(self, batches):
        self.batches = list(batches)
        self.sent = []

    def poll(self, *, offset=None, timeout_s=25):
        return PollBatch(messages=tuple(self.batches.pop(0)), next_offset=offset)

    def send(self, message, text):
        self.sent.append((message.conversation_id, text))


def test_manual_reply_is_learned_then_reused_without_fallback():
    class SilentFallback:
        def __init__(self):
            self.calls = 0

        def reply(self, message, context):
            self.calls += 1
            return None

    metadata = {"reply_scope": "telegram-business:one"}
    inbound = MessageEnvelope(
        channel="telegram",
        conversation_id="customer",
        sender_id="customer",
        text="Have you arrived?",
        metadata=metadata,
    )
    outbound = MessageEnvelope(
        channel="telegram",
        conversation_id="customer",
        sender_id="owner",
        text="I have arrived.",
        direction="outbound",
        metadata=metadata,
    )
    repeated = inbound.model_copy(update={"text": " HAVE you arrived?! "})
    adapter = SequencedAdapter([[inbound], [outbound], [repeated]])
    memory = LocalLearnedReplyStore(":memory:")
    fallback = SilentFallback()
    bot = ReplyBot(
        adapter,
        DeterministicReplyEngine([], fallback),
        learned=memory,
    )

    first, _ = bot.run_once()
    taught, _ = bot.run_once()
    reused, _ = bot.run_once()

    assert first.sent == 0
    assert taught.learned_pairs == 1
    assert reused.learned_replies == 1
    assert adapter.sent[-1] == ("customer", "I have arrived.")
    assert fallback.calls == 1
    memory.close()


def test_file_offset_store_is_scoped_to_bot_identity(tmp_path):
    path = tmp_path / "offset.json"
    first = FileOffsetStore(path, identity="telegram:first")
    first.save(42)

    assert first.load() == 42
    assert FileOffsetStore(path, identity="telegram:other").load() is None


def test_telegram_http_error_keeps_description_but_not_token(monkeypatch):
    token = "123456789:secret_token_value_123456789"
    error = urllib.error.HTTPError(
        url="https://example.invalid",
        code=401,
        msg="Unauthorized",
        hdrs=None,
        fp=io.BytesIO(b'{"ok":false,"description":"Unauthorized"}'),
    )

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr("urllib.request.urlopen", fail)
    with pytest.raises(ChannelError) as caught:
        TelegramBotApiTransport(token).call("getUpdates", {})

    assert "HTTP 401" in str(caught.value) and "Unauthorized" in str(caught.value)
    assert token not in str(caught.value)


def test_telegram_rejects_command_text_as_token():
    with pytest.raises(ValueError, match="paste only"):
        TelegramBotApiTransport("py -m oolu.cli telegram")


def test_explicit_teaching_works_for_plain_bot_scope():
    memory = LocalLearnedReplyStore(":memory:")
    memory.teach(
        scope="telegram-bot",
        prompt="Where are you?",
        reply="I am at the pickup point.",
    )
    message = _message(" WHERE are you?! ").model_copy(
        update={"metadata": {"reply_scope": "telegram-bot"}}
    )

    assert memory.lookup(message) == "I am at the pickup point."
    memory.close()
