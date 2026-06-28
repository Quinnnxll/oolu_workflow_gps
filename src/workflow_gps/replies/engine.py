"""Model-free reply routing with an explicit optional fallback port."""

from __future__ import annotations

import re
import unicodedata
from typing import Protocol, runtime_checkable

from .models import MessageEnvelope, ReplyDecision, ReplyRule


def normalize_message(text: str) -> str:
    """Normalize casing, Unicode forms, punctuation, and whitespace."""
    text = unicodedata.normalize("NFKC", text).casefold()
    text = "".join(
        " " if unicodedata.category(char).startswith("P") else char for char in text
    )
    return re.sub(r"\s+", " ", text).strip()


@runtime_checkable
class ReplyFallback(Protocol):
    """Port for a future model or human-review fallback."""

    def reply(
        self, message: MessageEnvelope, context: dict[str, str]
    ) -> str | None: ...


class NoopReplyFallback:
    def reply(self, message: MessageEnvelope, context: dict[str, str]) -> str | None:
        return None


class DeterministicReplyEngine:
    def __init__(self, rules: list[ReplyRule], fallback: ReplyFallback | None = None):
        self._rules = list(rules)
        self._fallback = fallback or NoopReplyFallback()

    def decide(
        self, message: MessageEnvelope, context: dict[str, str]
    ) -> ReplyDecision:
        normalized = normalize_message(message.text)
        for rule in self._rules:
            if self._matches(rule, message.channel, normalized, context):
                try:
                    reply = rule.reply.format_map(context)
                except KeyError:
                    continue
                return ReplyDecision(text=reply, source="rule", rule_id=rule.id)

        fallback_text = self._fallback.reply(message, context)
        if fallback_text:
            return ReplyDecision(text=fallback_text, source="fallback")
        return ReplyDecision()

    @staticmethod
    def _matches(
        rule: ReplyRule, channel: str, normalized: str, context: dict[str, str]
    ) -> bool:
        if rule.channels and channel not in rule.channels:
            return False
        if any(context.get(key) != value for key, value in rule.requires.items()):
            return False
        phrases = [normalize_message(phrase) for phrase in rule.phrases]
        if rule.match == "contains":
            return any(phrase and phrase in normalized for phrase in phrases)
        return normalized in phrases
