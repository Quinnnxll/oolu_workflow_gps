"""A stored key the machine can no longer read must degrade, never kill.

The production incident this pins down: model keys sealed in the shared
database under one machine key, while the install's machine.key file was
later replaced (a rebuilt volume, a moved install). Decryption then fails
AUTHENTICATION on every chat turn — and before this fix, the KeyringError
escaped the assistant (which only catches model errors) and the gateway
(which only caught GatewayError), reaching clients as a bare text/plain
"Internal Server Error" that broke their JSON parsing.

Three nets, tested inward-out: the router turns an unreadable key into
words that name the fix; chat therefore degrades to the model-less path;
and the gateway's last-resort net turns ANY future unhandled bug into a
JSON 500 instead of a text one.
"""

from __future__ import annotations

import pytest
from test_http_gateway import _app, _req

from oolu.billing import ModelCallMeter
from oolu.chat import ChatAssistant, ModelUnavailable
from oolu.durable.connection import DurableConnection
from oolu.providers.chatmodel import ChatModelRouter
from oolu.providers.keyring import ModelKeyring


@pytest.fixture()
def mismatched(tmp_path):
    """A keyring whose rows were sealed under a DIFFERENT machine key."""
    conn = DurableConnection(tmp_path / "durable.db")
    old = ModelKeyring(conn, key_path=tmp_path / "old-machine.key")
    old.store("t1", "openai", "sk-" + "a" * 40)
    old.store("t1", "anthropic", "sk-ant-" + "a" * 40)
    current = ModelKeyring(conn, key_path=tmp_path / "new-machine.key")
    yield current
    conn.close()


def _router(keyring, **kwargs):
    return ChatModelRouter(
        keyring,
        "t1",
        transport=None,
        meter=ModelCallMeter(),
        source=lambda: "own-api",
        **kwargs,
    )


def test_an_unreadable_key_becomes_words_that_name_the_fix(mismatched):
    router = _router(mismatched)
    with pytest.raises(ModelUnavailable) as caught:
        router.reply([{"role": "user", "content": "hello"}])
    message = str(caught.value)
    assert "can't be read on this machine" in message
    assert "remove it in Settings" in message
    assert "openai" in message and "anthropic" in message

    # The search probe survives the same corruption instead of crashing.
    assert _router(mismatched, web_search=lambda: True).web_search_ready() is False

    # And the conversation itself never dies: the assistant degrades to
    # its model-less path exactly as it does for a dead network.
    turn = ChatAssistant().respond(
        "please convert this report to pdf", model=_router(mismatched)
    )
    assert turn.source == "intent" and turn.task


def test_the_gateway_turns_any_unhandled_bug_into_a_json_500(tmp_path):
    app, conn, ident = _app(tmp_path)

    class _Buggy:
        def respond(self, *args, **kwargs):
            raise RuntimeError("wires crossed")

    app._chat = _Buggy()
    response = app.handle(
        _req("POST", "/v1/chat", token=ident.token("user-1"),
             body={"message": "hi", "history": []})
    )
    assert response.status == 500
    assert response.body["error"]["code"] == "internal"
    assert "RuntimeError" in response.body["error"]["message"]
    # Still a first-class response: JSON body, security headers, counted.
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    conn.close()
