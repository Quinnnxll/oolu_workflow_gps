"""The BYO-key door and the brain behind /v1/chat, end to end.

A pasted key goes in through POST /v1/keys/model, only a fingerprint ever
comes back, and the next chat turn is answered by the real (here: scripted)
provider. A dead provider degrades the same turn to the intent path — the
conversation survives everything.
"""

from __future__ import annotations

import json

from test_chat_model_router import FakeTransport, _anthropic_reply
from test_http_gateway import _app, _req

from oolu.billing import ModelCallMeter
from oolu.durable.connection import DurableConnection
from oolu.gateway import GatewayApp
from oolu.providers.keyring import ModelKeyring, fingerprint
from oolu.settings_node import SettingsNode, SettingsStore

KEY = "sk-ant-live-0123456789"


def _wired(tmp_path):
    """A gateway with the model door open, over the plain test scenario."""
    app, conn, ident = _app(tmp_path)
    transport = FakeTransport()
    meter = ModelCallMeter()
    keys_conn = DurableConnection(tmp_path / "keys.db")
    gateway = GatewayApp(
        app._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        settings_node=SettingsNode(SettingsStore(keys_conn)),
        model_keys=ModelKeyring(keys_conn, key_path=tmp_path / "machine.key"),
        model_meter=meter,
        model_transport=transport,
    )
    return gateway, transport, meter, (conn, keys_conn), ident


def test_a_pasted_key_becomes_a_fingerprint_and_a_working_brain(tmp_path):
    gateway, transport, meter, conns, ident = _wired(tmp_path)
    token = ident.token("user-1")
    transport.script(
        "anthropic.com",
        200,
        _anthropic_reply(json.dumps({"say": "Hello Quinn.", "task": None})),
    )

    added = gateway.handle(
        _req(
            "POST",
            "/v1/keys/model",
            token=token,
            body={"provider": "anthropic", "key": KEY},
        )
    )
    assert added.status == 201
    assert added.body == {"provider": "anthropic", "fingerprint": fingerprint(KEY)}

    listing = gateway.handle(_req("GET", "/v1/keys/model", token=token))
    assert listing.status == 200
    assert listing.body["items"][0]["fingerprint"] == fingerprint(KEY)
    assert KEY not in json.dumps(listing.body)

    turn = gateway.handle(
        _req("POST", "/v1/chat", token=token, body={"message": "say hello"})
    )
    assert turn.status == 200
    assert turn.body["reply"] == "Hello Quinn."
    assert turn.body["source"] == "model"
    assert turn.body["run_id"] is None
    # The consultation entered the books.
    assert meter.total_cost("chat.turn") > 0

    for conn in conns:
        conn.close()


def test_a_dead_provider_degrades_the_turn_to_intent(tmp_path):
    gateway, transport, meter, conns, ident = _wired(tmp_path)
    token = ident.token("user-1")
    gateway.handle(
        _req(
            "POST",
            "/v1/keys/model",
            token=token,
            body={"provider": "anthropic", "key": KEY},
        )
    )
    transport.script("anthropic.com", 500, {"error": "provider is down"})

    turn = gateway.handle(
        _req("POST", "/v1/chat", token=token, body={"message": "auto"})
    )
    assert turn.status == 200
    assert turn.body["source"] == "intent"
    assert turn.body["run_id"] is not None  # the work still started

    for conn in conns:
        conn.close()


def test_junk_and_removal(tmp_path):
    gateway, transport, meter, conns, ident = _wired(tmp_path)
    token = ident.token("user-1")

    bad_provider = gateway.handle(
        _req(
            "POST",
            "/v1/keys/model",
            token=token,
            body={"provider": "skynet", "key": KEY},
        )
    )
    assert bad_provider.status == 400
    bad_key = gateway.handle(
        _req(
            "POST",
            "/v1/keys/model",
            token=token,
            body={"provider": "openai", "key": "x"},
        )
    )
    assert bad_key.status == 400

    gateway.handle(
        _req(
            "POST",
            "/v1/keys/model",
            token=token,
            body={"provider": "openai", "key": KEY},
        )
    )
    removed = gateway.handle(
        _req("DELETE", "/v1/keys/model/openai", token=token)
    )
    assert removed.status == 200
    again = gateway.handle(_req("DELETE", "/v1/keys/model/openai", token=token))
    assert again.status == 404

    for conn in conns:
        conn.close()


def test_an_unexecutable_plan_is_said_not_crashed(tmp_path):
    """Found live: with the starter pack loaded, an intent whose planned
    route needs a capability no executor provides raised PreflightError
    straight through /v1/chat as a 500. The engine's refusal must become
    words in the conversation (and a 422 on /v1/runs), never a crash."""
    from test_http_gateway import _autonomous, _blueprint, _Executor

    def _unexecutable():
        brief, _, _, _ = _autonomous()
        # The plan grounds and routes fine — but the one executor on this
        # machine lacks the required capability, so execution preflight
        # refuses (the exact live failure shape).
        return (
            brief,
            _blueprint(operation="http/get", capability="get"),
            _Executor({"something-else"}),
            {"a": "get"},
        )

    app, conn, ident = _app(tmp_path, _unexecutable)
    token = ident.token("user-1")

    turn = app.handle(
        _req("POST", "/v1/chat", token=token, body={"message": "auto"})
    )
    assert turn.status == 200
    assert "can't run that on this machine" in turn.body["reply"]
    assert turn.body["run_id"] is None

    submit = app.handle(
        _req("POST", "/v1/runs", token=token, body={"intent": "auto"})
    )
    assert submit.status == 422
    assert submit.body["error"]["code"] == "cannot_execute"

    conn.close()


def test_keys_are_tenant_scoped(tmp_path):
    gateway, transport, meter, conns, ident = _wired(tmp_path)
    gateway.handle(
        _req(
            "POST",
            "/v1/keys/model",
            token=ident.token("user-1"),  # tenant t1
            body={"provider": "anthropic", "key": KEY},
        )
    )
    other = gateway.handle(
        _req("GET", "/v1/keys/model", token=ident.token("user-2", tenant="t2"))
    )
    assert other.body["items"] == []

    for conn in conns:
        conn.close()
