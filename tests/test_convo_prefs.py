"""How a conversation sits in the list: pinned, muted, hidden, gone.

Exit gate: the friends list reads like a messenger — pinned first, then
the most recently spoken — and every margin is the owner's own: pin and
mute are flags only they see, hide is a MOMENT (the thread returns by
itself when the other side speaks again), and delete unfriends without
shredding history or laying a block. The Noder list carries the same
margins per run thread, walled to the run's own submitter.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from test_http_gateway import _app, _req

from oolu.durable.connection import DurableConnection
from oolu.gateway import GatewayApp
from oolu.social import DirectMessageStore, FriendshipStore

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)


def _store(tmp_path):
    conn = DurableConnection(tmp_path / "f.db")
    tick = {"now": NOW}

    def clock():
        tick["now"] += timedelta(seconds=1)
        return tick["now"]

    return conn, FriendshipStore(conn, clock=clock)


# --------------------------------------------------------------------------- #
# The store: margins are the owner's own.                                      #
# --------------------------------------------------------------------------- #
def test_prefs_move_only_the_named_fields(tmp_path):
    conn, f = _store(tmp_path)
    pref = f.set_pref(tenant="t", owner="me", kind="friend", key="bob", pinned=True)
    assert pref["pinned"] is True and pref["muted"] is False
    # Muting later leaves the pin standing.
    pref = f.set_pref(tenant="t", owner="me", kind="friend", key="bob", muted=True)
    assert pref["pinned"] is True and pref["muted"] is True
    # Hiding stamps a moment; unhiding clears it.
    pref = f.set_pref(tenant="t", owner="me", kind="friend", key="bob", hidden=True)
    assert pref["hidden_at"]
    pref = f.set_pref(tenant="t", owner="me", kind="friend", key="bob", hidden=False)
    assert pref["hidden_at"] is None
    conn.close()


def test_remove_unfriends_and_clears_my_margins_without_a_block(tmp_path):
    conn, f = _store(tmp_path)
    f.request(tenant="t", requester="alice", target="bob")
    f.accept(tenant="t", me="bob", requester="alice")
    f.set_alias(tenant="t", owner="bob", peer="alice", alias="Anna")
    f.set_pref(tenant="t", owner="bob", kind="friend", key="alice", pinned=True)

    f.remove(tenant="t", me="bob", other="alice")

    assert f.relationship(tenant="t", me="bob", other="alice") == "none"
    assert f.aliases(tenant="t", owner="bob") == {}
    assert f.prefs(tenant="t", owner="bob", kind="friend") == {}
    # No block was laid: alice may ask again.
    assert f.request(tenant="t", requester="alice", target="bob") == "pending_out"
    conn.close()


# --------------------------------------------------------------------------- #
# The gateway: the list reads like a messenger.                                #
# --------------------------------------------------------------------------- #
def _host(tmp_path):
    from oolu.identity import LocalAccountService, LocalUserStore

    app, conn, ident = _app(tmp_path)
    users = LocalUserStore(":memory:")
    accounts = LocalAccountService(users, ident.store, ident._signer)
    for name in ("user-1", "alice", "bob", "carol"):
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


def _befriend(gw, ident, me, other):
    sent = gw.handle(
        _req(
            "POST", "/v1/friends/requests",
            token=ident.token(me, "t1"), body={"username": other},
        )
    )
    assert sent.status == 200, sent.body
    accepted = gw.handle(
        _req(
            "POST", f"/v1/friends/requests/{me}",
            token=ident.token(other, "t1"), body={"action": "accept"},
        )
    )
    assert accepted.status == 200, accepted.body


def _say(gw, ident, me, to, text):
    sent = gw.handle(
        _req(
            "POST", f"/v1/friends/{to}/messages",
            token=ident.token(me, "t1"), body={"text": text},
        )
    )
    assert sent.status in (200, 201), sent.body


def _listed(gw, ident, me):
    got = gw.handle(_req("GET", "/v1/friends", token=ident.token(me, "t1")))
    assert got.status == 200, got.body
    return got.body["items"]


def test_the_list_reads_pinned_first_then_newest(tmp_path):
    gw, conn, ident = _host(tmp_path)
    try:
        for peer in ("alice", "bob", "carol"):
            _befriend(gw, ident, "user-1", peer)
        _say(gw, ident, "alice", "user-1", "oldest words")
        _say(gw, ident, "bob", "user-1", "newer words")
        # Newest speaks uppermost.
        items = _listed(gw, ident, "user-1")
        assert [i["peer"] for i in items][:2] == ["bob", "alice"]
        # Pinning alice lifts her above the newer conversation.
        pinned = gw.handle(
            _req(
                "PUT", "/v1/friends/alice/prefs",
                token=ident.token("user-1", "t1"), body={"pinned": True},
            )
        )
        assert pinned.status == 200 and pinned.body["pinned"] is True
        items = _listed(gw, ident, "user-1")
        assert [i["peer"] for i in items][:2] == ["alice", "bob"]
        assert items[0]["pinned"] is True and items[1]["pinned"] is False
    finally:
        conn.close()


def test_hide_is_a_moment_and_new_words_return_the_thread(tmp_path):
    gw, conn, ident = _host(tmp_path)
    try:
        _befriend(gw, ident, "user-1", "alice")
        _say(gw, ident, "alice", "user-1", "hello there")
        hidden = gw.handle(
            _req(
                "PUT", "/v1/friends/alice/prefs",
                token=ident.token("user-1", "t1"), body={"hidden": True},
            )
        )
        assert hidden.status == 200
        (item,) = _listed(gw, ident, "user-1")
        assert item["hidden"] is True
        # Alice speaks again: the thread returns by itself.
        _say(gw, ident, "alice", "user-1", "are you there?")
        (item,) = _listed(gw, ident, "user-1")
        assert item["hidden"] is False
    finally:
        conn.close()


def test_muted_rides_the_list_as_a_flag(tmp_path):
    gw, conn, ident = _host(tmp_path)
    try:
        _befriend(gw, ident, "user-1", "alice")
        muted = gw.handle(
            _req(
                "PUT", "/v1/friends/alice/prefs",
                token=ident.token("user-1", "t1"), body={"muted": True},
            )
        )
        assert muted.status == 200 and muted.body["muted"] is True
        (item,) = _listed(gw, ident, "user-1")
        assert item["muted"] is True
    finally:
        conn.close()


def test_delete_unfriends_and_the_thread_leaves_until_they_speak(tmp_path):
    gw, conn, ident = _host(tmp_path)
    try:
        _befriend(gw, ident, "user-1", "alice")
        _say(gw, ident, "alice", "user-1", "hello")
        gone = gw.handle(
            _req("DELETE", "/v1/friends/alice", token=ident.token("user-1", "t1"))
        )
        assert gone.status == 200 and gone.body["relationship"] == "none"
        (item,) = _listed(gw, ident, "user-1")
        assert item["hidden"] is True  # the frontend drops hidden threads
        # Alice's own list is untouched — the delete was one-sided.
        (theirs,) = _listed(gw, ident, "alice")
        assert theirs["hidden"] is False
        # She speaks again: the thread returns; the friendship does not.
        _say(gw, ident, "alice", "user-1", "hey?")
        (item,) = _listed(gw, ident, "user-1")
        assert item["hidden"] is False
        looked = gw.handle(
            _req(
                "POST", "/v1/friends/lookup",
                token=ident.token("user-1", "t1"), body={"query": "alice"},
            )
        )
        assert looked.body["relationship"] == "none"
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The Noder list carries the same margins, walled to the submitter.            #
# --------------------------------------------------------------------------- #
def test_run_threads_carry_margins_for_their_own_submitter(tmp_path):
    gw, conn, ident = _host(tmp_path)
    try:
        submitted = gw.handle(
            _req(
                "POST", "/v1/runs",
                token=ident.token("user-1", "t1"), body={"intent": "tidy csvs"},
            )
        )
        assert submitted.status in (200, 201, 202), submitted.body
        run_id = submitted.body["run_id"]

        listed = gw.handle(
            _req("GET", "/v1/runs", token=ident.token("user-1", "t1"))
        )
        (run,) = listed.body["items"]
        assert run["updated_at"]  # the sort key the sidebar orders by
        assert (run["pinned"], run["muted"], run["hidden"]) == (
            False, False, False,
        )

        pinned = gw.handle(
            _req(
                "PUT", f"/v1/runs/{run_id}/prefs",
                token=ident.token("user-1", "t1"),
                body={"pinned": True, "muted": True},
            )
        )
        assert pinned.status == 200, pinned.body
        listed = gw.handle(
            _req("GET", "/v1/runs", token=ident.token("user-1", "t1"))
        )
        (run,) = listed.body["items"]
        assert run["pinned"] is True and run["muted"] is True

        # Another account cannot reach my run's margins.
        walled = gw.handle(
            _req(
                "PUT", f"/v1/runs/{run_id}/prefs",
                token=ident.token("alice", "t1"), body={"pinned": True},
            )
        )
        assert walled.status == 404
    finally:
        conn.close()


def test_work_nodes_carry_margins_and_last_activity(tmp_path):
    """The Work list reads like Life: each node carries when it last
    moved, plus the owner's own pin/mute/hide margins — walled to the
    caller's desk."""
    from test_org_templates import _template_rig

    from oolu.social import FriendshipStore as FS

    app, conn, ident, desk, super_id, member_id, owner = _template_rig(tmp_path)
    try:
        app._friendships = FS(conn)
        listed = app.handle(_req("GET", "/v1/work/nodes", token=owner))
        assert listed.status == 200, listed.body
        entry = next(
            i for i in listed.body["items"] if i["node_id"] == super_id
        )
        assert (entry["pinned"], entry["muted"], entry["hidden"]) == (
            False, False, False,
        )
        assert "last_activity" in entry

        pinned = app.handle(
            _req(
                "PUT", f"/v1/work/nodes/{super_id}/prefs",
                token=owner, body={"pinned": True, "muted": True},
            )
        )
        assert pinned.status == 200, pinned.body
        listed = app.handle(_req("GET", "/v1/work/nodes", token=owner))
        entry = next(
            i for i in listed.body["items"] if i["node_id"] == super_id
        )
        assert entry["pinned"] is True and entry["muted"] is True

        # Hide = delete-from-list: hidden as it stands.
        hidden = app.handle(
            _req(
                "PUT", f"/v1/work/nodes/{super_id}/prefs",
                token=owner, body={"hidden": True},
            )
        )
        assert hidden.status == 200
        listed = app.handle(_req("GET", "/v1/work/nodes", token=owner))
        entry = next(
            i for i in listed.body["items"] if i["node_id"] == super_id
        )
        assert entry["hidden"] is True

        # Another account cannot reach my desk's margins.
        walled = app.handle(
            _req(
                "PUT", f"/v1/work/nodes/{super_id}/prefs",
                token=ident.token("stranger", "t1"), body={"pinned": True},
            )
        )
        assert walled.status == 404
    finally:
        conn.close()
