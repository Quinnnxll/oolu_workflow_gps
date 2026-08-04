"""The topic desk (N2): WHAT to report, decided on typed evidence.

Exit gate (news-agent-benchmark-roadmap, phase N2): every topic brief
resolves to typed marketplace/contribution evidence (a brief without
facts cannot be stored); an advertiser-adjacent topic carries its
disclosure FROM BIRTH — stamped on the candidate at mining time, never
bolted on at render; the selection breakdown (demand, evidence,
freshness) is stored and reproducible; the miners' floors are honest
(a neutral no-evidence factor never manufactures a gap, a trust score
without a real order book stays silent); and the import scan still
holds — the desk decides with no model call and no marketplace import
inside the press package.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from test_http_gateway import _app, _req

from oolu.durable.connection import DurableConnection
from oolu.gateway import GatewayApp
from oolu.identity import LocalAccountService, LocalUserStore
from oolu.press import (
    BeatRow,
    ClusterPiece,
    ContributionStore,
    GenreDemandStore,
    IntakeStore,
    PreferenceStore,
    PressDesk,
    PressError,
    StoryMetricsStore,
    StoryStore,
    TopicBrief,
    TopicBriefStore,
    mine_clusters,
    mine_measured_gaps,
    mine_price_moves,
    mine_trust_bands,
    select_topics,
    topics_line,
)
from oolu.settings_node import SettingsNode, SettingsStore
from oolu.social import AssistantHistoryStore

NOW = datetime(2026, 8, 4, 8, 0, tzinfo=UTC)


def _row(**over) -> BeatRow:
    base = dict(
        listing_id="l1",
        title="The steel kettle",
        seller="kettleworks",
        price_micros=27_000_000,
        list_price_micros=None,
        discount_percent=None,
        feedback={"count": 0, "mean": None, "factor": 0.5},
        trust={
            "score": 0.6,
            "finished": 0,
            "refunded": 0,
            "disputed": 0,
            "basis": "thin",
        },
        lab={"count": 0, "mean_score": None, "factor": 0.5},
    )
    base.update(over)
    return BeatRow(**base)


# --------------------------------------------------------------------------- #
# The miners: typed events past honest floors.                                 #
# --------------------------------------------------------------------------- #
def test_price_moves_mine_the_discount_fact_past_the_floor():
    rows = [
        _row(listing_id="l1", list_price_micros=30_000_000, discount_percent=10),
        _row(listing_id="l2", list_price_micros=28_000_000, discount_percent=4),
        _row(listing_id="l3"),  # no list price: no fact, no story
    ]
    found = mine_price_moves(rows, now=NOW)
    assert [c.key for c in found] == ["price:l1"]
    assert found[0].genre == "products"
    kinds = [f.kind for f in found[0].facts]
    assert kinds == ["listing", "price"] and found[0].facts[1].value == 10.0


def test_the_disclosure_is_born_with_the_candidate():
    advertiser = _row(
        listing_id="l1",
        list_price_micros=30_000_000,
        discount_percent=15,
        advertiser=True,
    )
    promoted = _row(
        listing_id="l2",
        list_price_micros=30_000_000,
        discount_percent=15,
        promoted=True,
    )
    clean = _row(
        listing_id="l3", list_price_micros=30_000_000, discount_percent=15
    )
    found = {c.key: c for c in mine_price_moves(
        [advertiser, promoted, clean], now=NOW
    )}
    assert "runs active advertising" in found["price:l1"].disclosure
    assert "target of an active advertising campaign" in found["price:l2"].disclosure
    assert found["price:l3"].disclosure == ""
    # The flag survives selection verbatim — from birth to brief.
    briefs = select_topics(list(found.values()), demand_rank={}, now=NOW)
    assert any("advertising" in b.disclosure for b in briefs)


def test_trust_bands_need_a_real_book_behind_the_number():
    silent_thin = _row(trust={"score": 0.2, "finished": 1, "refunded": 1,
                              "disputed": 0, "basis": "thin"})
    concern = _row(
        listing_id="l2",
        trust={"score": 0.3, "finished": 1, "refunded": 1, "disputed": 1,
               "basis": "book"},
    )
    proven = _row(
        listing_id="l3",
        seller="steady",
        trust={"score": 0.9, "finished": 6, "refunded": 0, "disputed": 0,
               "basis": "book"},
    )
    mid = _row(listing_id="l4", trust={"score": 0.6, "finished": 9,
                                       "refunded": 0, "disputed": 0,
                                       "basis": "book"})
    found = {c.key: c for c in mine_trust_bands(
        [silent_thin, concern, proven, mid], now=NOW
    )}
    # Two orders is no book: the concerning score stays honestly silent.
    assert "trust:l1" not in found
    assert "trust concern" in found["trust:l2"].subject
    assert "proven record" in found["trust:l3"].subject
    assert "trust:l4" not in found  # mid-band: no story either way


def test_the_measured_gap_needs_real_evidence_on_both_sides():
    no_lab = _row(feedback={"count": 4, "mean": 4.8, "factor": 0.96})
    split = _row(
        listing_id="l2",
        feedback={"count": 4, "mean": 4.8, "factor": 0.96},
        lab={"count": 2, "mean_score": 40.0, "factor": 0.4},
    )
    agreeing = _row(
        listing_id="l3",
        feedback={"count": 4, "mean": 4.0, "factor": 0.8},
        lab={"count": 2, "mean_score": 75.0, "factor": 0.75},
    )
    found = {c.key: c for c in mine_measured_gaps(
        [no_lab, split, agreeing], now=NOW
    )}
    # The neutral 0.5 of "no lab reports" never manufactures a gap.
    assert list(found) == ["measured:l2"]
    assert found["measured:l2"].genre == "results"
    assert {f.kind for f in found["measured:l2"].facts} == {
        "listing", "feedback", "lab",
    }


HARBOR = (
    "After two years of repairs the harbor market reopened this morning "
    "with forty stalls and a queue down the pier."
)
HARBOR_AGREE = (
    "The harbor market reopened today — forty stalls, and the queue down "
    "the pier said everything about the wait."
)
VALLEY = (
    "Three months dry, and this morning the terraces flooded with runoff "
    "after the storm finally reached the valley."
)


def _piece(pid, author, title, text, *, hours=0, genre="local"):
    return ClusterPiece(
        contribution_id=pid,
        author=author,
        genre=genre,
        title=title,
        text=text,
        created_at=NOW + timedelta(hours=hours),
    )


def test_clusters_need_independent_voices_and_never_twin():
    pieces = [
        _piece("c1", "alice", "Harbor market reopened", HARBOR),
        _piece("c2", "bob", "Market back at the harbor", HARBOR_AGREE, hours=1),
        _piece("c3", "alice", "Rain reached the valley", VALLEY, hours=2),
    ]
    found = mine_clusters(pieces)
    # One cluster: the harbor subject, anchored on the FIRST telling,
    # both voices in the evidence. Alice's valley piece stands alone.
    assert [c.key for c in found] == ["cluster:c1"]
    assert "2 members are telling the same story" in found[0].subject
    assert {f.ref for f in found[0].facts} == {"c1", "c2"}
    assert found[0].fresh_at == NOW + timedelta(hours=1)
    # A second run over the same shelf mints the same cluster, not a twin.
    assert [c.key for c in mine_clusters(pieces)] == ["cluster:c1"]


def test_one_author_retelling_themselves_is_no_cluster():
    pieces = [
        _piece("c1", "alice", "Harbor market reopened", HARBOR),
        _piece("c2", "alice", "Market back at the harbor", HARBOR_AGREE),
    ]
    assert mine_clusters(pieces) == []


# --------------------------------------------------------------------------- #
# Selection: named factors, reproducible, demand-steered.                      #
# --------------------------------------------------------------------------- #
def test_selection_is_reproducible_and_demand_steers_it():
    rows = [
        _row(listing_id="l1", list_price_micros=30_000_000,
             discount_percent=12),
    ]
    cluster = mine_clusters(
        [
            _piece("c1", "alice", "Harbor market reopened", HARBOR),
            _piece("c2", "bob", "Market back at the harbor", HARBOR_AGREE),
        ]
    )
    candidates = [*mine_price_moves(rows, now=NOW), *cluster]
    # Demand leads with "local": the member cluster outranks the deal.
    local_first = select_topics(
        candidates, demand_rank={"local": 1, "products": 2}, now=NOW
    )
    assert [b.genre for b in local_first] == ["local", "products"]
    assert local_first == select_topics(
        candidates, demand_rank={"local": 1, "products": 2}, now=NOW
    )
    # Flip the reading: the products deal leads.
    products_first = select_topics(
        candidates, demand_rank={"products": 1, "local": 2}, now=NOW
    )
    assert [b.genre for b in products_first] == ["products", "local"]
    for brief in products_first:
        assert set(brief.factors) == {"demand", "evidence", "freshness"}
        assert brief.facts  # provenance, always
    # Ranks are 1..N in every reading.
    assert [b.rank for b in products_first] == [1, 2]


def test_the_slate_words_speak_every_disclosure():
    brief = TopicBrief(
        key="price:l1",
        genre="products",
        subject="The steel kettle: 15% off its recorded list price",
        facts=(
            # one typed fact is enough for words — the store enforces
            # the real law
            *select_topics(
                mine_price_moves(
                    [_row(list_price_micros=30_000_000, discount_percent=15,
                          advertiser=True)],
                    now=NOW,
                ),
                demand_rank={},
                now=NOW,
            )[0].facts,
        ),
        disclosure="Disclosure: seller kettleworks runs active advertising "
        "on this platform.",
        rank=1,
        score=0.5,
        factors={},
    )
    words = topics_line([brief])
    assert "1. The steel kettle" in words
    assert "Disclosure: seller kettleworks" in words


# --------------------------------------------------------------------------- #
# The store: provenance mandatory, whole vintages.                             #
# --------------------------------------------------------------------------- #
def test_a_brief_without_facts_cannot_be_stored(tmp_path):
    conn = DurableConnection(tmp_path / "topics.db")
    store = TopicBriefStore(conn)
    bare = TopicBrief(
        key="price:l1", genre="products", subject="s", facts=(),
        disclosure="", rank=1, score=0.5, factors={},
    )
    with pytest.raises(PressError, match="provenance"):
        store.record(tenant="t1", briefs=[bare])
    # The refusal left NOTHING half-written.
    assert store.reading(tenant="t1") == []
    conn.close()


def test_the_slate_replaces_whole(tmp_path):
    conn = DurableConnection(tmp_path / "topics.db")
    store = TopicBriefStore(conn)
    first = select_topics(
        mine_price_moves(
            [_row(list_price_micros=30_000_000, discount_percent=15)],
            now=NOW,
        ),
        demand_rank={},
        now=NOW,
    )
    store.record(tenant="t1", briefs=first)
    store.record(tenant="t1", briefs=[])
    # An honest empty reading replaced the deal — never a stale slate.
    assert store.reading(tenant="t1") == []
    conn.close()


# --------------------------------------------------------------------------- #
# The gateway: the door and the News desk speak the slate.                     #
# --------------------------------------------------------------------------- #
def _host(tmp_path):
    app, conn, ident = _app(tmp_path)
    users = LocalUserStore(":memory:")
    accounts = LocalAccountService(users, ident.store, ident._signer)
    for name in ("alice", "bob"):
        accounts.create_user(name, f"{name}-password-1", tenant="t1")
    press = PressDesk(
        ContributionStore(conn),
        stories=StoryStore(conn),
        preferences=PreferenceStore(conn),
        intake=IntakeStore(conn),
        metrics=StoryMetricsStore(conn),
        demand=GenreDemandStore(conn),
        topics=TopicBriefStore(conn),
    )
    gateway = GatewayApp(
        app._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        accounts=accounts,
        settings_node=SettingsNode(SettingsStore(conn)),
        assistant_history=AssistantHistoryStore(conn),
        press=press,
    )
    return gateway, conn, ident, press


def test_the_topics_door_and_the_news_desk_speak_the_slate(tmp_path):
    gateway, conn, ident, press = _host(tmp_path)
    for author, title, body in (
        ("alice", "Harbor market reopened", HARBOR),
        ("bob", "Market back at the harbor", HARBOR_AGREE),
    ):
        press.publish(
            tenant="t1",
            author=author,
            title=title,
            body=body,
            genres=("local",),
            license="oolu-members-1",
            consent=True,
        )
    alice = ident.token("alice")
    slate = gateway.handle(_req("GET", "/v1/press/topics", token=alice))
    assert slate.status == 200
    items = slate.body["items"]
    assert len(items) == 1 and items[0]["topic_key"].startswith("cluster:")
    assert items[0]["genre"] == "local"
    assert {f["kind"] for f in items[0]["facts"]} == {"contribution"}
    assert set(items[0]["factors"]) == {"demand", "evidence", "freshness"}
    assert items[0]["disclosure"] == ""

    spoken = gateway.handle(
        _req(
            "POST",
            "/v1/chat",
            token=alice,
            body={"message": "topics", "agent": "news"},
        )
    )
    assert spoken.status == 200
    assert "The slate, on evidence:" in spoken.body["reply"]
    assert "telling the same story" in spoken.body["reply"]
    conn.close()
