"""Friend memory: name notes, when-we-met, and OoLu's find_friend.

Exit gate (Issue 13): a user renames a friend the old way — a private
name note like "Anna from the conference" only they ever see — through
the avatar in the Friends list; the friends listing carries the note and
when the friendship began; and OoLu finds a friend by name, by the note,
by words from the conversation, or by roughly when they became friends —
reporting only what is actually stored.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from test_http_gateway import _app, _Identity, _req

from oolu.chat import GatewayChatTools
from oolu.durable.connection import DurableConnection
from oolu.durable.files import UserFileStore
from oolu.gateway import GatewayApp
from oolu.identity import LocalAccountService, LocalUserStore
from oolu.social import DirectMessageStore, FriendshipError, FriendshipStore

NOW = datetime(2026, 5, 12, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# The store: a note is the owner's own, and the date is remembered.            #
# --------------------------------------------------------------------------- #
def _store(tmp_path):
    conn = DurableConnection(tmp_path / "f.db")
    return conn, FriendshipStore(conn, clock=lambda: NOW)


def test_a_name_note_is_the_owners_own(tmp_path):
    conn, f = _store(tmp_path)
    try:
        f.set_alias(tenant="t", owner="alice", peer="bob", alias="Bob from gym")
        assert f.aliases(tenant="t", owner="alice") == {"bob": "Bob from gym"}
        # Bob never sees what alice calls him — the note is hers alone.
        assert f.aliases(tenant="t", owner="bob") == {}
        # Renaming replaces; whitespace is tidied to one clean label.
        f.set_alias(tenant="t", owner="alice", peer="bob", alias="  Bob\n Smith ")
        assert f.aliases(tenant="t", owner="alice") == {"bob": "Bob Smith"}
        # An empty note erases — back to the real username.
        f.set_alias(tenant="t", owner="alice", peer="bob", alias="")
        assert f.aliases(tenant="t", owner="alice") == {}
    finally:
        conn.close()


def test_a_note_is_a_label_not_a_paragraph(tmp_path):
    conn, f = _store(tmp_path)
    try:
        with pytest.raises(FriendshipError, match="label, not a paragraph"):
            f.set_alias(tenant="t", owner="alice", peer="bob", alias="x" * 61)
    finally:
        conn.close()


def test_friends_since_remembers_the_acceptance(tmp_path):
    conn, f = _store(tmp_path)
    try:
        f.request(tenant="t", requester="alice", target="bob")
        f.accept(tenant="t", me="bob", requester="alice")
        since = f.friends_since(tenant="t", me="alice")
        assert since["bob"].startswith("2026-05-12")
        # A pending request is not a friendship yet — no date to remember.
        f.request(tenant="t", requester="alice", target="carol")
        assert "carol" not in f.friends_since(tenant="t", me="alice")
    finally:
        conn.close()


def test_erasure_takes_the_notes_too(tmp_path):
    conn, f = _store(tmp_path)
    try:
        f.set_alias(tenant="t", owner="alice", peer="bob", alias="gym Bob")
        f.set_alias(tenant="t", owner="carol", peer="bob", alias="cousin Bob")
        f.erase_principal(tenant="t", principal="bob")
        assert f.aliases(tenant="t", owner="alice") == {}
        assert f.aliases(tenant="t", owner="carol") == {}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The gateway: rename through the avatar, and a listing that remembers.        #
# --------------------------------------------------------------------------- #
def _host(tmp_path):
    app, conn, ident = _app(tmp_path)
    users = LocalUserStore(":memory:")
    accounts = LocalAccountService(users, ident.store, ident._signer)
    for name in ("alice", "bob"):
        accounts.create_user(name, f"{name}-password-1", tenant="t1")
    gateway = GatewayApp(
        app._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        accounts=accounts,
        direct_messages=DirectMessageStore(conn),
        friendships=FriendshipStore(conn),
    )
    return gateway, conn, ident


def _befriend(gateway, ident):
    alice, bob = ident.token("alice", "t1"), ident.token("bob", "t1")
    gateway.handle(
        _req("POST", "/v1/friends/requests", token=alice, body={"username": "bob"})
    )
    gateway.handle(
        _req("POST", "/v1/friends/requests/alice", token=bob,
             body={"action": "accept"})
    )
    return alice, bob


def test_renaming_a_friend_shows_in_the_list(tmp_path):
    gateway, conn, ident = _host(tmp_path)
    try:
        alice, bob = _befriend(gateway, ident)
        named = gateway.handle(
            _req("PUT", "/v1/friends/bob/alias", token=alice,
                 body={"alias": "Bob from the conference"})
        )
        assert named.status == 200
        assert named.body == {"peer": "bob", "alias": "Bob from the conference"}
        # Alice's list carries her note and the date; the thread is empty
        # (friendship exists from acceptance) yet the friend still shows.
        [row] = gateway.handle(_req("GET", "/v1/friends", token=alice)).body[
            "items"
        ]
        assert row["peer"] == "bob"
        assert row["alias"] == "Bob from the conference"
        assert row["since"]  # ISO timestamp of the acceptance
        # Bob's own list shows alice unrenamed — the note is not his.
        [his] = gateway.handle(_req("GET", "/v1/friends", token=bob)).body[
            "items"
        ]
        assert his["peer"] == "alice" and his["alias"] == ""
        # Clearing the note through the same door.
        cleared = gateway.handle(
            _req("PUT", "/v1/friends/bob/alias", token=alice, body={"alias": ""})
        )
        assert cleared.body["alias"] == ""
    finally:
        conn.close()


def test_a_paragraph_note_is_refused_politely(tmp_path):
    gateway, conn, ident = _host(tmp_path)
    try:
        alice, _bob = _befriend(gateway, ident)
        refused = gateway.handle(
            _req("PUT", "/v1/friends/bob/alias", token=alice,
                 body={"alias": "y" * 61})
        )
        assert refused.status == 400
        assert "label" in refused.body["error"]["message"]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# OoLu's find_friend: search the way memory works.                             #
# --------------------------------------------------------------------------- #
def _search_rig(tmp_path):
    conn = DurableConnection(tmp_path / "durable.db")
    ident = _Identity(tmp_path)
    users = LocalUserStore(":memory:")
    accounts = LocalAccountService(users, ident.store, ident._signer)
    for name in ("alice", "bob", "carol"):
        accounts.create_user(name, f"{name}-password-1", tenant="t1")
    friendships = FriendshipStore(conn, clock=lambda: NOW)
    messages = DirectMessageStore(conn)
    for peer in ("bob", "carol"):
        friendships.request(tenant="t1", requester="alice", target=peer)
        friendships.accept(tenant="t1", me=peer, requester="alice")
    tools = GatewayChatTools(
        UserFileStore(conn),
        tenant="t1",
        principal="alice",
        accounts=accounts,
        direct_messages=messages,
        friendships=friendships,
    )
    return conn, tools, friendships, messages


def test_find_friend_by_name_note_words_and_date(tmp_path):
    conn, tools, friendships, messages = _search_rig(tmp_path)
    try:
        friendships.set_alias(
            tenant="t1", owner="alice", peer="carol",
            alias="Carol from the conference",
        )
        messages.send(
            tenant="t1", sender="bob", recipient="alice",
            body="the cabin is booked for August",
        )
        # By username.
        assert "bob" in tools.search_friends("bob")
        # By the owner's own note.
        by_note = tools.search_friends("conference")
        assert "carol" in by_note and "Carol from the conference" in by_note
        # By words from the conversation — with who said them.
        by_words = tools.search_friends("cabin")
        assert "bob" in by_words and "they said" in by_words
        # By roughly when the friendship began.
        by_date = tools.search_friends("2026-05")
        assert "bob" in by_date and "carol" in by_date
        assert "friends since 2026-05-12" in by_date
        # No match reports honestly instead of guessing a name.
        assert tools.search_friends("zanzibar").startswith("no friend matched")
    finally:
        conn.close()


def test_find_friend_without_friends_enabled_answers_in_words(tmp_path):
    conn = DurableConnection(tmp_path / "d.db")
    try:
        tools = GatewayChatTools(
            UserFileStore(conn), tenant="t1", principal="alice"
        )
        assert tools.search_friends("bob").startswith("error:")
    finally:
        conn.close()
