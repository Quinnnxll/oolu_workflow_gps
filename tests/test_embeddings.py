"""The embedding-model integration — the retrieval seam, filled.

``ModelEmbedder`` implements the Embedder protocol over any dense
``embed_fn``: cached (compiles repeat), fail-open (recall degrades,
builds proceed), and self-silencing after consecutive failures (a dead
endpoint must not tax every compile). The OpenAI-shaped ``/embeddings``
wire rides the same authenticated adapter pipeline as every provider
call, and the compiler's ranking visibly obeys an injected embedder.
"""

from __future__ import annotations

import pytest

from oolu.contextpack import ContextPackCompiler
from oolu.providers.base import ProviderResponse
from oolu.providers.apikey import OpenAiAdapter
from oolu.providers.embeddings import (
    DEFAULT_OPENAI_EMBEDDING_MODEL,
    ModelEmbedder,
    openai_embedding_fn,
)
from oolu.providers.vault import SecretVault
from oolu.retrieval import score


# --------------------------------------------------------------------------- #
# ModelEmbedder: cache, fail-open, self-silencing                              #
# --------------------------------------------------------------------------- #
def test_dense_vectors_score_through_the_same_cosine():
    table = {
        "fetch the sales rows": [1.0, 0.0],
        "fetch sales for today": [0.9, 0.1],
        "water the plants": [0.0, 1.0],
    }
    embedder = ModelEmbedder(lambda text: table[text])
    close = score("fetch the sales rows", "fetch sales for today", embedder=embedder)
    far = score("fetch the sales rows", "water the plants", embedder=embedder)
    assert close > 0.9 and far == 0.0


def test_texts_embed_once_per_process():
    calls: list[str] = []

    def embed_fn(text):
        calls.append(text)
        return [1.0, 2.0]

    embedder = ModelEmbedder(embed_fn)
    for _ in range(5):
        embedder.embed("the same node card")
    assert calls == ["the same node card"]


def test_a_failing_endpoint_falls_back_to_lexical_and_then_goes_quiet():
    calls: list[int] = []

    def embed_fn(text):
        calls.append(1)
        raise RuntimeError("endpoint down")

    embedder = ModelEmbedder(embed_fn, max_failures=3)
    # Every failure still answers — lexically — and builds never notice.
    for i in range(6):
        vector = embedder.embed(f"goal number {i}")
        assert vector  # the lexical fallback spoke
    # After three consecutive failures the embedder stopped calling out.
    assert len(calls) == 3
    assert embedder.gave_up


def test_a_recovery_before_the_ceiling_resets_the_count():
    answers = iter([RuntimeError(), [1.0], RuntimeError(), RuntimeError()])

    def embed_fn(text):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    embedder = ModelEmbedder(embed_fn, max_failures=3)
    embedder.embed("a")  # fail 1
    embedder.embed("b")  # success — the count resets
    embedder.embed("c")  # fail 1 again
    embedder.embed("d")  # fail 2
    assert not embedder.gave_up


# --------------------------------------------------------------------------- #
# The wire: /embeddings through the authenticated adapter                      #
# --------------------------------------------------------------------------- #
class FakeTransport:
    def __init__(self, vector):
        self.requests: list[dict] = []
        self._vector = vector

    def request(self, method, url, *, headers=None, body=None, timeout=30.0):
        self.requests.append(
            {"method": method, "url": url, "headers": dict(headers or {}), "body": body}
        )
        return ProviderResponse(
            status=200, json={"data": [{"embedding": self._vector}]}
        )


def test_the_openai_embeddings_wire_rides_the_adapter_pipeline():
    vault = SecretVault()
    transport = FakeTransport([0.25, 0.5])
    adapter = OpenAiAdapter(
        vault=vault,
        transport=transport,
        api_key_ref=vault.put("sk-openai-0123456789", kind="api_key"),
    )

    embed = openai_embedding_fn(adapter)
    assert embed("rank this") == [0.25, 0.5]

    call = transport.requests[-1]
    assert call["method"] == "POST"
    assert call["url"].endswith("/embeddings")
    assert call["body"] == {
        "model": DEFAULT_OPENAI_EMBEDDING_MODEL,
        "input": ["rank this"],
    }
    # The key rode the auth header — embeddings are provider requests,
    # not a side door.
    assert call["headers"]["Authorization"].startswith("Bearer ")


# --------------------------------------------------------------------------- #
# The compiler's ranking obeys the injected embedder                           #
# --------------------------------------------------------------------------- #
CATALOG = [
    {
        "node_id": "node-fetch-sales",
        "title": "Fetch sales rows",
        "goal": "fetch the day's sales rows",
        "consumes": [],
        "produces": [{"name": "sales_rows", "type": "str"}],
    },
    {
        "node_id": "node-notify",
        "title": "Send a notification",
        "goal": "send a short notification message",
        "consumes": [{"name": "message", "type": "str"}],
        "produces": [{"name": "delivery", "type": "str"}],
    },
]


def test_the_compiler_ranks_with_the_model_backed_embedder():
    goal = "total the sales revenue"

    class Contrarian:
        """Insists the NOTIFY node is the goal's nearest neighbor —
        opposite of the lexical verdict, so obedience is observable."""

        def embed(self, text):
            if text == goal or "notification" in text:
                return {"m": 1.0}
            return {"z": 1.0}

    lexical = ContextPackCompiler(max_contracts=1).compile(goal, catalog=CATALOG)
    steered = ContextPackCompiler(max_contracts=1, embedder=Contrarian()).compile(
        goal, catalog=CATALOG
    )
    assert "Fetch sales rows" in lexical.text
    assert "Send a notification" in steered.text


# --------------------------------------------------------------------------- #
# The gateway's opt-in stays fail-open                                         #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("choice", ["", "off"])
def test_embeddings_off_means_no_embedder(tmp_path, monkeypatch, choice):
    from test_growth_trigger import _rig

    monkeypatch.setenv("OOLU_EMBEDDINGS", choice)
    app, conn, ident, desk, _script = _rig(tmp_path)
    try:
        session = type("S", (), {"tenant_id": "t1", "principal_id": "u1"})()
        assert app._author_embedder(session) is None
    finally:
        conn.close()


def test_openai_embeddings_without_a_key_stay_lexical(tmp_path, monkeypatch):
    from test_growth_trigger import _rig

    monkeypatch.setenv("OOLU_EMBEDDINGS", "openai")
    app, conn, ident, desk, _script = _rig(tmp_path)
    try:
        session = type("S", (), {"tenant_id": "t1", "principal_id": "u1"})()
        # No keyring on the rig → None, never an exception: recall is
        # advisory and a missing key must not break a build.
        assert app._author_embedder(session) is None
    finally:
        conn.close()
