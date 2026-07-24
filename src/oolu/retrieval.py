"""One retrieval scorer for every recall — Phase 5 of the context plan.

Three recall sites grew three notions of "similar": the context pack's
token-overlap cosine (authoring examples), the representative's binary
token cosine (voice recall), and exact-sentence matching everywhere
else. This module is the ONE scorer they now share, and the seam a
model-backed embedding index replaces without the consumers noticing:

- ``LexicalEmbedder`` turns text into a sparse vector of WORDS
  (sublinear term frequency — a long text doesn't win by repeating
  itself) blended with CHARACTER TRIGRAMS at lower weight — the part
  plain token overlap never had: "normalizing invoices" now resembles
  "normalize invoice csv" through their shared character shapes, no
  stemmer, no model, no dependency, fully deterministic.
- ``cosine`` over those vectors; ``score(a, b)`` is the one-call form.
- ``Embedder`` is the protocol: anything with ``embed(text) -> dict``
  slots in. A real embedding model implements it and every consumer —
  authoring examples, representative recall, the bench — upgrades at
  once, which is the whole point of one scorer.

Deliberately NOT here: persistence and ranking policy. Stores keep
their own rows and their own gates (the representative still refuses a
hit that shares no words — recall silence is a feature); this module
only answers "how alike are these two texts."
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Protocol, runtime_checkable

_WORD_RE = re.compile(r"[a-z0-9]+")

# Trigrams complement words, never dominate them: shared character
# shapes should break ties and catch morphology, not manufacture
# similarity between unrelated sentences out of "the" and "ing".
_TRIGRAM_WEIGHT = 0.35


@runtime_checkable
class Embedder(Protocol):
    """Anything that turns text into a sparse {feature: weight} vector."""

    def embed(self, text: str) -> dict[str, float]: ...


class LexicalEmbedder:
    """The dependency-free default: words plus character trigrams."""

    def embed(self, text: str) -> dict[str, float]:
        words = _WORD_RE.findall(str(text or "").lower())
        vector: dict[str, float] = {}
        for word, count in Counter(words).items():
            vector[f"w:{word}"] = 1.0 + math.log(count)
        grams: Counter = Counter()
        for word in set(words):
            padded = f" {word} "
            for i in range(len(padded) - 2):
                grams[padded[i : i + 3]] += 1
        for gram, count in grams.items():
            vector[f"g:{gram}"] = _TRIGRAM_WEIGHT * (1.0 + math.log(count))
        return vector


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    if len(b) < len(a):
        a, b = b, a
    dot = sum(weight * b.get(feature, 0.0) for feature, weight in a.items())
    if dot == 0.0:
        return 0.0
    norm_a = math.sqrt(sum(w * w for w in a.values()))
    norm_b = math.sqrt(sum(w * w for w in b.values()))
    return dot / (norm_a * norm_b)


_DEFAULT = LexicalEmbedder()


def score(a: str, b: str, *, embedder: Embedder | None = None) -> float:
    """How alike two texts are, 0.0 to 1.0 — the one scorer every
    recall site shares. Pass a model-backed ``embedder`` to upgrade a
    single call site; replace ``LexicalEmbedder`` to upgrade them all."""
    active = embedder or _DEFAULT
    return cosine(active.embed(a), active.embed(b))


def shares_words(a: str, b: str) -> bool:
    """The silence gate recall sites keep: no shared WORDS means no hit,
    whatever the trigrams think — 'the' and 'ing' are not memories."""
    return bool(
        set(_WORD_RE.findall(str(a or "").lower()))
        & set(_WORD_RE.findall(str(b or "").lower()))
    )
