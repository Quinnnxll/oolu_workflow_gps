"""The genre desk (N1 v2): which genre are readers interested in, on evidence.

Exit gate (news-agent-benchmark-roadmap, phase N1, amended): the
ranking THOMPSON-SAMPLES the completion posterior — cold start
explores by construction, an unlucky early record keeps earning
re-tests, and the desk never locks into a cumulative suboptimal
choice — while staying auditable: the same seed replays the same
reading exactly, and the gateway records every reading's seed. Every
rank carries its factor breakdown and raw evidence; below the reader
floor the row is flagged ``explored`` (ranked on a draw, honestly
named); rng=None is the deterministic posterior-mean reading; the
interest book is anonymous by schema (no principal column exists to
leak); and no model call happens anywhere in the decision — the desk
answers on a host with no brain at all.
"""

from __future__ import annotations

import random

from test_http_gateway import _app, _req

from oolu.durable.connection import DurableConnection
from oolu.gateway import GatewayApp
from oolu.identity import LocalAccountService, LocalUserStore
from oolu.press import (
    DEMAND_READER_FLOOR,
    ContributionStore,
    GenreDemandStore,
    GenreEvidence,
    IntakeStore,
    PreferenceStore,
    PressDesk,
    StoryMetricsStore,
    StoryStore,
    demand_line,
    rank_demand,
)
from oolu.settings_node import SettingsNode, SettingsStore
from oolu.social import AssistantHistoryStore

GENRE_SET = ("food", "local", "science")


# --------------------------------------------------------------------------- #
# The ranking: deterministic, explained, floored.                              #
# --------------------------------------------------------------------------- #
def _evidenced(readers=DEMAND_READER_FLOOR, opens=None, completions=0,
               dwell_ms=0, likes=0):
    return GenreEvidence(
        readers=readers,
        opens=opens if opens is not None else readers,
        completions=completions,
        dwell_ms=dwell_ms,
        likes=likes,
    )


def test_a_seeded_reading_replays_exactly_and_explains_itself():
    inputs = dict(
        genres=GENRE_SET,
        engagement={
            "food": _evidenced(completions=4, dwell_ms=50_000, likes=3),
            "local": _evidenced(completions=1, dwell_ms=20_000),
        },
        taps={"food": 2, "science": 6},
        supply={"food": 4, "local": 2, "science": 1},
    )
    first = rank_demand(**inputs, rng=random.Random(7))
    replay = rank_demand(**inputs, rng=random.Random(7))
    # Stored inputs + stored seed = the SAME reading — auditable
    # stochasticity, the amended reproducibility law.
    assert first == replay
    assert [d.rank for d in first] == [1, 2, 3]
    for item in first:
        assert set(item.factors) == {"engagement", "interest", "supply"}
        assert isinstance(item.factors["engagement"], float)
        assert set(item.evidence) == {
            "readers", "opens", "completions", "likes", "pushed",
            "taps", "pieces",
        }
    # Below the reader floor the rank rests on a draw — and says so.
    by_genre = {d.genre: d for d in first}
    assert by_genre["food"].explored is False
    assert by_genre["science"].explored is True
    # A different seed may order differently: the draw is the draw.
    other = rank_demand(**inputs, rng=random.Random(8))
    assert {d.genre for d in other} == {d.genre for d in first}


def test_cold_start_explores_and_never_locks_in():
    # One incumbent with a mediocre record vs one stone-cold genre.
    inputs = dict(
        genres=("food", "science"),
        engagement={
            "food": _evidenced(readers=20, opens=20, completions=8),
        },
        taps={},
        supply={"food": 1, "science": 1},
    )
    leaders = [
        rank_demand(**inputs, rng=random.Random(seed))[0].genre
        for seed in range(100)
    ]
    # The cold genre wins SOME mornings (cold start explores) and the
    # evidenced incumbent wins others (evidence still counts): the desk
    # neither starves the newcomer nor abandons the record. A
    # deterministic rule would pick one winner 100 times out of 100 —
    # the cumulative suboptimal lock-in this amendment removes.
    assert 0 < leaders.count("science") < 100


def test_evidence_narrows_the_posterior_and_earns_the_lead():
    # A strong, well-read record leads almost always — sampling is not
    # noise: the posterior tightens as the evidence grows.
    inputs = dict(
        genres=("food", "science"),
        engagement={
            "food": _evidenced(readers=60, opens=60, completions=55),
        },
        taps={},
        supply={"food": 1, "science": 1},
    )
    leaders = [
        rank_demand(**inputs, rng=random.Random(seed))[0].genre
        for seed in range(100)
    ]
    assert leaders.count("food") > 80


def test_the_reader_floor_flags_the_draw_and_rng_none_is_the_mean():
    # Below the floor: flagged explored — ranked on a draw, named.
    short = rank_demand(
        genres=("food",),
        engagement={
            "food": _evidenced(readers=DEMAND_READER_FLOOR - 1,
                               completions=3, likes=9)
        },
        taps={},
        supply={"food": 1},
        rng=random.Random(1),
    )
    assert short[0].explored is True
    at_floor = rank_demand(
        genres=("food",),
        engagement={"food": _evidenced(completions=3)},
        taps={},
        supply={"food": 1},
        rng=random.Random(1),
    )
    assert at_floor[0].explored is False
    # rng=None: the deterministic posterior-mean reading — same call,
    # twice, identical; the opt-out for certain-and-specific callers.
    mean_one = rank_demand(
        genres=GENRE_SET, engagement={}, taps={"local": 3}, supply={}
    )
    mean_two = rank_demand(
        genres=GENRE_SET, engagement={}, taps={"local": 3}, supply={}
    )
    assert mean_one == mean_two
    assert all(item.explored for item in mean_one)
    words = demand_line(mean_one, {mean_one[0].genre: "Around me"})
    assert "(trial)" in words


