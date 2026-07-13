"""Finding someone is a request they decide — not a message that appears.

Exit gate (Issue 7): a friend search sends a request the recipient accepts
or blocks; a blocked account can neither message nor request; an account
that only accepts friends turns a stranger's message into "send a request
first", while an open account is unchanged; an e-mail search finds a
Google-linked account (its email is stored); and a Google-created account
can set a sign-in password so username+password works next time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from test_http_gateway import _app, _req

from oolu.durable.connection import DurableConnection
from oolu.gateway import GatewayApp
from oolu.identity import LocalAccountService, LocalUserStore
from oolu.identity.google_signin import IdentityLinkStore
from oolu.social import DirectMessageStore, FriendshipError, FriendshipStore

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# The friendship store.                                                       #
# --------------------------------------------------------------------------- #
def _store(tmp_path):
    conn = DurableConnection(tmp_path / "f.db")
    tick = {"now": NOW}

    def clock():
        tick["now"] += timedelta(seconds=1)
        return tick["now"]

    return conn, FriendshipStore(conn, clock=clock)


def test_a_request_is_decided_by_the_recipient(tmp_path):
    conn, f = _store(tmp_path)
    assert f.request(tenant="t", requester="alice", target="bob") == "pending_out"
    assert f.relationship(tenant="t", me="bob", other="alice") == "pending_in"
    assert f.incoming(tenant="t", me="bob") == ["alice"]
    # Bob accepts: friendship reads the same from both sides.
    f.accept(tenant="t", me="bob", requester="alice")
    assert f.are_friends(tenant="t", a="alice", b="bob")
    assert f.are_friends(tenant="t", a="bob", b="alice")
    assert f.incoming(tenant="t", me="bob") == []
    conn.close()


def test_two_requests_meeting_become_a_friendship(tmp_path):
    conn, f = _store(tmp_path)
    f.request(tenant="t", requester="alice", target="bob")
    # Bob asks back instead of pressing accept: the requests meet.
    assert f.request(tenant="t", requester="bob", target="alice") == "friends"
    assert f.are_friends(tenant="t", a="alice", b="bob")
    conn.close()


def test_a_block_stops_messages_and_requests(tmp_path):
    conn, f = _store(tmp_path)
    f.request(tenant="t", requester="alice", target="bob")
    f.block(tenant="t", me="bob", other="alice")
    # The pending request is gone, and alice can't message or re-request.
    assert f.incoming(tenant="t", me="bob") == []
    assert not f.may_message(tenant="t", sender="alice", recipient="bob")
    with pytest.raises(FriendshipError, match="can't send a request"):
        f.request(tenant="t", requester="alice", target="bob")
    # Unblocking restores the ability to message (open recipient).
    f.unblock(tenant="t", me="bob", other="alice")
    assert f.may_message(tenant="t", sender="alice", recipient="bob")
    conn.close()


def test_friends_only_recipient_refuses_strangers(tmp_path):
    conn, f = _store(tmp_path)
    # Default is open: a stranger may message.
    assert f.may_message(tenant="t", sender="alice", recipient="bob")
    f.set_allow_nonfriend(tenant="t", principal="bob", allow=False)
    assert not f.may_message(tenant="t", sender="alice", recipient="bob")
    # A friend still may.
    f.request(tenant="t", requester="alice", target="bob")
    f.accept(tenant="t", me="bob", requester="alice")
    assert f.may_message(tenant="t", sender="alice", recipient="bob")
    conn.close()


# --------------------------------------------------------------------------- #
# The gateway: requests, the message gate, e-mail search, password set.        #
# --------------------------------------------------------------------------- #
def _host(tmp_path):
    app, conn, ident = _app(tmp_path)
    users = LocalUserStore(":memory:")
    accounts = LocalAccountService(users, ident.store, ident._signer)
    for name in ("alice", "bob"):
        accounts.create_user(name, f"{name}-password-1", tenant="t1")
    links = IdentityLinkStore(conn)
    # A Google-linked account: email lives in the email COLUMN, provider
    # 'google' — exactly the case that used to fail an e-mail search.
    links.link(
        provider="google", subject="google-sub-123", tenant="t1",
        username="bob", email="bob@gmail.com", at=NOW,
    )
    gateway = GatewayApp(
        app._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        accounts=accounts,
        identity_links=links,
        direct_messages=DirectMessageStore(conn),
        friendships=FriendshipStore(conn),
    )
    return gateway, conn, ident


def test_the_request_flow_end_to_end(tmp_path):
    gateway, conn, ident = _host(tmp_path)
    alice, bob = ident.token("alice", "t1"), ident.token("bob", "t1")

    # A Google user is findable by e-mail (the column, not the subject).
    found = gateway.handle(
        _req("POST", "/v1/friends/lookup", token=alice,
             body={"query": "bob@gmail.com"})
    )
    assert found.status == 200 and found.body["username"] == "bob"
    assert found.body["relationship"] == "none"

    # Sending a request, not a message.
    sent = gateway.handle(
        _req("POST", "/v1/friends/requests", token=alice, body={"username": "bob"})
    )
    assert sent.status == 200 and sent.body["relationship"] == "pending_out"
    # Bob sees it waiting and accepts.
    assert gateway.handle(
        _req("GET", "/v1/friends/requests", token=bob)
    ).body["items"] == ["alice"]
    accepted = gateway.handle(
        _req("POST", "/v1/friends/requests/alice", token=bob,
             body={"action": "accept"})
    )
    assert accepted.body["relationship"] == "friends"
    conn.close()


def test_a_friends_only_recipient_refuses_a_strangers_message(tmp_path):
    gateway, conn, ident = _host(tmp_path)
    alice, bob = ident.token("alice", "t1"), ident.token("bob", "t1")

    # Bob closes his door to strangers.
    gateway.handle(
        _req("PUT", "/v1/friends/settings", token=bob,
             body={"allow_nonfriend_messages": False})
    )
    refused = gateway.handle(
        _req("POST", "/v1/friends/bob/messages", token=alice,
             body={"text": "hi stranger"})
    )
    assert refused.status == 403 and refused.body["error"]["code"] == "not_friends"

    # After they befriend, the message goes through.
    gateway.handle(
        _req("POST", "/v1/friends/requests", token=alice, body={"username": "bob"})
    )
    gateway.handle(
        _req("POST", "/v1/friends/requests/alice", token=bob,
             body={"action": "accept"})
    )
    assert gateway.handle(
        _req("POST", "/v1/friends/bob/messages", token=alice,
             body={"text": "hi friend"})
    ).status == 201

    # A block stops mail cold.
    gateway.handle(
        _req("POST", "/v1/friends/requests/alice", token=bob,
             body={"action": "block"})
    )
    assert gateway.handle(
        _req("POST", "/v1/friends/bob/messages", token=alice,
             body={"text": "let me back in"})
    ).status == 403
    conn.close()


def test_a_google_account_can_set_a_sign_in_password(tmp_path):
    gateway, conn, ident = _host(tmp_path)
    # bob arrived via Google — the token stands in for that session.
    bob = ident.token("bob", "t1")
    set_pw = gateway.handle(
        _req("POST", "/v1/auth/password", token=bob,
             body={"password": "chosen-password-9"})
    )
    assert set_pw.status == 200 and set_pw.body["username"] == "bob"
    # Too-short passwords are refused.
    assert gateway.handle(
        _req("POST", "/v1/auth/password", token=bob, body={"password": "short"})
    ).status == 400
    conn.close()
