"""The genre desk (N1): which genre are readers interested in, on evidence.

Exit gate (news-agent-benchmark-roadmap, phase N1): the ranking is
reproducible from its recorded inputs; every rank carries its factor
breakdown and raw evidence; a genre with zero evidence ranks by the
exploration rule and SAYS so (the one bounded trial slot promotes the
best explorer to second place, deterministically — never a random
draw); the reader floor gates the engagement factor — a number is
never faked from two receipts; the interest book is anonymous by
schema (no principal column exists to leak); and no model call happens
anywhere in the decision — the desk answers on a host with no brain at
all.
"""

from __future__ import annotations

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


def test_the_ranking_is_reproducible_and_explains_itself():
    inputs = dict(
        genres=GENRE_SET,
        engagement={
            "food": _evidenced(completions=4, dwell_ms=50_000, likes=3),
            "local": _evidenced(completions=1, dwell_ms=20_000),
        },
        taps={"food": 2, "science": 6},
        supply={"food": 4, "local": 2, "science": 1},
    )
    first = rank_demand(**inputs)
    second = rank_demand(**inputs)
    # Same inputs, same order, same breakdowns — reproducibility IS the
    # audit.
    assert first == second
    assert [d.rank for d in first] == [1, 2, 3]
    for item in first:
        assert set(item.factors) == {"engagement", "interest", "supply"}
        assert set(item.evidence) == {
            "readers", "opens", "completions", "likes", "taps", "pieces",
        }
    # Food's finished, lingered-over, liked stories lead; the unmeasured
    # science stream takes the trial slot at SECOND place and says so.
    assert first[0].genre == "food" and first[0].explored is False
    assert first[1].genre == "science" and first[1].explored is True
    assert first[1].factors["engagement"] is None
    assert first[2].genre == "local" and first[2].explored is False


def test_the_reader_floor_gates_the_engagement_factor():
    # One reader short of the floor: the engagement factor is honestly
    # ABSENT — never a number faked from a handful of receipts.
    short = rank_demand(
        genres=("food",),
        engagement={
            "food": _evidenced(readers=DEMAND_READER_FLOOR - 1,
                               completions=3, likes=9)
        },
        taps={},
        supply={"food": 1},
    )
    assert short[0].explored is True
    assert short[0].factors["engagement"] is None
    at_floor = rank_demand(
        genres=("food",),
        engagement={"food": _evidenced(completions=3)},
        taps={},
        supply={"food": 1},
    )
    assert at_floor[0].explored is False
    assert at_floor[0].factors["engagement"] is not None


def test_a_floor_with_no_evidence_is_exploration_end_to_end():
    items = rank_demand(
        genres=GENRE_SET,
        engagement={},
        taps={"local": 3},
        supply={"food": 1},
    )
    assert all(item.explored for item in items)
    # Partial scores order the explorers; ties break by name — still
    # deterministic, still explained.
    assert items[0].genre == "local"  # taps lead the partial blend
    words = demand_line(items, {"local": "Around me"})
    assert "Around me (trial)" in words


def test_the_trial_slot_is_bounded_to_one():
    items = rank_demand(
        genres=("food", "local", "science", "results"),
        engagement={"food": _evidenced(completions=5)},
        taps={"science": 2, "results": 1},
        supply={"local": 3},
    )
    # One evidenced leader, ONE promoted explorer, the rest behind.
    assert [d.genre for d in items[:2]] == ["food", "science"]
    assert [d.explored for d in items] == [False, True, True, True]


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
    assert reading[0]["factors"]["engagement"] is None
    assert reading[0]["evidence"]["taps"] == 1
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
    # no readers anywhere, every genre honestly explores.
    reading = gateway.handle(
        _req("GET", "/v1/press/genres/demand", token=alice)
    )
    assert reading.status == 200
    items = reading.body["items"]
    assert [r["rank"] for r in items] == list(range(1, len(items) + 1))
    assert all(r["explored"] for r in items)
    assert items[0]["genre"] == "food"  # supply leads the partial blend
    assert items[0]["factors"]["engagement"] is None
    assert items[0]["evidence"]["pieces"] == 1

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
