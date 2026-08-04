"""The composition desk (N4): the final post — every claim sourced.

Exit gate (news-agent-benchmark-roadmap, phase N4): a post with an
unresolvable claim cannot be stored (no lineage AND no sources refuses;
a malformed source row refuses); the rendered post's source table
matches the stored rows exactly (they are one table); the model-down
path still produces a publishable desk post; and the disclosure flag
survives from topic brief to rendered post VERBATIM — appended by the
desk, never entrusted to the model. Contribution sources still produce
lineage shares summing exactly to 1.0, so the dividend keeps paying;
a pure-market post stores with sources and no lineage. The pipeline
closes: a completed read of a topic post writes the kind book's
engaged side — the exploration draws learn from real reading.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from test_http_gateway import _app, _req

from oolu.durable.connection import DurableConnection
from oolu.gateway import GatewayApp
from oolu.identity import LocalAccountService, LocalUserStore
from oolu.press import (
    ContributionStore,
    GenreDemandStore,
    IntakeStore,
    PairwiseStore,
    PreferenceStore,
    PressDesk,
    PressError,
    Story,
    StoryMetricsStore,
    StoryStore,
    SurveyDesk,
    SurveyStore,
    TopicBriefStore,
    compose_post,
    compose_story_parts,
    desk_post,
    source_rows,
)
from oolu.settings_node import SettingsNode, SettingsStore
from oolu.social import AssistantHistoryStore

NOW = datetime(2026, 8, 4, 8, 0, tzinfo=UTC)

MARKET_BRIEF = {
    "topic_key": "trust:l1",
    "genre": "products",
    "subject": "The steel kettle: the order book raises a trust concern",
    "disclosure": "Disclosure: seller kettleworks runs active advertising "
    "on this platform.",
    "factors": {"demand": 0.5, "evidence": 0.5, "freshness": 1.0},
    "facts": [
        {"kind": "listing", "ref": "l1",
         "summary": "“The steel kettle” by kettleworks, 27.00"},
        {"kind": "trust", "ref": "l1",
         "summary": "order-book trust 0.3 for kettleworks "
         "(1 finished, 1 refunded, 1 disputed)"},
    ],
}

SURVEY_RESULT = {
    "survey_id": "s1",
    "topic_key": "trust:l1",
    "kind": "editorial",
    "question": "Is this worth a full story?",
    "status": "closed",
    "sample_size": 3,
    "answers": 6,
    "counts": {"worth": 5, "not_worth": 1},
}


# --------------------------------------------------------------------------- #
# Composition: the desk voice, the contract, the disclosure.                   #
# --------------------------------------------------------------------------- #
def test_the_desk_post_renders_the_typed_facts_and_the_survey_line():
    headline, prose = desk_post(MARKET_BRIEF, SURVEY_RESULT)
    assert headline.startswith("The steel kettle")
    # The unfavorable number rides unedited — honesty cuts both ways.
    assert "trust 0.3" in prose and "1 disputed" in prose
    assert "Reader survey (6 answers)" in prose and "worth: 5" in prose
    # Below the floor, the survey line is the honest reason instead.
    _, veiled = desk_post(
        MARKET_BRIEF,
        {**SURVEY_RESULT, "answers": 2, "counts": None,
         "reason": "not enough answers yet"},
    )
    assert "not enough answers yet" in veiled


def test_the_contract_holds_and_a_broken_model_falls_back():
    class Keeps:
        def reply(self, messages):
            assert "S1. [listing]" in messages[0]["content"]
            return "HEADLINE: Kettle trust concern\nPROSE: The book says 0.3."

    headline, prose, voice = compose_post(
        MARKET_BRIEF, SURVEY_RESULT, model=Keeps()
    )
    assert (headline, voice) == ("Kettle trust concern", "model")

    class Breaks:
        def reply(self, messages):
            return "A story about kettles, freely invented."

    headline, prose, voice = compose_post(
        MARKET_BRIEF, SURVEY_RESULT, model=Breaks()
    )
    assert voice == "desk" and "trust 0.3" in prose
    # No model at all: still publishable — degraded is honest.
    assert compose_post(MARKET_BRIEF, SURVEY_RESULT)[2] == "desk"


def test_the_disclosure_survives_both_voices_verbatim():
    class Keeps:
        def reply(self, messages):
            return "HEADLINE: Kettle\nPROSE: The book says 0.3."

    for model in (None, Keeps()):
        parts = compose_story_parts(
            MARKET_BRIEF, SURVEY_RESULT,
            contribution_of=lambda cid: None, model=model,
        )
        assert parts["disclosure"] == MARKET_BRIEF["disclosure"]
        assert parts["prose"].endswith(MARKET_BRIEF["disclosure"])


def test_source_rows_carry_every_fact_plus_the_survey():
    rows = source_rows(MARKET_BRIEF, SURVEY_RESULT)
    assert [r["kind"] for r in rows] == ["listing", "trust", "survey"]
    assert rows[2]["ref"] == "s1"
    assert "Is this worth a full story?" in rows[2]["summary"]
    assert "worth: 5" in rows[2]["summary"]


def test_contribution_sources_become_lineage_summing_to_one(tmp_path):
    conn = DurableConnection(tmp_path / "press.db")
    desk = PressDesk(ContributionStore(conn))
    pieces = [
        desk.publish(
            tenant="t1", author=author, title=f"Piece {author}",
            body="After two years the harbor market reopened this morning "
            + extra,
            genres=("local",), license="oolu-members-1", consent=True,
        )
        for author, extra in (
            ("alice", "with forty stalls."),
            ("bob", "and the queue said everything."),
            ("carol", "and the old bell rang at eight."),
        )
    ]
    brief = {
        "topic_key": "cluster:c1",
        "subject": "3 members are telling the same story",
        "facts": [
            {"kind": "contribution", "ref": p.contribution_id,
             "summary": f"“{p.title}” by {p.author}"}
            for p in pieces
        ] + [{"kind": "contribution", "ref": "gone",
              "summary": "an unpublished piece"}],
    }
    parts = compose_story_parts(
        brief, None,
        contribution_of=lambda cid: desk.store.get(cid, tenant="t1"),
    )
    # The gone ref honestly dropped; the live three split exactly 1.0.
    assert len(parts["lineage"]) == 3
    assert round(sum(s.weight for s in parts["lineage"]), 6) == 1.0
    assert {s.author for s in parts["lineage"]} == {"alice", "bob", "carol"}
    conn.close()


# --------------------------------------------------------------------------- #
# The store: the extended provenance law, one table for the sources.           #
# --------------------------------------------------------------------------- #
def test_the_extended_provenance_law_refuses_the_unsourced(tmp_path):
    conn = DurableConnection(tmp_path / "stories.db")
    stories = StoryStore(conn)
    bare = Story(
        story_id="s1", tenant_id="t1", headline="Unsourced",
        prose="Words from nowhere.", genres=("products",), lineage=(),
        breakdown={}, rubric_version=0, source="desk", created_at=NOW,
        topic_key="trust:l1",
    )
    with pytest.raises(PressError, match="provenance"):
        stories.insert(bare)
    with pytest.raises(PressError, match="source row"):
        stories.insert(bare, sources=[{"kind": "listing", "ref": ""}])
    # A pure-market post stores with sources and NO lineage — and the
    # stored table reads back exactly as written.
    rows = source_rows(MARKET_BRIEF, SURVEY_RESULT)
    stories.insert(bare, sources=rows)
    assert stories.sources_of("s1") == sorted(
        rows, key=lambda r: (r["kind"], r["ref"])
    )
    assert stories.told_topic_keys(tenant="t1") == {"trust:l1"}
    conn.close()


# --------------------------------------------------------------------------- #
# The gateway: closed survey → post → the loop's engaged side.                 #
# --------------------------------------------------------------------------- #
HARBOR = (
    "After two years of repairs the harbor market reopened this morning "
    "with forty stalls and a queue down the pier."
)
HARBOR_AGREE = (
    "The harbor market reopened today — forty stalls, and the queue down "
    "the pier said everything about the wait."
)


def _host(tmp_path):
    app, conn, ident = _app(tmp_path)
    users = LocalUserStore(":memory:")
    accounts = LocalAccountService(users, ident.store, ident._signer)
    for name in ("alice", "bob"):
        accounts.create_user(name, f"{name}-password-1", tenant="t1")
    pairwise = PairwiseStore(conn)
    press = PressDesk(
        ContributionStore(conn),
        stories=StoryStore(conn),
        preferences=PreferenceStore(conn),
        pairwise=pairwise,
        intake=IntakeStore(conn),
        metrics=StoryMetricsStore(conn),
        demand=GenreDemandStore(conn),
        topics=TopicBriefStore(conn),
        surveys=SurveyDesk(SurveyStore(conn), pairwise),
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


def test_a_closed_survey_becomes_a_sourced_post_and_the_loop_learns(
    tmp_path,
):
    gateway, conn, ident, press = _host(tmp_path)
    for author, title, body in (
        ("alice", "Harbor market reopened", HARBOR),
        ("bob", "Market back at the harbor", HARBOR_AGREE),
    ):
        press.publish(
            tenant="t1", author=author, title=title, body=body,
            genres=("local",), license="oolu-members-1", consent=True,
        )
    gateway._topic_reading("t1")
    gateway._survey_tick("t1")
    [survey] = press.surveys.store.list(tenant="t1", status="open")
    # An open survey composes nothing; closing it is the desk's cue.
    assert gateway._compose_topic_posts("t1") == 0
    press.surveys.store.close(survey.survey_id, tenant="t1")
    assert gateway._compose_topic_posts("t1") == 1
    # Never twice — the told stay told.
    assert gateway._compose_topic_posts("t1") == 0

    story = next(
        s for s in press.stories.list(tenant="t1") if s.topic_key
    )
    assert story.topic_key == survey.topic_key
    assert story.source == "desk"  # model-less host: still publishable
    assert story.genres == ("local",)
    # Contributors stayed principals: the cluster's two voices split 1.0.
    assert round(sum(s.weight for s in story.lineage), 6) == 1.0
    # The rendered source table IS the stored one — and the survey row
    # rides it.
    alice = ident.token("alice")
    detail = gateway.handle(
        _req("GET", f"/v1/press/stories/{story.story_id}", token=alice)
    )
    assert detail.body["topic_key"] == survey.topic_key
    assert detail.body["sources"] == press.stories.sources_of(story.story_id)
    kinds = [row["kind"] for row in detail.body["sources"]]
    assert kinds.count("contribution") == 2 and "survey" in kinds

    # A consented COMPLETED read of the topic post writes the kind
    # book's engaged side — the exploration draws learn from reading.
    assert gateway.handle(
        _req(
            "PUT", "/v1/settings", token=alice,
            body={"changes": {"press.personalize": True}},
        )
    ).status == 200
    assert gateway.handle(
        _req(
            "POST", f"/v1/press/stories/{story.story_id}/feedback",
            token=alice,
            body={"signal": "read", "dwell_ms": 9000, "completed": True},
        )
    ).body == {"recorded": True}
    book = press.topics.kind_book(tenant="t1")
    assert book["cluster"][1] == 1  # engaged
    conn.close()
