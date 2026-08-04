"""The publication desk (N5): push to the readers it fits, exactly once.

Exit gate (news-agent-benchmark-roadmap, phase N5): a post lands only
with members whose consented signals clear the threshold (plus the
serendipity slot — tastes never fully close), as the expandable block;
a member without signals keeps the neutral digest they subscribed to;
delivery receipts are exactly-once per (post, member) — a member's
morning never repeats a story they were already handed, and a morning
with nothing new says so honestly; the engagement report over the
receipts is the same numbers the benchmark aggregate shows (they are
one store: pushed → opened → finished → liked); and deliveries ride
account erasure like every per-member row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from test_http_gateway import NOW as GATEWAY_NOW
from test_http_gateway import _app, _req

from oolu.durable.connection import DurableConnection
from oolu.gateway import GatewayApp
from oolu.identity import LocalAccountService, LocalUserStore
from oolu.press import (
    EDITION_PULSE_GOAL,
    MATCH_FLOOR,
    ContributionStore,
    GenreDemandStore,
    IntakeStore,
    PairwiseStore,
    PreferenceStore,
    PressDesk,
    Story,
    StoryMetricsStore,
    StoryStore,
    SurveyDesk,
    SurveyStore,
    TopicBriefStore,
    match_edition,
    rank_edition,
)
from oolu.press.newsroom import LineageShare
from oolu.settings_node import SettingsNode, SettingsStore
from oolu.social import AssistantHistoryStore

NOW = datetime(2026, 8, 4, 8, 0, tzinfo=UTC)


def _story(sid, genre, selection, *, hours=0):
    return Story(
        story_id=sid,
        tenant_id="t1",
        headline=f"Story {sid}",
        prose=f"The whole telling of {sid}.",
        genres=(genre,),
        lineage=(
            LineageShare(contribution_id=f"c-{sid}", author="alice",
                         weight=1.0),
        ),
        breakdown={"selection": selection},
        rubric_version=1,
        source="desk",
        created_at=NOW + timedelta(hours=hours),
    )


# --------------------------------------------------------------------------- #
# The matcher: the threshold, the slice, the neutral digest.                   #
# --------------------------------------------------------------------------- #
def test_the_threshold_gates_and_the_serendipity_slot_survives_it():
    stories = [
        _story("f1", "food", 0.5),
        _story("f2", "food", 0.4, hours=1),
        _story("s1", "sport", 0.2, hours=2),
        _story("l1", "local", 0.1, hours=3),
    ]
    affinity = {"food": 1.0}
    matched = match_edition(stories, affinity=affinity)
    # Food clears the bar (0.5·1.5, 0.4·1.5); sport (0.2) and local
    # (0.1) do not — but the whole edition leaning in hands the LAST
    # slot to the best story OUTSIDE the leaning, below the bar or not:
    # tastes never fully close.
    assert [s.story_id for s in matched] == ["f1", "s1"]
    # A hostile bend suppresses: the same food stories with the taste
    # against them clear nothing, and with no outside leaning to
    # balance, nothing lands at all.
    assert (
        match_edition(
            [_story("f1", "food", 0.5)], affinity={"food": -1.0}
        )
        == []
    )
    # The floor is the named constant — a bent score AT the bar lands.
    at_bar = _story("x1", "quiet", MATCH_FLOOR)
    assert match_edition([at_bar], affinity={"food": 1.0}) == [at_bar]


def test_a_member_without_signals_keeps_the_neutral_digest():
    stories = [_story(f"n{i}", "local", 0.1 * i) for i in range(8)]
    # Nothing to match on: the subscribed digest, exactly as ranked.
    assert match_edition(stories) == rank_edition(stories)


# --------------------------------------------------------------------------- #
# The receipts: exactly-once, one store with the benchmark numbers.            #
# --------------------------------------------------------------------------- #
def test_deliveries_are_exactly_once_and_ride_the_aggregate(tmp_path):
    conn = DurableConnection(tmp_path / "metrics.db")
    metrics = StoryMetricsStore(conn)
    metrics.record_pushed(
        tenant="t1", story_ids=["s1", "s2"], principal="alice"
    )
    metrics.record_pushed(tenant="t1", story_ids=["s1"], principal="alice")
    metrics.record_pushed(tenant="t1", story_ids=["s1"], principal="bob")
    # A re-push is a no-op; two members are two receipts.
    assert metrics.pushed_ids(tenant="t1", principal="alice") == {"s1", "s2"}
    assert metrics.pushed_count(tenant="t1", story_id="s1") == 2
    # The report's denominator rides BOTH aggregate shapes — below the
    # floor it is the only number, and it reveals no reader.
    veiled = metrics.aggregate(tenant="t1", story_id="s1")
    assert veiled == {
        "story_id": "s1",
        "revealed": False,
        "reason": "not enough readers yet",
        "pushed": 2,
    }
    # Erasure removes the member's delivery rows with everything else.
    assert metrics.erase(tenant="t1", principal="alice") == 2
    assert metrics.pushed_count(tenant="t1", story_id="s1") == 1
    conn.close()


# --------------------------------------------------------------------------- #
# The gateway: two mornings, one delivery.                                     #
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
    accounts.create_user("alice", "alice-password-1", tenant="t1")
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


def test_two_mornings_push_each_story_once_and_say_so(tmp_path):
    gateway, conn, ident, press = _host(tmp_path)
    for author, title, body in (
        ("alice", "Harbor market reopened", HARBOR),
        ("bob-less", "Market back at the harbor", HARBOR_AGREE),
    ):
        press.publish(
            tenant="t1", author=author, title=title, body=body,
            genres=("local",), license="oolu-members-1", consent=True,
        )
    alice = ident.token("alice")
    assert gateway.handle(
        _req("POST", "/v1/press/newsroom/run", token=alice)
    ).status == 200
    scheduled = gateway.handle(
        _req(
            "POST", "/v1/press/edition/schedule", token=alice,
            body={"at_minute": 8 * 60}, now=GATEWAY_NOW,
        )
    )
    assert scheduled.body["edition_schedule"]["goal"] == EDITION_PULSE_GOAL

    def fire(day):
        morning = (GATEWAY_NOW + timedelta(days=day)).replace(
            hour=8, minute=0, second=0, microsecond=0
        )
        gateway._pulse_gate = 0.0
        gateway.handle(
            _req("GET", "/v1/press/stories", token=alice, now=morning)
        )
        return gateway._assistant_history.history(
            tenant="t1", principal="alice", agent="news"
        )

    first = fire(1)
    assert first[-1]["block"]["kind"] == "story"
    [story] = press.stories.list(tenant="t1")
    assert press.metrics.pushed_count(
        tenant="t1", story_id=story.story_id
    ) == 1
    # The engagement report and the benchmark aggregate are one store.
    metrics = gateway.handle(
        _req(
            "GET", f"/v1/press/stories/{story.story_id}/metrics",
            token=alice,
        )
    )
    assert metrics.body["pushed"] == 1

    second = fire(2)
    # The second morning has nothing NEW: the words say so, no block
    # rides, and the receipt count never moved — exactly-once.
    assert "nothing new on the shelf" in second[-1]["body"]
    assert second[-1]["block"] is None
    assert press.metrics.pushed_count(
        tenant="t1", story_id=story.story_id
    ) == 1
    conn.close()
