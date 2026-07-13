"""Friends for real: people talking to people, and one thread per account.

Exit gate: messages between two accounts land in order with read state
(opening the thread reads it, unread counts say what waits); discovery is
EXACT username or e-mail — a public host holds strangers, so there is no
directory to browse; the peer must be a real, enabled account in the
caller's own tenant; and the OoLu conversation itself now survives the
device — /v1/chat records turns per account and /v1/chat/history is what
a fresh device loads.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from test_http_gateway import _app, _req

from oolu.durable.connection import DurableConnection
from oolu.gateway import GatewayApp
from oolu.identity import LocalAccountService, LocalUserStore
from oolu.identity.google_signin import IdentityLinkStore
from oolu.social import (
    MAX_MESSAGE_CHARS,
    AssistantHistoryStore,
    DirectMessageStore,
)

NOW = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# The store itself.                                                            #
# --------------------------------------------------------------------------- #
def _store(tmp_path):
    conn = DurableConnection(tmp_path / "dm.db")
    tick = {"now": NOW}

    def clock():
        tick["now"] += timedelta(seconds=1)
        return tick["now"]

    return conn, DirectMessageStore(conn, clock=clock)


def test_messages_land_in_order_with_read_state(tmp_path):
    conn, store = _store(tmp_path)
    store.send(tenant="t1", sender="alice", recipient="bob", body="hey bob")
    store.send(tenant="t1", sender="bob", recipient="alice", body="hey alice")
    store.send(tenant="t1", sender="bob", recipient="alice", body="you there?")

    thread = store.between(tenant="t1", me="alice", peer="bob")
    assert [m.body for m in thread] == ["hey bob", "hey alice", "you there?"]

    # Alice has two unread from bob; opening the thread reads them.
    [conversation] = store.conversations(tenant="t1", principal="alice")
    assert conversation["peer"] == "bob" and conversation["unread"] == 2
    assert store.unread_total(tenant="t1", principal="alice") == 2
    assert store.mark_read(tenant="t1", reader="alice", peer="bob") == 2
    [conversation] = store.conversations(tenant="t1", principal="alice")
    assert conversation["unread"] == 0
    conn.close()


def test_the_peer_list_sorts_by_freshness_and_scopes_by_tenant(tmp_path):
    conn, store = _store(tmp_path)
    store.send(tenant="t1", sender="alice", recipient="bob", body="old thread")
    store.send(tenant="t1", sender="carol", recipient="alice", body="new thread")
    store.send(tenant="t2", sender="mallory", recipient="alice", body="other world")

    conversations = store.conversations(tenant="t1", principal="alice")
    assert [c["peer"] for c in conversations] == ["carol", "bob"]
    assert conversations[0]["last_text"] == "new thread"
    conn.close()


def test_the_store_refuses_junk(tmp_path):
    conn, store = _store(tmp_path)
    with pytest.raises(ValueError, match="needs words"):
        store.send(tenant="t1", sender="alice", recipient="bob", body="   ")
    with pytest.raises(ValueError, match="too long"):
        store.send(
            tenant="t1",
            sender="alice",
            recipient="bob",
            body="x" * (MAX_MESSAGE_CHARS + 1),
        )
    with pytest.raises(ValueError, match="notes to self"):
        store.send(tenant="t1", sender="alice", recipient="alice", body="hi me")
    conn.close()


# --------------------------------------------------------------------------- #
# The gateway: real accounts behind the routes.                               #
# --------------------------------------------------------------------------- #
def _host(tmp_path):
    app, conn, ident = _app(tmp_path)
    users = LocalUserStore(":memory:")
    accounts = LocalAccountService(users, ident.store, ident._signer)
    for name in ("alice", "bob"):
        accounts.create_user(name, f"{name}-password-1", tenant="t1")
    links = IdentityLinkStore(conn)
    links.link(
        provider="email", subject="bob@mphepo.io", tenant="t1",
        username="bob", email="bob@mphepo.io", at=NOW,
    )
    gateway = GatewayApp(
        app._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        accounts=accounts,
        identity_links=links,
        direct_messages=DirectMessageStore(conn),
        assistant_history=AssistantHistoryStore(conn),
    )
    return gateway, conn, ident, users


def test_the_friend_flow_end_to_end(tmp_path):
    gateway, conn, ident, users = _host(tmp_path)
    alice, bob = ident.token("alice", "t1"), ident.token("bob", "t1")

    # Discovery is exact: an e-mail resolves through the identity link.
    found = gateway.handle(
        _req("POST", "/v1/friends/lookup", token=alice,
             body={"query": "bob@mphepo.io"})
    )
    assert found.status == 200 and found.body["username"] == "bob"

    sent = gateway.handle(
        _req("POST", "/v1/friends/bob/messages", token=alice,
             body={"text": "hey bob!"})
    )
    assert sent.status == 201, sent.body
    assert sent.body["mine"] is True and sent.body["text"] == "hey bob!"

    # Bob's peer list shows one unread conversation from alice...
    [conversation] = gateway.handle(
        _req("GET", "/v1/friends", token=bob)
    ).body["items"]
    assert conversation["peer"] == "alice" and conversation["unread"] == 1

    # ...and opening the thread reads it.
    thread = gateway.handle(
        _req("GET", "/v1/friends/alice/messages", token=bob)
    )
    assert [m["text"] for m in thread.body["items"]] == ["hey bob!"]
    assert thread.body["items"][0]["mine"] is False
    [conversation] = gateway.handle(
        _req("GET", "/v1/friends", token=bob)
    ).body["items"]
    assert conversation["unread"] == 0
    conn.close()


def test_you_address_people_by_exact_name_never_a_directory(tmp_path):
    gateway, conn, ident, users = _host(tmp_path)
    alice = ident.token("alice", "t1")

    for query in ("bo", "nobody", "nobody@mphepo.io"):
        missing = gateway.handle(
            _req("POST", "/v1/friends/lookup", token=alice, body={"query": query})
        )
        assert missing.status == 404, query
    yourself = gateway.handle(
        _req("POST", "/v1/friends/lookup", token=alice, body={"query": "alice"})
    )
    assert yourself.status == 400
    assert "notes to self" in yourself.body["error"]["message"]

    # Sending checks the same wall — no account, no thread.
    nobody = gateway.handle(
        _req("POST", "/v1/friends/nobody/messages", token=alice,
             body={"text": "hello?"})
    )
    assert nobody.status == 404
    conn.close()


def test_a_disabled_account_stops_receiving(tmp_path):
    gateway, conn, ident, users = _host(tmp_path)
    alice = ident.token("alice", "t1")
    gateway._accounts.set_disabled("bob", True)
    refused = gateway.handle(
        _req("POST", "/v1/friends/bob/messages", token=alice,
             body={"text": "hello?"})
    )
    assert refused.status == 404
    conn.close()


def test_friends_answer_404_on_hosts_without_the_store(tmp_path):
    app, conn, ident = _app(tmp_path)  # bare gateway: no direct_messages
    response = app.handle(
        _req("GET", "/v1/friends", token=ident.token("alice", "t1"))
    )
    assert response.status == 404
    assert "server" in response.body["error"]["message"]
    conn.close()


# --------------------------------------------------------------------------- #
# The OoLu thread survives the device.                                         #
# --------------------------------------------------------------------------- #
def test_chat_turns_land_in_the_account_history(tmp_path):
    gateway, conn, ident, users = _host(tmp_path)
    alice = ident.token("alice", "t1")

    turn = gateway.handle(
        _req("POST", "/v1/chat", token=alice, body={"message": "hi"})
    )
    assert turn.status == 200, turn.body

    history = gateway.handle(_req("GET", "/v1/chat/history", token=alice))
    assert history.status == 200
    kinds = [item["kind"] for item in history.body["items"]]
    assert kinds == ["user", "assistant"]
    assert history.body["items"][0]["body"] == "hi"
    assert history.body["items"][1]["body"] == turn.body["reply"]

    # Another account's device loads ITS OWN thread, not alice's.
    bob_history = gateway.handle(
        _req("GET", "/v1/chat/history", token=ident.token("bob", "t1"))
    )
    assert bob_history.body["items"] == []
    conn.close()


def test_the_history_is_capped_like_a_messenger(tmp_path):
    conn = DurableConnection(tmp_path / "turns.db")
    store = AssistantHistoryStore(conn)
    from oolu.social import ASSISTANT_HISTORY_KEEP

    for i in range(ASSISTANT_HISTORY_KEEP + 20):
        store.append(tenant="t1", principal="alice", kind="user", body=f"m{i}")
    items = store.history(tenant="t1", principal="alice", limit=10_000)
    assert len(items) == ASSISTANT_HISTORY_KEEP
    assert items[0]["body"] == "m20"  # the oldest fell off the back
    assert items[-1]["body"] == f"m{ASSISTANT_HISTORY_KEEP + 19}"
    conn.close()