# --------------------------------------------------------------------------- #
# The stores: aggregates out, anonymity by schema.                             #
# --------------------------------------------------------------------------- #
def test_genre_evidence_rolls_up_distinct_readers_per_genre(tmp_path):
    conn = DurableConnection(tmp_path / "metrics.db")
    metrics = StoryMetricsStore(conn)
    genres_of = {"s1": ("food",), "s2": ("food", "local")}.get
    for principal, story, dwell, done in (
        ("alice", "s1", 10_000, True),
        ("alice", "s2", 5_000, False),  # same reader, second story
        ("bob", "s1", 8_000, True),
    ):
        metrics.read_receipt(
            tenant="t1", story_id=story, principal=principal,
            dwell_ms=dwell, completed=done,
        )
    metrics.like(tenant="t1", story_id="s2", principal="alice")
    book = metrics.genre_evidence(
        tenant="t1", genres_of=lambda sid: genres_of(sid) or ()
    )
    # Alice read two food stories but is ONE food reader.
    assert book["food"].readers == 2 and book["food"].opens == 3
    assert book["food"].completions == 2 and book["food"].likes == 1
    assert book["local"].readers == 1 and book["local"].opens == 1
    conn.close()


def test_the_interest_book_is_anonymous_by_schema(tmp_path):
    conn = DurableConnection(tmp_path / "demand.db")
    store = GenreDemandStore(conn)
    store.tap(tenant="t1", genre="food")
    store.tap(tenant="t1", genre="food")
    assert store.taps(tenant="t1") == {"food": 2}
    # No principal column EXISTS — there is nothing to consent to and
    # nothing to erase. Anonymity by construction, not by discipline.
    columns = {
        row["name"]
        for row in conn.db.execute(
            "PRAGMA table_info(press_genre_interest)"
        ).fetchall()
    }
    assert "principal" not in columns
    assert columns == {"tenant_id", "genre", "taps"}
    conn.close()


def test_the_standing_reading_replaces_whole(tmp_path):
    conn = DurableConnection(tmp_path / "demand.db")
    store = GenreDemandStore(conn)
    first = rank_demand(
        genres=GENRE_SET, engagement={}, taps={"food": 1}, supply={}
    )
    store.record(tenant="t1", items=first)
    second = rank_demand(
        genres=("food",), engagement={}, taps={"food": 1}, supply={}
    )
    store.record(tenant="t1", items=second)
    reading = store.reading(tenant="t1")
    # Never a mixed vintage: the second reading replaced the first.
    assert [r["genre"] for r in reading] == ["food"]
    assert reading[0]["evidence"]["taps"] == 1
    # No seed was passed: the reading honestly records none.
    assert reading[0]["draw_seed"] is None
    conn.close()


# --------------------------------------------------------------------------- #
# The gateway: the door and the News desk speak the reading.                   #
# --------------------------------------------------------------------------- #
def _host(tmp_path):
    app, conn, ident = _app(tmp_path)
    users = LocalUserStore(":memory:")
    accounts = LocalAccountService(users, ident.store, ident._signer)
    accounts.create_user("alice", "alice-password-1", tenant="t1")
    press = PressDesk(
        ContributionStore(conn),
        stories=StoryStore(conn),
        preferences=PreferenceStore(conn),
        intake=IntakeStore(conn),
        metrics=StoryMetricsStore(conn),
        demand=GenreDemandStore(conn),
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


def _say(gateway, token, message):
    response = gateway.handle(
        _req(
            "POST", "/v1/chat", token=token,
            body={"message": message, "agent": "news"},
        )
    )
    assert response.status == 200
    return response.body


def test_the_demand_door_and_the_news_desk_speak_the_reading(tmp_path):
    gateway, conn, ident, press = _host(tmp_path)
    alice = ident.token("alice")
    # Supply: one live piece in the food stream (through the desk's own
    # gate — the door's inputs are the platform's records).
    press.publish(
        tenant="t1",
        author="alice",
        title="The steel kettle, tested",
        body="Four minutes to a litre, day after day.",
        genres=("food",),
        license="oolu-members-1",
        consent=True,
    )

    # The door: a full reading, ranks 1..N, every row explained. With
    # no readers anywhere, every genre honestly explores — and the
    # reading records the seed its draws replay from.
    reading = gateway.handle(
        _req("GET", "/v1/press/genres/demand", token=alice)
    )
    assert reading.status == 200
    items = reading.body["items"]
    assert [r["rank"] for r in items] == list(range(1, len(items) + 1))
    assert all(r["explored"] for r in items)
    assert all(isinstance(r["factors"]["engagement"], float) for r in items)
    assert all(isinstance(r["draw_seed"], int) for r in items)
    food = next(r for r in items if r["genre"] == "food")
    assert food["evidence"]["pieces"] == 1

    # Naming a stream in the News thread is an ANONYMOUS tap — counted,
    # answered with the stream's standing, no principal anywhere.
    noted = _say(gateway, alice, "Food")
    assert noted["source"] == "desk"
    assert "Noted — Food" in noted["reply"]
    assert "trial candidate" in noted["reply"]
    assert press.demand.taps(tenant="t1") == {"food": 1}

    # "genres" answers with the chips AND the current demand reading.
    chips = _say(gateway, alice, "genres")
    assert chips["block"]["kind"] == "genres"
    assert "What readers lean toward now:" in chips["reply"]
    assert "(trial)" in chips["reply"]
    conn.close()
