"""The newsroom (A2): stories to magazine standard, pushed by preference.

Exit gate (agents-expansion plan, phase A2): a story with no resolvable
contribution lineage cannot publish; every published story records why
it was selected (factor breakdown + rubric version) and the reasons
render on demand; two members with different consented preferences get
different edition orderings while a learning-off member gets the neutral
edition; editions arrive on schedule as News's own thread messages with
a missed window caught up once and the skipped count named; the
serendipity slice is present under personalization (property test); and
the import scan still holds over the grown press package.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from test_http_gateway import NOW as GATEWAY_NOW
from test_http_gateway import _app, _req

from oolu.durable.connection import DurableConnection
from oolu.gateway import GatewayApp
from oolu.identity import LocalAccountService, LocalUserStore
from oolu.press import (
    EDITION_PULSE_GOAL,
    LICENSES,
    ContributionStore,
    LineageShare,
    Newsroom,
    PreferenceStore,
    PressDesk,
    PressError,
    Story,
    StoryStore,
    edition_message,
    rank_edition,
    score,
    select,
)
from oolu.settings_node import SettingsNode, SettingsStore
from oolu.social import AssistantHistoryStore

NOW = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)
LICENSE = next(iter(LICENSES))


def _stores(tmp_path):
    conn = DurableConnection(tmp_path / "newsroom.db")
    tick = {"now": NOW}

    def clock():
        tick["now"] += timedelta(seconds=1)
        return tick["now"]

    contributions = ContributionStore(conn, clock=clock)
    stories = StoryStore(conn, clock=clock)
    return conn, PressDesk(contributions, stories=stories), stories


def _publish(desk, author, title, body, genres=("local",)):
    return desk.publish(
        tenant="t1",
        author=author,
        title=title,
        body=body,
        genres=genres,
        license=LICENSE,
        consent=True,
    )


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


# --------------------------------------------------------------------------- #
# The rubric.                                                                  #
# --------------------------------------------------------------------------- #
def test_corroboration_counts_independent_authors_once(tmp_path):
    conn, desk, _ = _stores(tmp_path)
    anchor = _publish(desk, "alice", "Harbor market reopened", HARBOR)
    _publish(desk, "bob", "Market back at the harbor", HARBOR_AGREE)
    # Bob agreeing twice is still one voice; alice's own retelling never
    # corroborates herself.
    _publish(desk, "bob", "Harbor market, again", HARBOR_AGREE + " Again.")
    corpus = desk.store.list(tenant="t1")
    breakdown = score(
        next(c for c in corpus if c.contribution_id == anchor.contribution_id),
        corpus,
        now=NOW + timedelta(hours=1),
    )
    assert breakdown.corroborators == 1
    assert 0 < breakdown.critical <= 1.0
    # The whole factor set is present — the reasons a reader can demand.
    for factor in ("inspiring", "critical", "knowledgeable", "selection"):
        assert factor in breakdown.as_dict()
    conn.close()


def test_select_never_anchors_a_flagged_retelling_or_a_told_piece(tmp_path):
    conn, desk, _ = _stores(tmp_path)
    anchor = _publish(desk, "alice", "Harbor market reopened", HARBOR)
    retold = _publish(
        desk,
        "bob",
        "Harbor market reopened this morning",
        HARBOR.replace("queue", "line"),
    )
    assert retold.similar_to == anchor.contribution_id  # flagged (A1)
    corpus = desk.store.list(tenant="t1")
    chosen = select(corpus, now=NOW + timedelta(hours=1), limit=10)
    assert [c.contribution_id for c, _ in chosen] == [anchor.contribution_id]
    # And a piece a standing story already cites is excluded by id.
    assert (
        select(
            corpus,
            now=NOW + timedelta(hours=1),
            limit=10,
            exclude={anchor.contribution_id},
        )
        == []
    )
    conn.close()


# --------------------------------------------------------------------------- #
# Composition: lineage recorded, provenance mandatory.                         #
# --------------------------------------------------------------------------- #
def test_the_desk_composes_with_lineage_weights_that_sum_to_one(tmp_path):
    conn, desk, stories = _stores(tmp_path)
    anchor = _publish(desk, "alice", "Harbor market reopened", HARBOR)
    _publish(desk, "bob", "Market back at the harbor", HARBOR_AGREE)
    retold = _publish(
        desk, "carol", "Harbor market reopened!", HARBOR.replace("forty", "40")
    )
    assert retold.similar_to == anchor.contribution_id
    [story] = Newsroom(desk.store, stories).run(tenant="t1")
    assert story.source == "desk"  # model-less: the anchor's own words
    assert story.headline == "Harbor market reopened"
    # The attribution set A5 pays: anchor weighted highest, every cited
    # contributor present, the whole thing summing to exactly 1.0.
    weights = {s.author: s.weight for s in story.lineage}
    assert weights["alice"] == 0.6
    assert set(weights) == {"alice", "bob", "carol"}
    assert round(sum(s.weight for s in story.lineage), 6) == 1.0
    # Idempotent: the told stay told.
    assert Newsroom(desk.store, stories).run(tenant="t1") == []
    conn.close()


def test_a_story_without_lineage_cannot_publish(tmp_path):
    conn, _, stories = _stores(tmp_path)
    with pytest.raises(PressError, match="provenance"):
        stories.insert(
            Story(
                story_id="s1",
                tenant_id="t1",
                headline="Unsourced",
                prose="Words from nowhere.",
                genres=("local",),
                lineage=(),
                breakdown={},
                rubric_version=1,
                source="desk",
                created_at=NOW,
            )
        )
    conn.close()


def test_the_seat_composes_when_it_keeps_the_contract(tmp_path):
    conn, desk, stories = _stores(tmp_path)
    _publish(desk, "alice", "Harbor market reopened", HARBOR)

    class Keeps:
        def reply(self, messages):
            # The frame binds composition to the cited material only.
            assert "Never invent" in messages[0]["content"]
            assert "harbor market" in messages[1]["content"]
            return "HEADLINE: The pier queues again\n\nForty stalls returned."

    [story] = Newsroom(desk.store, stories).run(tenant="t1", model=Keeps())
    assert story.source == "model"
    assert story.headline == "The pier queues again"
    conn.close()


def test_a_contract_breaking_model_falls_back_to_the_desk(tmp_path):
    conn, desk, stories = _stores(tmp_path)
    _publish(desk, "alice", "Harbor market reopened", HARBOR)

    class Rambles:
        def reply(self, messages):
            return "Sure! Here's a story I made up about dragons."

    [story] = Newsroom(desk.store, stories).run(tenant="t1", model=Rambles())
    assert story.source == "desk"
    assert story.prose.startswith("After two years of repairs")
    conn.close()


# --------------------------------------------------------------------------- #
# Editions: consent bends, serendipity holds.                                  #
# --------------------------------------------------------------------------- #
def _story(story_id, genres, selection, at):
    return Story(
        story_id=story_id,
        tenant_id="t1",
        headline=story_id,
        prose="…",
        genres=tuple(genres),
        lineage=(
            LineageShare(contribution_id=f"c-{story_id}", author="a", weight=1.0),
        ),
        breakdown={"selection": selection},
        rubric_version=1,
        source="desk",
        created_at=at,
    )


def test_affinity_bends_the_neutral_order_and_absence_does_not():
    stories = [
        _story("s-food", ("food",), 0.5, NOW),
        _story("s-sport", ("sport",), 0.55, NOW),
    ]
    neutral = rank_edition(stories, affinity=None)
    assert [s.story_id for s in neutral] == ["s-sport", "s-food"]
    bent = rank_edition(stories, affinity={"food": 1.0, "sport": -1.0})
    assert [s.story_id for s in bent][0] == "s-food"


def test_the_serendipity_slice_survives_a_strong_leaning():
    # Five loved-genre stories outrank the lone outsider — the slice
    # still seats the outsider in the edition's last slot.
    stories = [
        _story(f"s{i}", ("food",), 0.9 - i * 0.01, NOW) for i in range(5)
    ] + [_story("s-out", ("science",), 0.2, NOW)]
    edition = rank_edition(stories, affinity={"food": 1.0}, size=5)
    assert len(edition) == 5
    assert any(s.story_id == "s-out" for s in edition)


def test_the_edition_message_names_the_skipped_count():
    story = _story("s1", ("local",), 0.5, NOW)
    message = edition_message([story], skipped=2)
    assert "2 earlier editions were missed" in message
    assert "s1" in message  # the headline (test story uses id as headline)
    assert "by a" in message  # and the byline


def test_preferences_are_recorded_bounded_and_erasable(tmp_path):
    conn = DurableConnection(tmp_path / "prefs.db")
    prefs = PreferenceStore(conn)
    for _ in range(3):
        prefs.record(
            tenant="t1", principal="alice", signal="like",
            subject="story:s1", genres=("food",),
        )
    prefs.record(
        tenant="t1", principal="alice", signal="skip",
        subject="story:s2", genres=("sport",),
    )
    affinity = prefs.genre_affinity(tenant="t1", principal="alice")
    assert affinity["food"] == 1.0 and affinity["sport"] == -1.0
    with pytest.raises(ValueError, match="unknown signal"):
        prefs.record(
            tenant="t1", principal="alice", signal="love",
            subject="story:s1",
        )
    assert prefs.erase(tenant="t1", principal="alice") == 4
    assert prefs.genre_affinity(tenant="t1", principal="alice") == {}
    conn.close()


# --------------------------------------------------------------------------- #
# The gateway: consent at the doors, the edition on the pulse.                 #
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
    return gateway, conn, ident


def _seed_and_compose(gateway, ident):
    alice, bob = ident.token("alice"), ident.token("bob")
    for token, title, body, genres in (
        (alice, "Harbor market reopened", HARBOR, ["local"]),
        (bob, "Market back at the harbor", HARBOR_AGREE, ["local"]),
        (bob, "Rain reached the valley", VALLEY, ["science"]),
    ):
        assert (
            gateway.handle(
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
            ).status
            == 201
        )
    composed = gateway.handle(
        _req("POST", "/v1/press/newsroom/run", token=alice)
    )
    assert composed.status == 200 and composed.body["composed"] >= 2
    return alice, bob


def test_stories_consent_and_reasons_end_to_end(tmp_path):
    gateway, conn, ident = _host(tmp_path)
    alice, bob = _seed_and_compose(gateway, ident)

    # The neutral edition, and the reasons on demand.
    edition = gateway.handle(_req("GET", "/v1/press/stories", token=bob))
    assert edition.status == 200 and edition.body["personalized"] is False
    items = edition.body["items"]
    assert len(items) >= 2
    detail = gateway.handle(
        _req("GET", f"/v1/press/stories/{items[0]['story_id']}", token=bob)
    )
    assert detail.body["breakdown"]["rubric_version"] == 1
    assert detail.body["lineage"] and detail.body["lineage"][0]["weight"] > 0

    # Learning off: the tap is honestly NOT recorded.
    dropped = gateway.handle(
        _req(
            "POST",
            f"/v1/press/stories/{items[0]['story_id']}/feedback",
            token=bob,
            body={"signal": "like"},
        )
    )
    assert dropped.body == {
        "recorded": False,
        "reason": "personalization is off",
    }

    # Bob consents; his science likes and local skips bend HIS edition —
    # alice keeps the neutral order. Two members, two orderings.
    assert (
        gateway.handle(
            _req(
                "PUT",
                "/v1/settings",
                token=bob,
                body={"changes": {"press.personalize": True}},
            )
        ).status
        == 200
    )
    science = next(i for i in items if "science" in i["genres"])
    local = next(i for i in items if "local" in i["genres"])
    for story, signal in ((science, "like"), (science, "like"), (local, "skip")):
        recorded = gateway.handle(
            _req(
                "POST",
                f"/v1/press/stories/{story['story_id']}/feedback",
                token=bob,
                body={"signal": signal},
            )
        )
        assert recorded.body == {"recorded": True}
    bent = gateway.handle(_req("GET", "/v1/press/stories", token=bob))
    assert bent.body["personalized"] is True
    assert bent.body["items"][0]["story_id"] == science["story_id"]
    neutral = gateway.handle(_req("GET", "/v1/press/stories", token=alice))
    assert neutral.body["personalized"] is False
    assert [i["story_id"] for i in neutral.body["items"]] != [
        i["story_id"] for i in bent.body["items"]
    ]
    conn.close()


def test_the_edition_arrives_on_the_pulse_as_news_own_message(tmp_path):
    gateway, conn, ident = _host(tmp_path)
    alice, _ = _seed_and_compose(gateway, ident)

    # Alice sets her morning-edition rhythm through the one door.
    scheduled = gateway.handle(
        _req(
            "POST",
            "/v1/press/edition/schedule",
            token=alice,
            body={"at_minute": 8 * 60},
            now=GATEWAY_NOW,
        )
    )
    assert scheduled.status == 200
    assert scheduled.body["edition_schedule"]["goal"] == EDITION_PULSE_GOAL

    # The next 08:00 arrives; any request's lazy tick fires the claim.
    # (The tick's once-a-minute monotonic gate is reopened by hand — the
    # seeding requests above already spent this minute's tick.)
    morning = (GATEWAY_NOW + timedelta(days=1)).replace(
        hour=8, minute=0, second=0, microsecond=0
    )
    gateway._pulse_gate = 0.0
    gateway.handle(_req("GET", "/v1/press/stories", token=alice, now=morning))
    history = gateway._assistant_history.history(
        tenant="t1", principal="alice", agent="news"
    )
    assert history, "the edition should land in the News thread"
    assert "your edition" in history[-1]["body"].lower()
    assert "Harbor" in history[-1]["body"]
    conn.close()


def test_the_import_scan_holds_over_the_grown_press_package():
    from pathlib import Path

    package = Path(__file__).resolve().parents[1] / "src" / "oolu" / "press"
    names = {p.name for p in package.glob("*.py")}
    # The A2 modules are in the scanned set with the rest.
    assert {"standards.py", "newsroom.py", "editions.py"} <= names
