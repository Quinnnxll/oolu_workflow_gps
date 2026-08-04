"""The survey desk (N3): ask the members what the records cannot tell.

Exit gate (news-agent-benchmark-roadmap, phase N3): a survey question
renders as a block and one answer per member is idempotent (replaying
the same answer is quiet; changing it is refused — the first answer is
the answer); below the floor the aggregate refuses to render and no
individual answer is EVER rendered; consent off → unsampled and
unrecordable, and the door says so; erasure removes the member's
answers and sample memberships and the aggregates honestly shrink; a
telling-kind answer writes the member's own pairwise preference row
(the book's first writer since the poll floor closed); the random
sample is bounded and replays from the survey's recorded seed; and a
survey source row (survey id, topic key, question, sample size, the
floored aggregate) resolves for any post to cite (N4).
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest
from test_http_gateway import NOW as GATEWAY_NOW
from test_http_gateway import _app, _req

from oolu.durable.connection import DurableConnection
from oolu.gateway import GatewayApp
from oolu.identity import LocalAccountService, LocalUserStore
from oolu.press import (
    SURVEY_K_FLOOR,
    SURVEY_TTL_HOURS,
    ContributionStore,
    GenreDemandStore,
    IntakeStore,
    PairwiseStore,
    PreferenceStore,
    PressDesk,
    PressError,
    StoryMetricsStore,
    StoryStore,
    SurveyDesk,
    SurveyStore,
    TopicBriefStore,
    compose_survey,
    draw_sample,
)
from oolu.settings_node import SettingsNode, SettingsStore
from oolu.social import AssistantHistoryStore

NOW = datetime(2026, 8, 4, 8, 0, tzinfo=UTC)

CLUSTER_BRIEF = {
    "topic_key": "cluster:c1",
    "tenant_id": "t1",
    "subject": "2 members are telling the same story: “Harbor market reopened”",
    "facts": [
        {"kind": "contribution", "ref": "c1",
         "summary": "“Harbor market reopened” by alice"},
        {"kind": "contribution", "ref": "c2",
         "summary": "“Market back at the harbor” by bob"},
    ],
}

PRICE_BRIEF = {
    "topic_key": "price:l1",
    "tenant_id": "t1",
    "subject": "The steel kettle: 15% off its recorded list price",
    "facts": [{"kind": "listing", "ref": "l1", "summary": "“The steel kettle”"}],
}


# --------------------------------------------------------------------------- #
# Composition and the sample draw.                                             #
# --------------------------------------------------------------------------- #
def test_a_cluster_brief_asks_which_telling_and_others_ask_worth():
    telling = compose_survey(CLUSTER_BRIEF, survey_id="s1", opened_at=NOW)
    assert telling.kind == "telling"
    assert "Which telling serves the reader better?" in telling.question
    assert [o.key for o in telling.options] == ["left", "right"]
    assert telling.options[0].contribution_id == "c1"
    assert str(SURVEY_K_FLOOR) in telling.reason  # the honest why

    editorial = compose_survey(PRICE_BRIEF, survey_id="s2", opened_at=NOW)
    assert editorial.kind == "editorial"
    assert "Is this worth a full story?" in editorial.question
    assert [o.key for o in editorial.options] == [
        "worth", "not_worth", "more_evidence",
    ]


def test_the_sample_is_bounded_and_replays_from_its_seed():
    frame = [f"m{i}" for i in range(40)]
    first = draw_sample(frame, size=12, rng=random.Random(9))
    again = draw_sample(list(reversed(frame)), size=12, rng=random.Random(9))
    # Bounded, order-independent of the frame, replayable from the seed.
    assert len(first) == 12 and first == again
    # A frame smaller than the bound is taken whole — no fake sampling.
    assert draw_sample(["a", "b"], size=12, rng=random.Random(9)) == ["a", "b"]


# --------------------------------------------------------------------------- #
# The answer laws.                                                             #
# --------------------------------------------------------------------------- #
def _desk(tmp_path):
    conn = DurableConnection(tmp_path / "surveys.db")
    pairwise = PairwiseStore(conn)
    desk = SurveyDesk(SurveyStore(conn), pairwise)
    survey = compose_survey(CLUSTER_BRIEF, survey_id="s1", opened_at=NOW)
    desk.store.insert(survey)
    return conn, desk, pairwise


def test_one_answer_once_and_the_floor_holds(tmp_path):
    conn, desk, _ = _desk(tmp_path)
    first = desk.answer("s1", tenant="t1", principal="alice", choice="left")
    assert first["answered"] is True and first["revealed"] is False
    assert first["reason"] == "not enough answers yet"
    # Replaying the same answer is quiet; changing it is refused.
    assert desk.answer(
        "s1", tenant="t1", principal="alice", choice="left"
    )["answered"] is True
    with pytest.raises(PressError, match="first answer is the answer"):
        desk.answer("s1", tenant="t1", principal="alice", choice="right")
    # An unknown option is refused by name; an unanswered member sees
    # nothing — answer first.
    with pytest.raises(PressError, match="choice must be one of"):
        desk.answer("s1", tenant="t1", principal="bob", choice="maybe")
    veiled = desk.reveal("s1", tenant="t1", principal="bob")
    assert veiled["answered"] is False and veiled["revealed"] is False
    # At the floor the aggregate reveals — counts only, never a name.
    for i in range(SURVEY_K_FLOOR - 1):
        desk.answer(
            "s1", tenant="t1", principal=f"m{i}",
            choice="left" if i % 2 else "right",
        )
    revealed = desk.reveal("s1", tenant="t1", principal="alice")
    assert revealed["revealed"] is True
    assert sum(revealed["counts"].values()) == SURVEY_K_FLOOR
    assert set(revealed) <= {
        "survey_id", "answered", "choice", "revealed", "counts", "total",
    }
    conn.close()


def test_a_telling_answer_writes_the_pairwise_book_under_consent(tmp_path):
    conn, desk, pairwise = _desk(tmp_path)
    # Without learning: the aggregate counts, the book stays untouched.
    desk.answer("s1", tenant="t1", principal="quiet", choice="left")
    assert pairwise.export(tenant="t1", principal="quiet") == []
    # With learning: the member's own DPO-shaped row, chosen vs rejected.
    desk.answer(
        "s1", tenant="t1", principal="alice", choice="left", learning=True
    )
    [pair] = pairwise.export(tenant="t1", principal="alice")
    assert "Which telling serves the reader better?" in pair["prompt"]
    assert "by alice" in pair["chosen"] and "by bob" in pair["rejected"]
    conn.close()


def test_erasure_shrinks_the_aggregate_and_the_sample(tmp_path):
    conn, desk, _ = _desk(tmp_path)
    desk.store.record_sample("s1", tenant="t1", principals=["alice", "bob"])
    for member in ("alice", "bob", "carol"):
        desk.answer("s1", tenant="t1", principal=member, choice="left")
    assert desk.store.erase(tenant="t1", principal="alice") == 2  # answer+sample
    assert desk.store.counts("s1", tenant="t1") == {"left": 2}
    assert desk.store.sample_of("s1", tenant="t1") == ["bob"]
    assert desk.store.respondents("s1", tenant="t1") == ["bob", "carol"]
    conn.close()


def test_expiry_closes_and_the_result_row_resolves(tmp_path):
    conn = DurableConnection(tmp_path / "surveys.db")
    tick = {"now": NOW}
    store = SurveyStore(conn, clock=lambda: tick["now"])
    desk = SurveyDesk(store)
    store.insert(compose_survey(CLUSTER_BRIEF, survey_id="s1", opened_at=NOW))
    assert desk.close_expired(tenant="t1") == []
    tick["now"] = NOW + timedelta(hours=SURVEY_TTL_HOURS)
    [closed] = desk.close_expired(tenant="t1")
    assert closed.survey_id == "s1"
    assert desk.store.get("s1", tenant="t1").status == "closed"
    # A closed survey refuses fresh answers...
    with pytest.raises(PressError, match="closed"):
        desk.answer("s1", tenant="t1", principal="late", choice="left")
    # ...and its SOURCE ROW resolves for a post to cite: id, topic,
    # question, sample size, the floored aggregate.
    row = desk.result_row("s1", tenant="t1")
    assert row["survey_id"] == "s1" and row["topic_key"] == "cluster:c1"
    assert row["status"] == "closed" and row["answers"] == 0
    assert row["reason"] == "not enough answers yet" and "counts" not in row
    conn.close()


# --------------------------------------------------------------------------- #
# The gateway: consent first, the sample lands, the thread speaks.             #
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
    for name in ("alice", "bob", "carol"):
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


def test_the_desk_opens_one_survey_samples_consented_members_and_answers(
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
    alice, bob = ident.token("alice"), ident.token("bob")
    # Alice subscribes to the edition AND consents; bob consents only —
    # the frame is edition subscribers with the consent, so the sample
    # is exactly alice.
    assert gateway.handle(
        _req(
            "POST", "/v1/press/edition/schedule", token=alice,
            body={"at_minute": 8 * 60}, now=GATEWAY_NOW,
        )
    ).status == 200
    for token in (alice, bob):
        assert gateway.handle(
            _req(
                "PUT", "/v1/settings", token=token,
                body={"changes": {"press.personalize": True}},
            )
        ).status == 200

    gateway._topic_reading("t1")
    gateway._survey_tick("t1")
    [survey] = press.surveys.store.list(tenant="t1", status="open")
    assert survey.kind == "telling" and survey.draw_seed is not None
    assert press.surveys.store.sample_of(survey.survey_id, tenant="t1") == [
        "alice"
    ]
    # A second tick opens nothing — one question at a time.
    gateway._survey_tick("t1")
    assert len(press.surveys.store.list(tenant="t1", status="open")) == 1
    # The sampled member's thread carries the question BLOCK with the
    # honest why — persisted, so every device renders it.
    news = gateway._assistant_history.history(
        tenant="t1", principal="alice", agent="news"
    )
    assert news[-1]["block"]["kind"] == "survey"
    assert news[-1]["block"]["survey"]["survey_id"] == survey.survey_id
    assert "drawn at random" in news[-1]["body"]
    # Carol never consented: her answer is UNRECORDED and the door says
    # so — nothing per-member is written.
    cold = gateway.handle(
        _req(
            "POST", f"/v1/press/surveys/{survey.survey_id}/answer",
            token=ident.token("carol"), body={"choice": "left"},
        )
    )
    assert cold.body == {"recorded": False, "reason": "personalization is off"}
    assert press.surveys.store.counts(survey.survey_id, tenant="t1") == {}
    # Bob volunteers through the open list — direct opinion collection.
    listed = gateway.handle(_req("GET", "/v1/press/surveys", token=bob))
    assert listed.body["items"][0]["survey_id"] == survey.survey_id
    answered = gateway.handle(
        _req(
            "POST", f"/v1/press/surveys/{survey.survey_id}/answer",
            token=bob, body={"choice": "right"},
        )
    )
    assert answered.body["recorded"] is True
    assert answered.body["revealed"] is False  # the floor holds
    # The telling answer wrote bob's OWN pairwise row.
    [pair] = press.pairwise.export(tenant="t1", principal="bob")
    assert "Which telling" in pair["prompt"]
    # Changing his mind is refused — the first answer is the answer.
    stands = gateway.handle(
        _req(
            "POST", f"/v1/press/surveys/{survey.survey_id}/answer",
            token=bob, body={"choice": "left"},
        )
    )
    assert stands.status == 409
    # "surveys" in the News thread answers with the open question block.
    spoken = gateway.handle(
        _req(
            "POST", "/v1/chat", token=bob,
            body={"message": "surveys", "agent": "news"},
        )
    )
    assert spoken.body["block"]["kind"] == "survey"
    assert spoken.body["block"]["survey"]["mine"] == "right"
    conn.close()
