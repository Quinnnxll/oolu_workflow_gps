"""The agent roster (A0): the list below OoLu, per-agent threads, the byline.

Exit gate (agents-expansion plan, phase A0): the roster door lists the
named agents below OoLu; a roster agent's /v1/chat turn answers through
its OWN registered seat and lands in its OWN durable thread while the
OoLu thread stays untouched; a store born before the roster migrates in
place with its history intact; an account can set a profile photo any
tenant member may see next to the display name — the byline; and every
roster seat is registered, so no roster model call can ride unmetered.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from test_http_gateway import NOW as GATEWAY_NOW
from test_http_gateway import _app, _req

from oolu.chat import ModelUnavailable
from oolu.durable.connection import DurableConnection
from oolu.gateway import GatewayApp
from oolu.gateway.http import Request
from oolu.identity import LocalAccountService, LocalUserStore
from oolu.roster import ROSTER, agent_card, agent_turn, roster_items
from oolu.seats import SEATS
from oolu.social import (
    ASSISTANT_HISTORY_KEEP,
    MAX_PHOTO_BYTES,
    AssistantHistoryStore,
    ProfilePhotoStore,
)

NOW = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# The registry: cards and seats, one vocabulary.                               #
# --------------------------------------------------------------------------- #
def test_the_roster_lists_the_four_agents_in_order():
    assert [c.agent_id for c in ROSTER] == ["news", "poll", "explorer", "travel"]
    items = roster_items()
    assert [i["agent_id"] for i in items] == ["news", "poll", "explorer", "travel"]
    for item in items:
        # A card without an honest scope (or a named future) cannot ship.
        assert item["name"] and item["tagline"]
        assert item["scope"] and item["ahead"]


def test_every_roster_seat_is_registered():
    # The seat is the office: a card naming an unregistered seat would let
    # a roster model call ride unmetered — a defect, not a gap.
    for card in ROSTER:
        assert card.seat in SEATS, card.agent_id
        seat = SEATS[card.seat]
        assert seat.charge
        # A0 seats hold no hands and no drawer scopes — words only.
        assert seat.hands == () and seat.reads == () and seat.writes == ()


def test_agent_card_lookup_is_exact():
    assert agent_card("news") is not None
    assert agent_card("oolu") is None  # OoLu heads the list; not a roster entry
    assert agent_card("nope") is None
    assert agent_card("") is None


# --------------------------------------------------------------------------- #
# The roster turn: the card speaks; the model fills in the words.              #
# --------------------------------------------------------------------------- #
def test_a_model_less_turn_answers_from_the_card():
    card = agent_card("news")
    say, reasoning, source = agent_turn(card, "compose today's stories")
    assert source == "card" and reasoning is None
    assert card.scope in say and card.ahead in say


def test_a_dead_model_degrades_to_the_card_never_dies():
    class Dead:
        def reply(self, messages):
            raise ModelUnavailable("no brain today")

    card = agent_card("poll")
    say, _, source = agent_turn(card, "run a poll", model=Dead())
    assert source == "card" and card.scope in say


def test_the_model_turn_stands_in_the_cards_frame():
    seen = {}

    class Echo:
        def reply(self, messages):
            seen["messages"] = messages
            return "Happy to talk travel."

    card = agent_card("travel")
    say, _, source = agent_turn(
        card,
        "plan me a trip",
        history=[{"role": "user", "content": "earlier"}],
        model=Echo(),
        context="Current time: 2026-07-10 12:00 UTC",
    )
    assert source == "model" and say == "Happy to talk travel."
    frame = seen["messages"][0]
    assert frame["role"] == "system"
    # The card IS the boundary: scope, the named future, and the no-
    # invention rules all stand in the one system frame.
    assert card.scope in frame["content"] and card.ahead in frame["content"]
    assert "Never invent" in frame["content"]
    assert seen["messages"][1]["content"].startswith("Current time")
    assert seen["messages"][-1] == {"role": "user", "content": "plan me a trip"}


# --------------------------------------------------------------------------- #
# The store: one table, threads apart, migration in place.                     #
# --------------------------------------------------------------------------- #
def _history(tmp_path):
    conn = DurableConnection(tmp_path / "threads.db")
    return conn, AssistantHistoryStore(conn)


def test_each_agents_thread_stays_its_own(tmp_path):
    conn, store = _history(tmp_path)
    store.append(tenant="t1", principal="alice", kind="user", body="hi oolu")
    store.append(
        tenant="t1", principal="alice", kind="user", body="hi news", agent="news"
    )
    store.append(
        tenant="t1", principal="alice", kind="assistant", body="hello!", agent="news"
    )
    assert [t["body"] for t in store.history(tenant="t1", principal="alice")] == [
        "hi oolu"
    ]
    news = store.history(tenant="t1", principal="alice", agent="news")
    assert [t["body"] for t in news] == ["hi news", "hello!"]
    assert all(t["agent"] == "news" for t in news)
    # agent=None is the export's completeness read: every thread, labeled.
    everything = store.history(tenant="t1", principal="alice", agent=None)
    assert [t["agent"] for t in everything] == ["oolu", "news", "news"]
    conn.close()


def test_the_cap_falls_per_thread_not_across_threads(tmp_path):
    conn, store = _history(tmp_path)
    store.append(tenant="t1", principal="alice", kind="user", body="keep me")
    for i in range(ASSISTANT_HISTORY_KEEP + 5):
        store.append(
            tenant="t1", principal="alice", kind="user", body=f"n{i}", agent="news"
        )
    # A chatty News never shortens the OoLu thread...
    assert [t["body"] for t in store.history(tenant="t1", principal="alice")] == [
        "keep me"
    ]
    # ...and its own thread holds exactly the keep window.
    news = store.history(
        tenant="t1", principal="alice", agent="news", limit=10_000
    )
    assert len(news) == ASSISTANT_HISTORY_KEEP
    assert news[-1]["body"] == f"n{ASSISTANT_HISTORY_KEEP + 4}"
    conn.close()


def test_a_pre_roster_table_migrates_with_its_history_intact(tmp_path):
    conn = DurableConnection(tmp_path / "old.db")
    # A table born before the roster: no agent column, one OoLu thread.
    with conn.transaction() as db:
        db.execute(
            """CREATE TABLE assistant_turns (
                tenant_id TEXT NOT NULL,
                principal TEXT NOT NULL,
                seq INTEGER NOT NULL,
                kind TEXT NOT NULL,
                body TEXT NOT NULL,
                at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, principal, seq)
            )"""
        )
        db.execute(
            "INSERT INTO assistant_turns VALUES (?, ?, ?, ?, ?, ?)",
            ("t1", "alice", 1, "user", "from before", NOW.isoformat()),
        )
    store = AssistantHistoryStore(conn)
    # The old rows read as OoLu's thread, exactly as they were written...
    assert [
        t["body"] for t in store.history(tenant="t1", principal="alice")
    ] == ["from before"]
    # ...and the migrated table takes tagged turns like a fresh one.
    store.append(
        tenant="t1", principal="alice", kind="user", body="new", agent="poll"
    )
    assert [
        t["body"]
        for t in store.history(tenant="t1", principal="alice", agent="poll")
    ] == ["new"]
    conn.close()


def test_a_turn_must_name_its_thread(tmp_path):
    conn, store = _history(tmp_path)
    with pytest.raises(ValueError, match="agent"):
        store.append(
            tenant="t1", principal="alice", kind="user", body="x", agent="  "
        )
    conn.close()


# --------------------------------------------------------------------------- #
# The photo store: a face, not a gallery.                                      #
# --------------------------------------------------------------------------- #
def test_the_photo_store_round_trips_and_bounds(tmp_path):
    conn = DurableConnection(tmp_path / "faces.db")
    store = ProfilePhotoStore(conn)
    store.save(
        tenant="t1", principal="alice", media_type="image/png", data=b"\x89PNG..."
    )
    media_type, data = store.get(tenant="t1", principal="alice")
    assert media_type == "image/png" and data == b"\x89PNG..."
    # Replacement is whole, never a patch.
    store.save(
        tenant="t1", principal="alice", media_type="image/jpeg", data=b"\xff\xd8"
    )
    assert store.get(tenant="t1", principal="alice")[0] == "image/jpeg"
    with pytest.raises(ValueError, match="too large"):
        store.save(
            tenant="t1",
            principal="alice",
            media_type="image/png",
            data=b"x" * (MAX_PHOTO_BYTES + 1),
        )
    with pytest.raises(ValueError, match="image"):
        store.save(
            tenant="t1", principal="alice", media_type="text/html", data=b"<b>"
        )
    assert store.remove(tenant="t1", principal="alice") is True
    assert store.get(tenant="t1", principal="alice") is None
    assert store.remove(tenant="t1", principal="alice") is False
    conn.close()


# --------------------------------------------------------------------------- #
# The gateway: the doors end to end.                                           #
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
        assistant_history=AssistantHistoryStore(conn),
        profile_photos=ProfilePhotoStore(conn),
    )
    return gateway, conn, ident


def test_the_roster_door_lists_the_agents(tmp_path):
    gateway, conn, ident = _host(tmp_path)
    token = ident.token("alice")
    listing = gateway.handle(_req("GET", "/v1/roster", token=token))
    assert listing.status == 200
    assert [i["agent_id"] for i in listing.body["items"]] == [
        "news",
        "poll",
        "explorer",
        "travel",
    ]
    conn.close()


def test_a_roster_turn_lands_in_its_own_thread(tmp_path):
    gateway, conn, ident = _host(tmp_path)
    token = ident.token("alice")
    # Model-less host: the agent answers from its card — honestly scoped,
    # never dead — and the turn still lands in the news thread.
    reply = gateway.handle(
        _req(
            "POST",
            "/v1/chat",
            token=token,
            body={"message": "make me a story", "agent": "news"},
        )
    )
    assert reply.status == 200
    assert reply.body["agent"] == "news" and reply.body["source"] == "card"
    assert reply.body["run_id"] is None
    assert "phase A1" in reply.body["reply"]  # the honest future, named

    news = gateway.handle(
        _req("GET", "/v1/chat/history", token=token, query={"agent": "news"})
    )
    assert [t["kind"] for t in news.body["items"]] == ["user", "assistant"]
    # The OoLu thread is untouched by the roster turn.
    oolu = gateway.handle(_req("GET", "/v1/chat/history", token=token))
    assert oolu.body["items"] == []

    unknown = gateway.handle(
        _req(
            "POST",
            "/v1/chat",
            token=token,
            body={"message": "hi", "agent": "nope"},
        )
    )
    assert unknown.status == 400
    bad_history = gateway.handle(
        _req("GET", "/v1/chat/history", token=token, query={"agent": "nope"})
    )
    assert bad_history.status == 400
    conn.close()


def _upload(path, *, token, media_type, payload):
    # The body IS the file: uploads ride Request.raw, exactly as picked.
    return Request(
        method="POST",
        path=path,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": media_type,
        },
        raw=payload,
        now=GATEWAY_NOW,
    )


def test_the_byline_photo_flow_end_to_end(tmp_path):
    gateway, conn, ident = _host(tmp_path)
    alice, bob = ident.token("alice"), ident.token("bob")
    # Alice sets her face; the body IS the image.
    saved = gateway.handle(
        _upload(
            "/v1/profile/photo",
            token=alice,
            media_type="image/png",
            payload=b"\x89PNG-alice",
        )
    )
    assert saved.status == 201 and saved.body["media_type"] == "image/png"
    # Bob — same tenant — sees the byline: name and face.
    profile = gateway.handle(_req("GET", "/v1/profiles/alice", token=bob))
    assert profile.status == 200 and profile.body["has_photo"] is True
    photo = gateway.handle(_req("GET", "/v1/profiles/alice/photo", token=bob))
    assert photo.status == 200 and photo.body == b"\x89PNG-alice"
    assert photo.content_type == "image/png"
    # A stranger's name 404s; an oversized photo 413s; removal sticks.
    assert (
        gateway.handle(_req("GET", "/v1/profiles/mallory", token=bob)).status
        == 404
    )
    huge = gateway.handle(
        _upload(
            "/v1/profile/photo",
            token=alice,
            media_type="image/png",
            payload=b"x" * (MAX_PHOTO_BYTES + 1),
        )
    )
    assert huge.status == 413
    removed = gateway.handle(_req("DELETE", "/v1/profile/photo", token=alice))
    assert removed.status == 200 and removed.body["removed"] is True
    assert (
        gateway.handle(_req("GET", "/v1/profiles/alice/photo", token=bob)).status
        == 404
    )
    conn.close()
