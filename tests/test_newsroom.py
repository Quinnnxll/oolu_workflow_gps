"""The newsroom (A2): stories to magazine standard, pushed by preference.

Exit gate (agents-expansion plan, phase A2, amended): a story with no
resolvable contribution lineage cannot publish; every published story
records why it was selected (factor breakdown + rubric version) but the
scoring stays the house's own — it never rides the member-facing wire;
the ranking is bent instead by the member's consented SEMANTICS: their
taps (a 👍 stores the story's words and attracts likes-alike, a ⏭
repels) and their own words in the OoLu and News threads; two members
with different consented preferences get different edition orderings
while a learning-off member gets the neutral edition; a story carries
its lineage's attached media by reference; editions arrive on schedule
as News's own thread messages with a missed window caught up once and
the skipped count named; the serendipity slice is present under
personalization (property test); and the import scan still holds over
the grown press package.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from test_http_gateway import NOW as GATEWAY_NOW
from test_http_gateway import _app, _req

from oolu.durable.connection import DurableConnection
from oolu.durable.files import UserFileStore
from oolu.gateway import GatewayApp
from oolu.identity import LocalAccountService, LocalUserStore
from oolu.press import (
    EDITION_PULSE_GOAL,
    LICENSES,
    ContributionStore,
    GenreDemandStore,
    LineageShare,
    Newsroom,
    PairwiseStore,
    PreferenceStore,
    PressDesk,
    PressError,
    Story,
    StoryMetricsStore,
    StoryStore,
    Taste,
    edition_message,
    rank_edition,
    score,
    select,
    semantic_affinity,
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
# Agrees with VALLEY inside the corroboration band, below the
# near-duplicate flag — an independent voice, not a retelling.
VALLEY_AGREE = (
    "The storm reached the valley at last and the flooded terraces ran "
    "with water after the long dry months."
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
def _story(story_id, genres, selection, at, prose="…"):
    return Story(
        story_id=story_id,
        tenant_id="t1",
        headline=story_id,
        prose=prose,
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


def test_semantic_affinity_is_bounded_and_empty_taste_is_neutral():
    text = "The pier reopened with forty stalls at dawn."
    assert semantic_affinity(text, None) == 0.0
    assert semantic_affinity(text, Taste()) == 0.0
    # A verbatim like is full attraction; a verbatim skip full repulsion.
    assert semantic_affinity(text, Taste(liked=(text,))) == 1.0
    assert semantic_affinity(text, Taste(skipped=(text,))) == -1.0
    # Spoken words attract, never repel — talking about a subject is
    # interest.
    spoken = Taste(spoken=("any news from the stalls on the pier?",))
    assert 0.0 < semantic_affinity(text, spoken) <= 1.0


def test_taste_bends_by_meaning_and_a_skip_repels():
    kettle_prose = "A bench test of three kettles and the quiet one that won."
    pier_prose = "The pier reopened with forty stalls and a queue at dawn."
    stories = [
        _story("s-pier", ("local",), 0.55, NOW, prose=pier_prose),
        _story("s-kettle", ("local",), 0.5, NOW, prose=kettle_prose),
    ]
    # Same genre both — genre affinity has nothing to say. The liked
    # story's WORDS pull its like-alike over the pier's higher rubric
    # standing: meaning did the work.
    liked = Taste(liked=(f"s-kettle\n{kettle_prose}",))
    assert [
        s.story_id for s in rank_edition(stories, taste=liked)
    ][0] == "s-kettle"
    # And a skipped story falls behind what it outscored.
    skipped = Taste(skipped=(f"s-pier\n{pier_prose}",))
    assert [
        s.story_id for s in rank_edition(stories, taste=skipped)
    ][-1] == "s-pier"


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
            snippet="Kettles\nA bench test of three kettles.",
        )
    prefs.record(
        tenant="t1", principal="alice", signal="skip",
        subject="story:s2", genres=("sport",),
        snippet="Derby\nThe derby ended level.",
    )
    affinity = prefs.genre_affinity(tenant="t1", principal="alice")
    assert affinity["food"] == 1.0 and affinity["sport"] == -1.0
    # The taps' words read back as taste — and the caller's spoken words
    # ride along untouched.
    taste = prefs.taste(tenant="t1", principal="alice", spoken=("hi",))
    assert taste.liked == ("Kettles\nA bench test of three kettles.",) * 3
    assert taste.skipped == ("Derby\nThe derby ended level.",)
    assert taste.spoken == ("hi",)
    with pytest.raises(ValueError, match="unknown signal"):
        prefs.record(
            tenant="t1", principal="alice", signal="love",
            subject="story:s1",
        )
    assert prefs.erase(tenant="t1", principal="alice") == 4
    assert prefs.genre_affinity(tenant="t1", principal="alice") == {}
    # Erasure empties the taste too — nothing left to lean on.
    assert not prefs.taste(tenant="t1", principal="alice")
    conn.close()


def test_a_prefs_table_from_before_taste_gains_the_snippet_column(tmp_path):
    conn = DurableConnection(tmp_path / "old.db")
    with conn.transaction() as db:
        db.execute(
            """CREATE TABLE press_preferences (
                tenant_id TEXT NOT NULL,
                principal TEXT NOT NULL,
                signal TEXT NOT NULL,
                subject TEXT NOT NULL,
                genres TEXT NOT NULL,
                at TEXT NOT NULL
            )"""
        )
        db.execute(
            """INSERT INTO press_preferences VALUES
               ('t1', 'a', 'like', 'story:s1', '["food"]',
                '2026-01-01T00:00:00+00:00')"""
        )
    prefs = PreferenceStore(conn)
    # The old row stands as genre history — with no words to lean on.
    assert prefs.genre_affinity(tenant="t1", principal="a")["food"] == 1.0
    assert prefs.taste(tenant="t1", principal="a").liked == ()
    # New taps carry their words from here forward.
    prefs.record(
        tenant="t1", principal="a", signal="like",
        subject="story:s2", snippet="words now",
    )
    assert prefs.taste(tenant="t1", principal="a").liked == ("words now",)
    conn.close()


# --------------------------------------------------------------------------- #
# The gateway: consent at the doors, the edition on the pulse.                 #
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
        pairwise=PairwiseStore(conn),
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
        files=UserFileStore(conn),
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


def test_stories_consent_and_hidden_scoring_end_to_end(tmp_path):
    gateway, conn, ident = _host(tmp_path)
    alice, bob = _seed_and_compose(gateway, ident)

    # The neutral edition — and the scoring stays the house's own: the
    # breakdown is recorded durably but never rides the member's wire.
    edition = gateway.handle(_req("GET", "/v1/press/stories", token=bob))
    assert edition.status == 200 and edition.body["personalized"] is False
    items = edition.body["items"]
    assert len(items) >= 2
    assert all("breakdown" not in item for item in items)
    detail = gateway.handle(
        _req("GET", f"/v1/press/stories/{items[0]['story_id']}", token=bob)
    )
    assert "breakdown" not in detail.body
    assert detail.body["rubric_version"] == 1
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


def test_the_pairwise_book_outlives_the_poll_floor(tmp_path):
    """The DPO export door stands without any poll: the pairwise book is
    the member's own, written by whatever instrument records a choice
    (story feedback, future surveys), consent-gated at the door, and
    scrubbed once more on the way out."""
    gateway, conn, ident = _host(tmp_path)
    bob = ident.token("bob")

    # Consent off: nothing consented exists to export.
    refused = gateway.handle(
        _req("GET", "/v1/press/preferences/export", token=bob)
    )
    assert refused.status == 403

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
    # An instrument records a pairwise choice into the shared book —
    # same table, no poll anywhere.
    PairwiseStore(conn).record(
        tenant="t1",
        principal="bob",
        source="survey",
        prompt="[local] Which telling serves the reader better?",
        chosen="The fuller telling, with the queue described.",
        rejected="The brief one.",
        genres=("local",),
    )
    exported = gateway.handle(
        _req("GET", "/v1/press/preferences/export", token=bob)
    )
    assert exported.status == 200 and exported.body["count"] == 1
    pair = exported.body["items"][0]
    assert pair["chosen"].startswith("The fuller telling")
    # And the book rides account erasure like every consented signal.
    assert PairwiseStore(conn).erase(tenant="t1", principal="bob") == 1
    conn.close()


def test_the_members_own_chat_words_bend_their_edition(tmp_path):
    """No taps at all: what the member says to the News agent IS the
    preference — their own turns attract; the assistant's never count."""
    gateway, conn, ident = _host(tmp_path)
    alice, bob, carol = (ident.token(n) for n in ("alice", "bob", "carol"))
    for token, title, body, genres in (
        (alice, "Harbor market reopened", HARBOR, ["local"]),
        (bob, "Market back at the harbor", HARBOR_AGREE, ["local"]),
        (alice, "Rain reached the valley", VALLEY, ["science"]),
        (carol, "The valley storm arrived", VALLEY_AGREE, ["science"]),
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
    assert (
        gateway.handle(
            _req("POST", "/v1/press/newsroom/run", token=alice)
        ).status
        == 200
    )

    # Neutral first: both stories corroborated, the harbor leads.
    neutral = gateway.handle(_req("GET", "/v1/press/stories", token=bob))
    assert neutral.body["personalized"] is False
    assert "Harbor" in neutral.body["items"][0]["headline"]

    # Bob consents and keeps telling the News agent about the valley.
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
    history = AssistantHistoryStore(conn)
    history.append(
        tenant="t1",
        principal="bob",
        kind="user",
        body=(
            "Any more on the storm? I keep thinking about the flooded "
            "terraces and the runoff reaching the valley after three "
            "months dry."
        ),
        agent="news",
    )
    # The assistant's own words never count as the member's preference —
    # were they gathered, this harbor retelling would out-pull the chat.
    history.append(
        tenant="t1", principal="bob", kind="assistant", body=HARBOR,
        agent="news",
    )

    bent = gateway.handle(_req("GET", "/v1/press/stories", token=bob))
    assert bent.body["personalized"] is True
    assert "valley" in bent.body["items"][0]["headline"].lower()
    # Alice never consented: her edition stands neutral.
    still = gateway.handle(_req("GET", "/v1/press/stories", token=alice))
    assert still.body["personalized"] is False
    assert "Harbor" in still.body["items"][0]["headline"]
    conn.close()


def test_a_story_carries_its_lineage_media_by_reference(tmp_path):
    """Multimedia rides the contribution into the story: the payload
    names each attachment; the press media door serves its true bytes —
    an inline data-URL row decoded, never base64-as-image."""
    from base64 import b64encode

    gateway, conn, ident = _host(tmp_path)
    alice = ident.token("alice")
    photo = b64encode(b"jpeg-bytes-here").decode()
    created = gateway.handle(
        _req(
            "POST",
            "/v1/files",
            token=alice,
            body={
                "name": "pier.jpg",
                "content": f"data:image/jpeg;base64,{photo}",
                "media_type": "image/jpeg",
            },
        )
    )
    assert created.status == 201
    file_id = created.body["file_id"]
    published = gateway.handle(
        _req(
            "POST",
            "/v1/press/contributions",
            token=alice,
            body={
                "title": "Harbor market reopened",
                "body": HARBOR,
                "genres": ["local"],
                "license": LICENSE,
                "consent": True,
                "file_ids": [file_id],
            },
        )
    )
    assert published.status == 201
    assert (
        gateway.handle(
            _req("POST", "/v1/press/newsroom/run", token=alice)
        ).status
        == 200
    )
    edition = gateway.handle(_req("GET", "/v1/press/stories", token=alice))
    [story] = edition.body["items"]
    assert story["media"] == [
        {
            "contribution_id": published.body["contribution_id"],
            "index": 0,
            "media_type": "image/jpeg",
            "name": "pier.jpg",
        }
    ]
    served = gateway.handle(
        _req(
            "GET",
            f"/v1/press/contributions/{published.body['contribution_id']}"
            "/media/0",
            token=alice,
        )
    )
    assert served.status == 200
    assert served.body == b"jpeg-bytes-here"
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
    # N0: the STORY BLOCK rides beside the words and survives reload —
    # previews with a pointer, never the whole prose twice.
    story_block = history[-1]["block"]
    assert story_block["kind"] == "story"
    first = story_block["items"][0]
    assert first["story_id"] and first["headline"] and first["bylines"]
    assert len(first["preview"]) <= 281
    # N1: the fire also refreshed the genre desk's standing reading —
    # the benchmark loop closing on the pulse.
    demand = gateway._press.demand.reading(tenant="t1")
    assert demand and [r["rank"] for r in demand] == list(
        range(1, len(demand) + 1)
    )
    conn.close()


# --------------------------------------------------------------------------- #
# N0 — the benchmark's measuring stick.                                        #
# --------------------------------------------------------------------------- #
def _read(gateway, token, story_id, *, dwell_ms, completed):
    return gateway.handle(
        _req(
            "POST",
            f"/v1/press/stories/{story_id}/feedback",
            token=token,
            body={"signal": "read", "dwell_ms": dwell_ms, "completed": completed},
        )
    )


def test_reading_is_measured_only_under_consent_and_reveals_over_the_floor(
    tmp_path,
):
    from oolu.press import METRICS_K_FLOOR

    gateway, conn, ident = _host(tmp_path)
    users = gateway._accounts
    for i in range(METRICS_K_FLOOR + 1):
        users.create_user(f"reader{i}", f"reader{i}-password-1", tenant="t1")
    alice, _ = _seed_and_compose(gateway, ident)
    story = gateway.handle(
        _req("GET", "/v1/press/stories", token=alice)
    ).body["items"][0]
    sid = story["story_id"]

    # Consent off: the read is honestly NOT recorded — no receipt, no
    # count, and the answer says so.
    cold = _read(gateway, ident.token("reader0"), sid, dwell_ms=9_000, completed=True)
    assert cold.body == {"recorded": False, "reason": "personalization is off"}

    # K_FLOOR consented readers open the story; some finish, some not.
    for i in range(METRICS_K_FLOOR):
        token = ident.token(f"reader{i}")
        assert (
            gateway.handle(
                _req(
                    "PUT",
                    "/v1/settings",
                    token=token,
                    body={"changes": {"press.personalize": True}},
                )
            ).status
            == 200
        )
        assert _read(
            gateway,
            token,
            sid,
            dwell_ms=10_000 + i * 1_000,
            completed=i % 2 == 0,
        ).body == {"recorded": True}
    # Below the floor first: one receipt short refuses to reveal.
    # (Simulate by asking for a story nobody read.)
    other = gateway.handle(
        _req("GET", "/v1/press/stories", token=alice)
    ).body["items"][1]
    veiled = gateway.handle(
        _req(
            "GET", f"/v1/press/stories/{other['story_id']}/metrics", token=alice
        )
    )
    assert veiled.body == {
        "story_id": other["story_id"],
        "revealed": False,
        "reason": "not enough readers yet",
    }

    # At the floor: the three benchmark numbers, exactly.
    likes = 2
    for i in range(likes):
        assert (
            gateway.handle(
                _req(
                    "POST",
                    f"/v1/press/stories/{sid}/feedback",
                    token=ident.token(f"reader{i}"),
                    body={"signal": "like"},
                )
            ).body["recorded"]
            is True
        )
    # A double like counts once — the first tap is the tap.
    gateway.handle(
        _req(
            "POST",
            f"/v1/press/stories/{sid}/feedback",
            token=ident.token("reader0"),
            body={"signal": "like"},
        )
    )
    revealed = gateway.handle(
        _req("GET", f"/v1/press/stories/{sid}/metrics", token=alice)
    ).body
    assert revealed["revealed"] is True
    assert revealed["opens"] == METRICS_K_FLOOR
    assert revealed["likes"] == likes
    completions = sum(1 for i in range(METRICS_K_FLOOR) if i % 2 == 0)
    assert revealed["completion_rate"] == round(
        completions / METRICS_K_FLOOR, 4
    )
    dwells = [10_000 + i * 1_000 for i in range(METRICS_K_FLOOR)]
    assert revealed["mean_attention_ms"] == int(
        round(sum(dwells) / len(dwells))
    )

    # A re-read never inflates: the longest dwell stands, completion
    # sticks, and opens stay one per reader.
    assert _read(
        gateway, ident.token("reader1"), sid, dwell_ms=500, completed=False
    ).body == {"recorded": True}
    again = gateway.handle(
        _req("GET", f"/v1/press/stories/{sid}/metrics", token=alice)
    ).body
    assert again["opens"] == METRICS_K_FLOOR
    assert again["mean_attention_ms"] == revealed["mean_attention_ms"]
    assert again["completion_rate"] == revealed["completion_rate"]

    # Erasure outranks the aggregate: a reader's rows go, the numbers
    # honestly shrink below the floor and the reveal closes.
    erased = StoryMetricsStore(conn).erase(tenant="t1", principal="reader0")
    assert erased == 2  # the receipt and the like
    closed = gateway.handle(
        _req("GET", f"/v1/press/stories/{sid}/metrics", token=alice)
    ).body
    assert closed["revealed"] is False
    conn.close()


def test_the_import_scan_holds_over_the_grown_press_package():
    from pathlib import Path

    package = Path(__file__).resolve().parents[1] / "src" / "oolu" / "press"
    names = {p.name for p in package.glob("*.py")}
    # The A2 modules are in the scanned set with the rest.
    assert {"standards.py", "newsroom.py", "editions.py"} <= names
