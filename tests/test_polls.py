"""The poll floor (A3): fun on the surface, instruments underneath.

Exit gate (agents-expansion plan, phase A3): results are invisible until
the member votes and below-floor aggregates refuse to render (property
test at the floor boundary); a vote is one durable idempotent event and
replaying changes nothing; revoking learning consent stops future use
and the export path passes scrubbing; seeded votes measurably reorder
that member's edition (end-to-end through the preference store);
exported pairs validate against the DPO dataset shape; and both poll
objects carry their contributor bylines.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest
from test_http_gateway import _app, _req

from oolu.durable.connection import DurableConnection
from oolu.gateway import GatewayApp
from oolu.identity import LocalAccountService, LocalUserStore
from oolu.knowledge.scrubbing import is_safe_to_store
from oolu.press import (
    K_FLOOR,
    LICENSES,
    ContributionStore,
    PairwiseStore,
    PollDesk,
    PollStore,
    PreferenceStore,
    PressDesk,
    PressError,
    StoryStore,
    comparable,
)
from oolu.settings_node import SettingsNode, SettingsStore

NOW = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
LICENSE = next(iter(LICENSES))

KETTLE_A = (
    "The steel kettle boiled a full litre in four minutes flat and the "
    "handle stayed cool; the lid rattles a little at full boil."
)
KETTLE_B = (
    "This glass kettle takes six minutes for a litre but the handle and "
    "lid feel solid; watching the boil is half the fun."
)
VALLEY = (
    "Three months dry, and this morning the terraces flooded with runoff "
    "after the storm finally reached the valley."
)


def _press(tmp_path):
    conn = DurableConnection(tmp_path / "polls.db")
    tick = {"now": NOW}

    def clock():
        tick["now"] += timedelta(seconds=1)
        return tick["now"]

    desk = PressDesk(
        ContributionStore(conn, clock=clock),
        stories=StoryStore(conn, clock=clock),
        preferences=PreferenceStore(conn, clock=clock),
        polls=PollDesk(
            PollStore(conn, clock=clock),
            PairwiseStore(conn, clock=clock),
            rng=random.Random(7),
        ),
    )
    return conn, desk


def _publish(desk, author, title, body, genres=("products",)):
    return desk.publish(
        tenant="t1",
        author=author,
        title=title,
        body=body,
        genres=genres,
        license=LICENSE,
        consent=True,
    )


def _seed_pair(desk):
    a = _publish(desk, "alice", "The steel kettle, tested", KETTLE_A)
    b = _publish(desk, "bob", "A week with the glass kettle", KETTLE_B)
    corpus = desk.store.list(tenant="t1")
    pair = desk.polls.next_pair(
        tenant="t1", principal="carol", corpus=corpus, genre="products"
    )
    return a, b, pair


# --------------------------------------------------------------------------- #
# Mining: comparable means something.                                          #
# --------------------------------------------------------------------------- #
def test_comparable_needs_distinct_authors_and_the_band(tmp_path):
    conn, desk = _press(tmp_path)
    a = _publish(desk, "alice", "The steel kettle, tested", KETTLE_A)
    same_author = _publish(desk, "alice", "Steel kettle, day two", KETTLE_B)
    unrelated = _publish(
        desk, "bob", "Rain reached the valley", VALLEY, genres=("science",)
    )
    b = _publish(desk, "bob", "A week with the glass kettle", KETTLE_B)
    assert comparable(a, b) is True
    assert comparable(a, same_author) is False  # one member, no contest
    assert comparable(a, unrelated) is False  # no shared subject
    conn.close()


def test_the_pair_carries_both_bylines_and_the_genre(tmp_path):
    conn, desk = _press(tmp_path)
    a, b, pair = _seed_pair(desk)
    assert {pair.left.author, pair.right.author} == {"alice", "bob"}
    assert {pair.left.contribution_id, pair.right.contribution_id} == {
        a.contribution_id,
        b.contribution_id,
    }
    assert pair.genre == "products"
    assert pair.left.title and pair.left.excerpt
    # Served again to the same member before voting: the SAME standing
    # pair, never a duplicate mint.
    again = desk.polls.next_pair(
        tenant="t1",
        principal="carol",
        corpus=desk.store.list(tenant="t1"),
        genre="products",
    )
    assert again.pair_id == pair.pair_id
    conn.close()


def test_an_empty_genre_refuses_honestly(tmp_path):
    conn, desk = _press(tmp_path)
    _publish(desk, "alice", "The steel kettle, tested", KETTLE_A)
    with pytest.raises(PressError, match="not enough comparable"):
        desk.polls.next_pair(
            tenant="t1",
            principal="carol",
            corpus=desk.store.list(tenant="t1"),
            genre="products",
        )
    conn.close()


# --------------------------------------------------------------------------- #
# Voting: once, idempotent; the floor holds at its boundary.                   #
# --------------------------------------------------------------------------- #
def test_votes_are_idempotent_and_a_change_of_heart_is_refused(tmp_path):
    conn, desk = _press(tmp_path)
    _, _, pair = _seed_pair(desk)
    first = desk.polls.vote(
        pair.pair_id, tenant="t1", principal="carol", choice="left"
    )
    assert first["voted"] is True and first["revealed"] is False
    # Replaying the same vote changes nothing — same answer, one row.
    again = desk.polls.vote(
        pair.pair_id, tenant="t1", principal="carol", choice="left"
    )
    assert again == first
    assert desk.polls.store.counts(pair.pair_id, tenant="t1") == {
        "left": 1,
        "right": 0,
    }
    with pytest.raises(PressError, match="already stands") as refusal:
        desk.polls.vote(
            pair.pair_id, tenant="t1", principal="carol", choice="right"
        )
    assert refusal.value.status == 409
    conn.close()


def test_the_floor_holds_exactly_at_its_boundary(tmp_path):
    conn, desk = _press(tmp_path)
    _, _, pair = _seed_pair(desk)
    # K_FLOOR - 1 votes: every voter sees "not enough votes yet" and no
    # counts — not even a total to reverse a neighbor's choice from.
    for i in range(K_FLOOR - 1):
        verdict = desk.polls.vote(
            pair.pair_id,
            tenant="t1",
            principal=f"member-{i}",
            choice="left" if i % 2 == 0 else "right",
        )
        assert verdict["revealed"] is False
        assert "counts" not in verdict and "total" not in verdict
    # A non-voter sees nothing either way.
    outsider = desk.polls.reveal(
        pair.pair_id, tenant="t1", principal="outsider"
    )
    assert outsider["voted"] is False and outsider["revealed"] is False
    # The K-th vote crosses the floor: real numbers, for voters only.
    crossing = desk.polls.vote(
        pair.pair_id, tenant="t1", principal="member-last", choice="left"
    )
    assert crossing["revealed"] is True
    assert crossing["total"] == K_FLOOR
    assert crossing["counts"]["left"] + crossing["counts"]["right"] == K_FLOOR
    # Still nothing for the non-voter: vote first, see second.
    assert (
        desk.polls.reveal(pair.pair_id, tenant="t1", principal="outsider")[
            "revealed"
        ]
        is False
    )
    conn.close()


# --------------------------------------------------------------------------- #
# The instruments: consent writes, export is DPO-shaped and scrubbed.          #
# --------------------------------------------------------------------------- #
def test_learning_writes_only_under_consent_and_exports_dpo_shape(tmp_path):
    conn, desk = _press(tmp_path)
    _, _, pair = _seed_pair(desk)
    # No consent: the vote counts, the instrument stays silent.
    desk.polls.vote(
        pair.pair_id, tenant="t1", principal="carol", choice="left",
        learning=False,
    )
    assert (
        desk.polls.pairwise.export(tenant="t1", principal="carol") == []
    )
    # Consent: the pair lands in the trainer's own vocabulary — the
    # chosen side is the side dana actually picked.
    desk.polls.vote(
        pair.pair_id, tenant="t1", principal="dana", choice="right",
        learning=True,
    )
    [exported] = desk.polls.pairwise.export(tenant="t1", principal="dana")
    assert set(exported) == {"prompt", "chosen", "rejected"}
    assert exported["chosen"].startswith(pair.right.title)
    assert exported["rejected"].startswith(pair.left.title)
    for field in exported.values():
        assert is_safe_to_store(field)  # the export path passes scrubbing
    # Erasure: the member's instruments, gone.
    assert desk.polls.pairwise.erase(tenant="t1", principal="dana") == 1
    assert desk.polls.pairwise.export(tenant="t1", principal="dana") == []
    conn.close()


def test_the_thompson_draw_prefers_the_genre_that_earns_votes(tmp_path):
    conn, desk = _press(tmp_path)
    store = desk.polls.store
    # products: heavily served, heavily voted. science: served, ignored.
    store.bump_genre(tenant="t1", genre="products", served=20, voted=18)
    store.bump_genre(tenant="t1", genre="science", served=20, voted=1)
    picks = {
        desk.polls.pick_genre(
            tenant="t1", candidates=["products", "science"]
        )
        for _ in range(20)
    }
    assert "products" in picks  # the earner surfaces
    conn.close()


# --------------------------------------------------------------------------- #
# The gateway: the doors, consent at the edge, the edition feels the vote.     #
# --------------------------------------------------------------------------- #
def _host(tmp_path):
    app, conn, ident = _app(tmp_path)
    users = LocalUserStore(":memory:")
    accounts = LocalAccountService(users, ident.store, ident._signer)
    for name in ("alice", "bob", "carol"):
        accounts.create_user(name, f"{name}-password-1", tenant="t1")
    press = PressDesk(
        ContributionStore(conn),
        stories=StoryStore(conn),
        preferences=PreferenceStore(conn),
        polls=PollDesk(
            PollStore(conn), PairwiseStore(conn), rng=random.Random(7)
        ),
    )
    gateway = GatewayApp(
        app._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        accounts=accounts,
        settings_node=SettingsNode(SettingsStore(conn)),
        press=press,
    )
    return gateway, conn, ident


def _contribute(gateway, token, title, body, genres):
    published = gateway.handle(
        _req(
            "POST",
            "/v1/press/contributions",
            token=token,
            body={
                "title": title,
                "body": body,
                "genres": genres,
                "license": LICENSE,
                "consent": True,
            },
        )
    )
    assert published.status == 201, published.body
    return published.body


def test_the_poll_doors_end_to_end_and_the_edition_feels_the_vote(tmp_path):
    gateway, conn, ident = _host(tmp_path)
    alice, bob, carol = (
        ident.token("alice"),
        ident.token("bob"),
        ident.token("carol"),
    )
    _contribute(gateway, alice, "The steel kettle, tested", KETTLE_A, ["products"])
    _contribute(gateway, bob, "A week with the glass kettle", KETTLE_B, ["products"])
    _contribute(gateway, bob, "Rain reached the valley", VALLEY, ["science"])

    # The named genre IS the stream — the switch is immediate.
    served = gateway.handle(
        _req(
            "GET",
            "/v1/press/polls/next",
            token=carol,
            query={"genre": "products"},
        )
    )
    assert served.status == 200
    pair = served.body
    assert {pair["left"]["author"], pair["right"]["author"]} == {
        "alice",
        "bob",
    }  # both bylines ride the pair
    unknown = gateway.handle(
        _req(
            "GET",
            "/v1/press/polls/next",
            token=carol,
            query={"genre": "crypto"},
        )
    )
    assert unknown.status == 400

    # Carol consents; her vote counts AND becomes her instruments.
    assert (
        gateway.handle(
            _req(
                "PUT",
                "/v1/settings",
                token=carol,
                body={"changes": {"press.personalize": True}},
            )
        ).status
        == 200
    )
    voted = gateway.handle(
        _req(
            "POST",
            f"/v1/press/polls/{pair['pair_id']}/vote",
            token=carol,
            body={"choice": "left"},
        )
    )
    assert voted.status == 200
    assert voted.body["voted"] is True and voted.body["learning"] is True
    assert voted.body["revealed"] is False  # below the floor

    # The stats door holds the same law for her, and refuses nothing
    # noisier than "not enough votes yet".
    stats = gateway.handle(
        _req(
            "GET", f"/v1/press/polls/{pair['pair_id']}/stats", token=carol
        )
    )
    assert stats.body["revealed"] is False
    assert stats.body["reason"] == "not enough votes yet"

    # Her consented export is DPO-shaped; bob (no consent) is refused.
    exported = gateway.handle(
        _req("GET", "/v1/press/preferences/export", token=carol)
    )
    assert exported.status == 200 and exported.body["count"] == 1
    assert set(exported.body["items"][0]) == {"prompt", "chosen", "rejected"}
    assert (
        gateway.handle(
            _req("GET", "/v1/press/preferences/export", token=bob)
        ).status
        == 403
    )

    # And the vote reaches her EDITION: compose stories, read her
    # ranking — the products story outranks science for her now.
    assert (
        gateway.handle(
            _req("POST", "/v1/press/newsroom/run", token=alice)
        ).body["composed"]
        >= 2
    )
    edition = gateway.handle(_req("GET", "/v1/press/stories", token=carol))
    assert edition.body["personalized"] is True
    genres_in_order = [i["genres"] for i in edition.body["items"]]
    assert "products" in genres_in_order[0]
    conn.close()


def test_the_import_scan_holds_over_the_poll_floor():
    from pathlib import Path

    package = Path(__file__).resolve().parents[1] / "src" / "oolu" / "press"
    assert (package / "polls.py").exists()
    forbidden = ("http", "urllib", "requests", "socket", "providers")
    for line in (package / "polls.py").read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith(("from ", "import ")):
            for word in forbidden:
                assert word not in stripped, stripped
