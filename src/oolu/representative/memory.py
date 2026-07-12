"""What the user has actually said: fuzzy recall over remembered exchanges.

The port allows a real embedding index later; the default is deliberately
dependency-free — normalized token-set cosine over the store's most recent
exchanges, scored in Python. A person's message history is human-sized;
this stays fast well past the point where an adapter is worth training.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..replies.engine import normalize_message
from .models import RecallHit
from .store import RepresentativeStore


@runtime_checkable
class ExchangeMemory(Protocol):
    """Port for whatever recalls how the user replies to things like this."""

    def remember(self, scope: str, *, key: str, prompt: str, reply: str) -> None: ...

    def recall(self, scope: str, text: str, *, k: int = 4) -> list[RecallHit]: ...

    def count(self, scope: str) -> int: ...


def _tokens(text: str) -> frozenset[str]:
    return frozenset(normalize_message(text).split())


class StoreExchangeMemory:
    """Token-overlap recall over the representative store's exchanges."""

    def __init__(self, store: RepresentativeStore, *, candidate_limit: int = 2000):
        self._store = store
        self._candidate_limit = candidate_limit

    def remember(self, scope: str, *, key: str, prompt: str, reply: str) -> None:
        self._store.remember_exchange(scope, key=key, prompt=prompt, reply=reply)

    def recall(self, scope: str, text: str, *, k: int = 4) -> list[RecallHit]:
        query = _tokens(text)
        if not query:
            return []
        hits: list[RecallHit] = []
        for row in self._store.exchanges(scope, limit=self._candidate_limit):
            document = _tokens(row["prompt_text"])
            if not document:
                continue
            shared = len(query & document)
            if shared == 0:
                continue
            # Cosine over binary token vectors: symmetric, and long
            # messages don't win just by mentioning everything.
            score = shared / (len(query) * len(document)) ** 0.5
            hits.append(
                RecallHit(
                    prompt_text=row["prompt_text"],
                    reply_text=row["reply_text"],
                    score=round(score, 4),
                )
            )
        # Candidates arrive newest first; the stable sort keeps recency as
        # the tie-break within equal scores.
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:k]

    def count(self, scope: str) -> int:
        return self._store.exchange_count(scope)
