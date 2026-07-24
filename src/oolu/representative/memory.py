"""What the user has actually said: fuzzy recall over remembered exchanges.

The port allows a real embedding index later; the default is deliberately
dependency-free. Ranking now rides the ONE retrieval scorer every recall
site shares (``oolu.retrieval``, context-harness plan Phase 5 — words
plus character trigrams, so "invoices" recalls "invoice"), behind the
same Embedder seam a model-backed index replaces for all consumers at
once. The silence gate stays local and stricter: an exchange that shares
no normalized WORDS with the message is never a hit, whatever the
trigrams think. A person's message history is human-sized; this stays
fast well past the point where an adapter is worth training.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..replies.engine import normalize_message
from .models import RecallHit
from .store import RepresentativeStore


@runtime_checkable
class ExchangeMemory(Protocol):
    """Port for whatever recalls how the user replies to things like this."""

    def remember(
        self, scope: str, *, key: str, prompt: str, reply: str, peer: str = ""
    ) -> None: ...

    def recall(
        self, scope: str, text: str, *, k: int = 4, peer: str | None = None
    ) -> list[RecallHit]: ...

    def count(self, scope: str) -> int: ...


# A similar exchange with the SAME person outranks a similar one with
# someone else: register (who you're talking to) shapes the reply as much
# as topic does. Cross-peer memories still count, just discounted.
CROSS_PEER_DISCOUNT = 0.75


def _tokens(text: str) -> frozenset[str]:
    return frozenset(normalize_message(text).split())


class StoreExchangeMemory:
    """Token-overlap recall over the representative store's exchanges."""

    def __init__(self, store: RepresentativeStore, *, candidate_limit: int = 2000):
        self._store = store
        self._candidate_limit = candidate_limit

    def remember(
        self, scope: str, *, key: str, prompt: str, reply: str, peer: str = ""
    ) -> None:
        self._store.remember_exchange(
            scope, key=key, prompt=prompt, reply=reply, peer=peer
        )

    def recall(
        self, scope: str, text: str, *, k: int = 4, peer: str | None = None
    ) -> list[RecallHit]:
        from ..retrieval import score as retrieval_score

        query = _tokens(text)
        if not query:
            return []
        normalized_query = normalize_message(text)
        hits: list[RecallHit] = []
        for row in self._store.exchanges(scope, limit=self._candidate_limit):
            document = _tokens(row["prompt_text"])
            if not document:
                continue
            shared = len(query & document)
            if shared == 0:
                # The silence gate: no shared words, no memory — however
                # the trigrams lean.
                continue
            # The shared scorer ranks: words plus character trigrams,
            # symmetric, and long messages don't win by mentioning
            # everything.
            score = retrieval_score(
                normalized_query, normalize_message(row["prompt_text"])
            )
            row_peer = str(row["peer"] or "")
            if peer and row_peer != peer:
                score *= CROSS_PEER_DISCOUNT
            hits.append(
                RecallHit(
                    prompt_text=row["prompt_text"],
                    reply_text=row["reply_text"],
                    score=round(score, 4),
                    peer=row_peer,
                )
            )
        # Candidates arrive newest first; the stable sort keeps recency as
        # the tie-break within equal scores.
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:k]

    def count(self, scope: str) -> int:
        return self._store.exchange_count(scope)
