"""Connect channel adapters to the deterministic reply engine."""

from __future__ import annotations

from dataclasses import dataclass

from .channels import ChannelAdapter
from .engine import DeterministicReplyEngine
from .learned import LearnedReplyStore, NoopLearnedReplyStore
from .models import ReplyDecision


@dataclass(frozen=True, slots=True)
class ReplyRunStats:
    received: int = 0
    sent: int = 0
    rule_replies: int = 0
    fallback_replies: int = 0
    learned_replies: int = 0
    learned_pairs: int = 0


class ReplyBot:
    def __init__(
        self,
        adapter: ChannelAdapter,
        engine: DeterministicReplyEngine,
        context: dict[str, str] | None = None,
        learned: LearnedReplyStore | None = None,
    ):
        self._adapter = adapter
        self._engine = engine
        self._context = dict(context or {})
        self._learned = learned or NoopLearnedReplyStore()

    def run_once(
        self, *, offset: int | None = None, timeout_s: int = 25
    ) -> tuple[ReplyRunStats, int | None]:
        batch = self._adapter.poll(offset=offset, timeout_s=timeout_s)
        sent = rule_replies = fallback_replies = learned_replies = learned_pairs = 0
        for message in batch.messages:
            if message.direction == "outbound":
                learned_pairs += self._learned.learn_from_outbound(message)
                continue
            learned_text = self._learned.lookup(message)
            decision = (
                ReplyDecision(text=learned_text, source="learned")
                if learned_text
                else self._engine.decide(message, self._context)
            )
            if decision.text is None:
                self._learned.remember_unanswered(message)
                continue
            self._adapter.send(message, decision.text)
            sent += 1
            rule_replies += decision.source == "rule"
            fallback_replies += decision.source == "fallback"
            learned_replies += decision.source == "learned"
        return (
            ReplyRunStats(
                received=len(batch.messages),
                sent=sent,
                rule_replies=rule_replies,
                fallback_replies=fallback_replies,
                learned_replies=learned_replies,
                learned_pairs=learned_pairs,
            ),
            batch.next_offset,
        )
