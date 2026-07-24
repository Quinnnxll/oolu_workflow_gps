"""Model-backed embeddings behind the retrieval seam — the seam, filled.

Phase 5 left one half of the recall upgrade open on purpose: every
recall site shares one scorer (``oolu.retrieval``) behind the
``Embedder`` protocol, and the lexical default was the floor a real
embedding model would raise. This module is that model integration:

- :class:`ModelEmbedder` implements the protocol over any
  ``embed_fn(text) -> list[float]``, mapping the dense vector into the
  sparse-dict shape ``cosine`` already speaks. Two disciplines are
  structural:

  * **Fail-open, never fail-loud.** Recall is advisory context; a dead
    embedding endpoint must degrade ranking quality, not break a build.
    Any failure falls back to the lexical embedder for that text — and
    after ``max_failures`` consecutive failures the embedder stops
    calling out entirely (a dead endpoint should not tax every compile
    with a timeout).
  * **Cached, because compiles repeat.** Texts are embedded once per
    process (bounded LRU); the same node card ranked against ten goals
    costs one call, not ten.

- :func:`openai_embedding_fn` speaks the OpenAI ``/embeddings`` wire —
  which is also the LOCAL wire: Ollama and LM Studio serve the same
  shape, so one function covers the hosted key and the machine's own
  model; only the adapter's base URL differs. Anthropic publishes no
  embeddings API, so there is deliberately no anthropic variant.

The calls ride the SAME authenticated adapter pipeline every provider
request rides (vault-held key, retries, budget walls) — embeddings are
provider requests, not a side door.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from typing import Callable

from ..retrieval import Embedder, LexicalEmbedder

# The hosted default; local servers name their own (nomic-embed-text,
# mxbai-embed-large, ...) via OOLU_EMBEDDING_MODEL.
DEFAULT_OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"


def openai_embedding_fn(
    adapter, *, model: str = DEFAULT_OPENAI_EMBEDDING_MODEL
) -> Callable[[str], list[float]]:
    """One text → one dense vector over the OpenAI-shaped ``/embeddings``
    wire, through the given authenticated adapter (hosted OpenAI or any
    local OpenAI-compatible server)."""

    def embed(text: str) -> list[float]:
        data = adapter.invoke("/embeddings", {"model": model, "input": [text]})
        return [float(v) for v in data["data"][0]["embedding"]]

    return embed


class ModelEmbedder:
    """The :class:`~oolu.retrieval.Embedder` a real model fills.

    Wraps ``embed_fn`` with an LRU cache and the fail-open discipline;
    plug it into ``retrieval.score(..., embedder=...)`` or a
    ``ContextPackCompiler`` and every ranking it serves upgrades — the
    consumers never learn which side of the seam answered."""

    def __init__(
        self,
        embed_fn: Callable[[str], list[float]],
        *,
        cache_size: int = 2048,
        fallback: Embedder | None = None,
        max_failures: int = 3,
    ) -> None:
        self._embed_fn = embed_fn
        self._cache: OrderedDict[str, dict[str, float]] = OrderedDict()
        self._cache_size = max(1, int(cache_size))
        self._fallback = fallback or LexicalEmbedder()
        self._max_failures = max(1, int(max_failures))
        self._consecutive_failures = 0

    @property
    def gave_up(self) -> bool:
        """True once consecutive failures crossed the ceiling and the
        embedder stopped calling out — recall is lexical from there."""
        return self._consecutive_failures >= self._max_failures

    def embed(self, text: str) -> dict[str, float]:
        raw = str(text or "")
        if not raw:
            return {}
        key = hashlib.sha1(raw.encode("utf-8")).hexdigest()
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        if self.gave_up:
            return self._fallback.embed(raw)
        try:
            vector = self._embed_fn(raw)
        except Exception:  # noqa: BLE001 - recall degrades, builds proceed
            self._consecutive_failures += 1
            return self._fallback.embed(raw)
        self._consecutive_failures = 0
        sparse = {f"d{i}": float(v) for i, v in enumerate(vector) if v}
        self._cache[key] = sparse
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return sparse
