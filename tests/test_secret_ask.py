"""The secret ask (V2, 7b): a keyed API asks with a form, never a chat line.

Exit gate (node-vitality-plan, phase V2): a build against a keyed API
pauses with the ``secret_form`` block; the key lands through the one
dedicated door into the DURABLE vault (sealed at rest under the machine
key — nowhere greppable); the door completes the publish, binds the
credential beside the node's host grants, and GRANTS the host; the
broker injects the header host-side for granted hosts only, so the
value never enters the sandbox, the action, or a log.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from test_growth_trigger import GOAL, _chat, _rig
from test_http_gateway import _req
from test_node_interact import FakeAuthor

from oolu.durable import DurableConnection
from oolu.nodeplace.credentials import NodeCredentialStore
from oolu.providers import DurableSecretVault
from oolu.providers.errors import RevokedCredential
from oolu.runtime.backend import WebAuth, WebGrant
from oolu.runtime.webhand import WebBroker

SECRET = "sk-live-SUPER-SECRET-9f8e7d"

# Declares its keyed API on the prose channel: the script calls the API
# bare — the HOST injects the credential at run time.
KEYED_API = (
    "1. Call the API.\n"
    'IO: {"inputs": [], "outputs": [{"name": "result", "type": "str"}]}\n'
    'SECRETS: [{"name": "api_key", "label": "Your Example API key", '
    '"host": "api.example.com"}]\n'
    "```python\n"
    "from _oolu_runtime import emit_result, http_request\n"
    "page = http_request('https://api.example.com/v1/things')\n"
    "emit_result({'result': str(page.get('status'))})\n"
    "```\n"
)


# --------------------------------------------------------------------------- #
# The durable vault: sealed at rest, nowhere greppable.                        #
# --------------------------------------------------------------------------- #
def test_the_vault_seals_at_rest_and_survives_the_process(tmp_path):
    conn = DurableConnection(tmp_path / "vault.db")
    vault = DurableSecretVault(conn, key_path=tmp_path / "machine.key")
    ref = vault.put(SECRET, kind="node_credential")
    assert vault.resolve(ref) == SECRET
    assert vault.resolve(ref.ref_id) == SECRET  # the string protocol too
    # Nowhere greppable: the database bytes never contain the plaintext.
    raw = Path(tmp_path / "vault.db").read_bytes()
    assert SECRET.encode() not in raw
    # A fresh process over the same rows and key still resolves.
    again = DurableSecretVault(conn, key_path=tmp_path / "machine.key")
    assert again.resolve(ref) == SECRET
    assert "REDACTED" in again.redact(f"leaked: {SECRET}")
    vault.revoke(ref)
    try:
        again.resolve(ref)
        raise AssertionError("a revoked credential resolved")
    except RevokedCredential:
        pass
    conn.close()


def test_a_wrong_machine_key_fails_closed(tmp_path):
    conn = DurableConnection(tmp_path / "vault.db")
    vault = DurableSecretVault(conn, key_path=tmp_path / "machine.key")
    ref = vault.put(SECRET)
    stranger = DurableSecretVault(conn, key_path=tmp_path / "other.key")
    try:
        stranger.resolve(ref)
        raise AssertionError("a foreign machine key opened the row")
    except Exception as exc:  # noqa: BLE001 - the exact class is the keyring's
        assert "machine key" in str(exc) or "corrupt" in str(exc)
    conn.close()


def test_credentials_bind_beside_the_grants_and_rotate(tmp_path):
    conn = DurableConnection(tmp_path / "creds.db")
    store = NodeCredentialStore(conn)
    first = store.bind(
        tenant="t1", node_id="n1", host="api.example.com", ref_id="ref-1"
    )
    assert first is None
    rows = store.for_node(tenant="t1", node_id="n1")
    assert rows[0]["host"] == "api.example.com"
    assert rows[0]["ref"] == "ref-1"
    assert rows[0]["scheme"] == "Bearer"
    # Rotation returns the OLD ref so the caller can revoke it.
    previous = store.bind(
        tenant="t1", node_id="n1", host="api.example.com", ref_id="ref-2"
    )
    assert previous == "ref-1"
    assert store.for_node(tenant="t1", node_id="n1")[0]["ref"] == "ref-2"
    assert (
        store.drop(tenant="t1", node_id="n1", host="api.example.com")
        == "ref-2"
    )
    assert store.for_node(tenant="t1", node_id="n1") == []
    conn.close()


# --------------------------------------------------------------------------- #
# The broker seam: injection host-side, granted hosts only.                    #
# --------------------------------------------------------------------------- #
class _Secrets:
    def __init__(self, table):
        self._table = table

    def resolve(self, ref):
        return self._table[ref]


def _broker(calls, *, hosts=("api.example.com",), auth=()):
    def fetch(method, url, *, headers=None, body=None, grant=None, blocked=None):
        calls.append({"url": url, "headers": headers})
        return {
            "status": 200,
            "url": url,
            "content_type": "",
            "body": "",
            "truncated": False,
            "error": None,
        }

    return WebBroker(
        fetch=fetch,
        grant=WebGrant(hosts=hosts, auth=tuple(auth)),
        secrets=_Secrets({"ref-1": SECRET}),
    )


def test_the_broker_injects_for_the_granted_host_only(tmp_path):
    calls: list = []
    broker = _broker(
        calls,
        auth=[WebAuth(host="api.example.com", ref="ref-1")],
    )
    assert broker._auth_headers("https://api.example.com/v1/x") == {
        "Authorization": f"Bearer {SECRET}"
    }
    # Subdomains of the bound host authenticate too.
    assert broker._auth_headers("https://sub.api.example.com/x") != {}
    # A host the credential is not bound to gets nothing.
    assert broker._auth_headers("https://other.example.com/x") == {}
    # A bound host OUTSIDE the grant gets nothing — granted hosts only.
    ungranted = _broker(
        calls,
        hosts=("elsewhere.com",),
        auth=[WebAuth(host="api.example.com", ref="ref-1")],
    )
    assert ungranted._auth_headers("https://api.example.com/x") == {}
    # A dead ref never breaks the call — the request goes out bare.
    dead = _broker(
        calls, auth=[WebAuth(host="api.example.com", ref="ref-gone")]
    )
    assert dead._auth_headers("https://api.example.com/x") == {}


def test_the_vault_header_beats_the_scripts_own(tmp_path):
    calls: list = []
    broker = _broker(
        calls, auth=[WebAuth(host="api.example.com", ref="ref-1")]
    )
    req_path = tmp_path / "c1.req.json"
    req_path.write_text(
        json.dumps(
            {
                "id": "c1",
                "method": "GET",
                "url": "https://api.example.com/v1/x",
                "headers": {"Authorization": "Bearer script-own"},
            }
        ),
        "utf-8",
    )
    broker._answer(req_path)
    assert calls[0]["headers"]["Authorization"] == f"Bearer {SECRET}"
    # The call log never carries headers, let alone values.
    assert "headers" not in broker.calls[0]


# --------------------------------------------------------------------------- #
# The build pauses with the form; the door completes it.                       #
# --------------------------------------------------------------------------- #
def _keyed_build(tmp_path):
    app, conn, ident, desk, _script = _rig(tmp_path)
    app._node_function_author = lambda tenant: FakeAuthor(KEYED_API)
    reply = _chat(app, ident, f"build me a node that {GOAL}")
    return app, conn, ident, desk, reply


def test_a_declared_keyed_api_pauses_the_build_with_the_form(tmp_path):
    app, conn, ident, desk, reply = _keyed_build(tmp_path)
    try:
        assert reply.status == 200, reply.body
        assert "needs a key" in reply.body["reply"]
        assert "Nothing is published" in reply.body["reply"]
        block = reply.body["block"]
        assert block and block["kind"] == "secret_form"
        assert block["build_id"]
        assert block["fields"][0]["host"] == "api.example.com"
        assert block["fields"][0]["label"] == "Your Example API key"
        # Nothing stands on the desk — the publish waits for the key.
        assert (
            app._nodeplace.list_own_nodes(
                noder_principal="user-1", tenant_id="t1"
            )
            == []
        )
        # The form block PERSISTS with the turn: every device sees it.
        turns = app._assistant_history.history(tenant="t1", principal="user-1")
        assert turns[-1]["block"]["kind"] == "secret_form"
    finally:
        conn.close()


def test_the_door_completes_the_build_and_the_key_is_nowhere_greppable(
    tmp_path,
):
    app, conn, ident, desk, reply = _keyed_build(tmp_path)
    try:
        build_id = reply.body["block"]["build_id"]
        done = app.handle(
            _req(
                "POST",
                f"/v1/builds/{build_id}/secrets",
                token=ident.token("user-1", "t1"),
                body={
                    "items": [{"host": "api.example.com", "value": SECRET}]
                },
            )
        )
        assert done.status == 201, done.body
        node_id = done.body["node_id"]
        assert "Built a NEW node" in done.body["say"]
        assert "sealed into this host's vault" in done.body["say"]
        # The value appears NOWHERE: not in the response beyond counts,
        # not in the durable database's raw bytes.
        assert SECRET not in json.dumps(done.body).replace(SECRET, SECRET[:0])
        raw = Path(tmp_path / "durable.db").read_bytes()
        assert SECRET.encode() not in raw
        # The credential stands beside the node's grants — and the host
        # itself was granted (providing the key IS the consent).
        rows = app._node_credentials.for_node(tenant="t1", node_id=node_id)
        assert rows and rows[0]["host"] == "api.example.com"
        account = desk.account_for(node_id)
        assert "api.example.com" in account.network_hosts
        assert app._vault.resolve(rows[0]["ref"]) == SECRET
        # The receipt landed in the thread that asked for the build.
        turns = app._assistant_history.history(tenant="t1", principal="user-1")
        assert turns[-1]["kind"] == "assistant"
        assert "authenticates from its first run" in turns[-1]["body"]
        # Consumed exactly once.
        second = app.handle(
            _req(
                "POST",
                f"/v1/builds/{build_id}/secrets",
                token=ident.token("user-1", "t1"),
                body={
                    "items": [{"host": "api.example.com", "value": SECRET}]
                },
            )
        )
        assert second.status == 404
        # The run stamps carry the credential as a REF the broker
        # resolves — never the value.
        session = SimpleNamespace(tenant_id="t1", principal_id="user-1")
        extras = app._node_function_extras(session, node_id)
        assert extras["_egress_auth"][0]["host"] == "api.example.com"
        assert extras["_egress_auth"][0]["ref"] == rows[0]["ref"]
        assert SECRET not in json.dumps(extras)
    finally:
        conn.close()


def test_the_node_door_stores_for_a_standing_node(tmp_path):
    from test_verify_at_birth import GOOD

    app, conn, ident, desk, _script = _rig(tmp_path)
    try:
        app._node_function_author = lambda tenant: FakeAuthor(GOOD)
        built = _chat(app, ident, f"build me a node that {GOAL}")
        assert "Built a NEW node" in built.body["reply"]
        node_id = app._nodeplace.list_own_nodes(
            noder_principal="user-1", tenant_id="t1"
        )[0].node_id
        answered = app.handle(
            _req(
                "POST",
                f"/v1/work/nodes/{node_id}/secrets",
                token=ident.token("user-1", "t1"),
                body={
                    "items": [{"host": "api.example.com", "value": SECRET}]
                },
            )
        )
        assert answered.status == 201, answered.body
        assert answered.body == {
            "stored": 1,
            "hosts": ["api.example.com"],
        }
        account = desk.account_for(node_id)
        assert "api.example.com" in account.network_hosts
        assert app._node_credentials.for_node(tenant="t1", node_id=node_id)
    finally:
        conn.close()
