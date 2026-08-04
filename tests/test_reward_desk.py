"""The reward desk (N6): revenue flows back over the FULL source table.

Exit gate (news-agent-benchmark-roadmap, phase N6): a settled post's
payouts sum exactly to the revenue less the named commission — one
conserved split through the standing PricingEngine, no parallel
pipeline; every payout row resolves to a source row (the citation table
beside the ledger agrees with the stored provenance); an erased
member's future shares stop while settled history stays balanced; and
nothing pays twice (idempotent settlement, the A5 law). A post with
lineage alone pays exactly what A5 always paid.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from test_http_gateway import _app, _req

from oolu.adhouse.delivery import AD_COMMISSION_ALPHA
from oolu.billing import (
    AdDividendService,
    BillingService,
    EarningsLedger,
    ad_event_id,
)
from oolu.billing.doubleentry import DoubleEntryLedger
from oolu.durable.connection import DurableConnection
from oolu.durable.idempotency import IdempotencyLedger
from oolu.explorer.evidence import LabReport, ProductReview
from oolu.gateway import GatewayApp
from oolu.identity import LocalAccountService, LocalUserStore
from oolu.legal import LEGAL_VERSIONS
from oolu.press import (
    TRANCHE_LINEAGE,
    TRANCHE_RESEARCH,
    TRANCHE_SURVEY,
    ContributionStore,
    LineageShare,
    PairwiseStore,
    PreferenceStore,
    PressDesk,
    RewardCitationStore,
    Story,
    StoryStore,
    SurveyDesk,
    SurveyStore,
    merged_shares,
    source_split,
)
from oolu.settings_node import SettingsNode, SettingsStore
from oolu.social import AssistantHistoryStore

NOW = datetime(2026, 8, 4, 9, 0, tzinfo=UTC)


def _lineage(*pairs):
    return tuple(
        LineageShare(contribution_id=cid, author=author, weight=weight)
        for cid, author, weight in pairs
    )


# --------------------------------------------------------------------------- #
# The split: tranches over the classes present, summing exactly to 1.0.        #
# --------------------------------------------------------------------------- #
def test_lineage_alone_pays_exactly_what_a5_always_paid():
    recorded = _lineage(("c-1", "alice", 0.6), ("c-2", "bob", 0.4))
    split = source_split(lineage=recorded)
    # The A5 case unchanged: the recorded weights ARE the split, and
    # each share cites the lineage row it pays for.
    assert [(s.principal, s.weight) for s in split] == [
        ("alice", 0.6),
        ("bob", 0.4),
    ]
    assert [s.citation for s in split] == ["lineage:c-1", "lineage:c-2"]
    assert abs(sum(s.weight for s in split) - 1.0) < 1e-9


def test_the_full_table_splits_by_tranche_and_sums_to_one():
    split = source_split(
        lineage=_lineage(("c-1", "alice", 1.0)),
        respondents={"sv-1": ["bob", "carol"]},
        research=[
            ("lab", "listing-42", ["dana"]),
            ("feedback", "listing-42", ["erin"]),
        ],
    )
    by_citation = {(s.principal, s.citation): s.weight for s in split}
    assert by_citation[("alice", "lineage:c-1")] == TRANCHE_LINEAGE
    # The survey tranche splits EVENLY over the retained respondents.
    assert by_citation[("bob", "survey:sv-1")] == TRANCHE_SURVEY / 2
    assert by_citation[("carol", "survey:sv-1")] == TRANCHE_SURVEY / 2
    # The research tranche splits evenly over the anchoring members.
    assert by_citation[("dana", "lab:listing-42")] == TRANCHE_RESEARCH / 2
    assert (
        by_citation[("erin", "feedback:listing-42")] == TRANCHE_RESEARCH / 2
    )
    assert abs(sum(s.weight for s in split) - 1.0) < 1e-9


def test_absent_classes_renormalize_never_leak_to_the_platform():
    # Lineage + survey, no research: the two tranches share the whole.
    partial = source_split(
        lineage=_lineage(("c-1", "alice", 1.0)),
        respondents={"sv-1": ["bob"]},
    )
    weights = {s.principal: s.weight for s in partial}
    scale = TRANCHE_LINEAGE + TRANCHE_SURVEY
    assert abs(weights["alice"] - TRANCHE_LINEAGE / scale) < 1e-9
    assert abs(weights["bob"] - TRANCHE_SURVEY / scale) < 1e-9
    assert abs(sum(s.weight for s in partial) - 1.0) < 1e-9
    # A single class carries everything; nothing at all is honestly [].
    solo = source_split(respondents={"sv-1": ["bob", "carol", "dana"]})
    assert abs(sum(s.weight for s in solo) - 1.0) < 1e-9
    assert {s.principal for s in solo} == {"bob", "carol", "dana"}
    assert source_split() == []
    # Empty respondent sets and unknown research kinds never count.
    assert source_split(respondents={"sv-1": []}) == []
    assert source_split(research=[("rumor", "x", ["bob"])]) == []


def test_a_member_in_two_classes_merges_for_the_engine_with_both_citations():
    split = source_split(
        lineage=_lineage(("c-1", "alice", 1.0)),
        respondents={"sv-1": ["alice", "bob"]},
    )
    # Two citations for alice — the finer grain the citation table keeps.
    assert [s.citation for s in split if s.principal == "alice"] == [
        "lineage:c-1",
        "survey:sv-1",
    ]
    # One engine line for alice — the PricingEngine pays per principal.
    merged = dict(merged_shares(split))
    assert set(merged) == {"alice", "bob"}
    assert abs(sum(merged.values()) - 1.0) < 1e-9
    scale = TRANCHE_LINEAGE + TRANCHE_SURVEY
    assert (
        abs(merged["alice"] - (TRANCHE_LINEAGE + TRANCHE_SURVEY / 2) / scale)
        < 1e-9
    )


# --------------------------------------------------------------------------- #
# The citation store: keyed, replay-proof, ordered out.                        #
# --------------------------------------------------------------------------- #
def test_citations_record_once_and_read_back_ordered(tmp_path):
    conn = DurableConnection(tmp_path / "rewards.db")
    store = RewardCitationStore(conn)
    shares = source_split(
        lineage=_lineage(("c-1", "alice", 1.0)),
        respondents={"sv-1": ["bob"]},
    )
    assert store.record(tenant="t1", event_id="ad:pl-1", shares=shares) == 2
    # A replayed settle re-records nothing — keyed inserts, the A5 law.
    assert store.record(tenant="t1", event_id="ad:pl-1", shares=shares) == 0
    rows = store.of("ad:pl-1", tenant="t1")
    assert [(r["principal"], r["citation"]) for r in rows] == [
        ("alice", "lineage:c-1"),
        ("bob", "survey:sv-1"),
    ]
    assert store.of("ad:pl-1", tenant="t2") == []
    conn.close()


# --------------------------------------------------------------------------- #
# The gateway: the full loop — settle, cite, resolve, erase, never twice.      #
# --------------------------------------------------------------------------- #
def _host(tmp_path):
    app, conn, ident = _app(tmp_path)
    users = LocalUserStore(":memory:")
    accounts = LocalAccountService(users, ident.store, ident._signer)
    for name in ("alice", "bob", "carol", "dave", "erin", "frank",
                 "gina", "shop"):
        accounts.create_user(name, f"{name}-password-1", tenant="t1")
    ledger = EarningsLedger(conn)
    pairwise = PairwiseStore(conn)
    press = PressDesk(
        ContributionStore(conn),
        stories=StoryStore(conn),
        preferences=PreferenceStore(conn),
        pairwise=pairwise,
        surveys=SurveyDesk(SurveyStore(conn), pairwise),
        rewards=RewardCitationStore(conn),
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
        billing=BillingService(ledger),
        # Money moves in THIS test: the production shim stands in for
        # the durable the guard inspects, so the whole loop runs.
        ad_dividend=AdDividendService(
            ledger=ledger,
            book=DoubleEntryLedger(conn),
            durable=SimpleNamespace(is_production_durable=True),
            providers=[],
            idempotency=IdempotencyLedger(conn),
        ),
    )
    gateway._commerce_seller_kyc.apply(
        tenant="t1",
        principal="shop",
        legal_name="Shop Ltd",
        company_email="ads@shop-ltd.example",
    )
    gateway._commerce_seller_kyc.decide(
        tenant="t1", principal="shop", reviewer="rev", approved=True
    )
    return gateway, conn, ident, press, ledger


def _topic_post(press):
    """A composed topic post with the full source table: contributor
    lineage, a cited survey, and lab/feedback research on a listing."""
    story = Story(
        story_id="post-1",
        tenant_id="t1",
        headline="The steel kettle, measured",
        prose="Four minutes to a litre; the survey called it worth it.",
        genres=("products",),
        lineage=_lineage(("c-1", "alice", 1.0)),
        breakdown={"selection": 0.8},
        rubric_version=1,
        source="desk",
        created_at=NOW,
        topic_key="gap:listing-42",
    )
    press.stories.insert(
        story,
        sources=[
            {"kind": "listing", "ref": "listing-42",
             "summary": "“Steel kettle” by shop, 29.00"},
            {"kind": "contribution", "ref": "c-1",
             "summary": "“The kettle, tested” by alice"},
            {"kind": "survey", "ref": "sv-1",
             "summary": "Worth it? — Reader survey (2 answers)"},
            {"kind": "lab", "ref": "listing-42",
             "summary": "lab mean score 88 over 1 member report"},
            {"kind": "feedback", "ref": "listing-42",
             "summary": "verified feedback mean 5 over 1 review"},
        ],
    )
    # The survey's retained pseudonymous respondents (N3's store).
    for who in ("carol", "dave"):
        press.surveys.store.record_answer(
            "sv-1", tenant="t1", principal=who, choice="worth"
        )
    return story


def _research_rows(gateway):
    gateway._explorer_lab.insert(
        LabReport(
            report_id="lab-1",
            tenant_id="t1",
            listing_id="listing-42",
            contribution_id="c-lab",
            author="erin",
            score=88,
            metrics={"minutes_to_litre": 4},
            created_at=NOW,
        )
    )
    gateway._explorer_reviews.store.insert(
        ProductReview(
            review_id="rev-1",
            tenant_id="t1",
            listing_id="listing-42",
            reviewer="frank",
            order_id="o-1",
            rating=5,
            words="Boils fast, handle stays cool.",
            created_at=NOW,
        )
    )


def _impress(gateway, ident, viewer):
    token = ident.token(viewer)
    gateway.handle(
        _req(
            "POST",
            "/v1/legal/consent",
            token=token,
            body={"document": "privacy", "version": LEGAL_VERSIONS["privacy"]},
        )
    )
    placement = gateway.handle(
        _req(
            "GET",
            "/v1/press/ads",
            token=token,
            query={"surface": "edition", "content": "post-1"},
        )
    ).body["placement"]
    assert placement is not None
    gateway.handle(
        _req(
            "POST",
            f"/v1/adhouse/placements/{placement['placement_id']}/impression",
            token=token,
        )
    )
    return placement["placement_id"]


def test_the_settled_post_pays_the_full_source_table_and_cites_every_row(
    tmp_path,
):
    gateway, conn, ident, press, ledger = _host(tmp_path)
    story = _topic_post(press)
    _research_rows(gateway)
    shop = ident.token("shop")
    assert (
        gateway.handle(
            _req(
                "POST",
                "/v1/adhouse/campaigns",
                token=shop,
                body={
                    "name": "Kettle week",
                    "creative": "Kettles for every kitchen.",
                    "offer_ref": "listing-42",
                    "genres": ["products"],
                    "bid_micros": 10_000,
                    "budget_micros": 1_000_000,
                },
            )
        ).status
        == 201
    )
    pid = _impress(gateway, ident, "bob")

    outcome = gateway.handle(
        _req("POST", "/v1/adhouse/settle", token=ident.token("shop"))
    )
    assert outcome.status == 200 and outcome.body["settled"] == 1
    event_id = ad_event_id(pid)

    # Payouts sum EXACTLY to the revenue less the named commission —
    # the standing engine, conserved to the micro.
    placement = gateway._ad_placements.get(pid, tenant="t1")
    net = placement.price_micros
    entries = ledger.entries_for_event(event_id)
    assert sum(e.amount_micros for e in entries) == net - round(
        net * AD_COMMISSION_ALPHA
    )
    # Every class of source work was paid: the contributor, both
    # respondents, the lab author, the reviewer.
    paid = {e.noder_principal for e in entries}
    assert paid == {"alice", "carol", "dave", "erin", "frank"}
    # The contributor's tranche leads; the finer grain is the split.
    micros = {e.noder_principal: e.amount_micros for e in entries}
    assert micros["alice"] > micros["carol"] > micros["erin"]

    # Every payout row resolves to a source row — the citation table
    # beside the ledger, checked against the STORED provenance.
    citations = press.rewards.of(event_id, tenant="t1")
    assert {c["principal"] for c in citations} == paid
    stored = {
        (row["kind"], row["ref"])
        for row in press.stories.sources_of(story.story_id)
    }
    lineage_refs = {s.contribution_id for s in story.lineage}
    for row in citations:
        kind, _, ref = row["citation"].partition(":")
        if kind == "lineage":
            assert ref in lineage_refs
        else:
            assert (kind, ref) in stored
    # The citation weights are the split itself, summing to the whole.
    assert abs(sum(c["weight"] for c in citations) - 1.0) < 1e-9

    # Nothing pays twice: a second crank replays the settle (cached)
    # and re-records nothing — ledger and citations both stand.
    again = gateway.handle(
        _req("POST", "/v1/adhouse/settle", token=ident.token("shop"))
    )
    assert again.status == 200
    assert len(ledger.entries_for_event(event_id)) == len(entries)
    assert press.rewards.of(event_id, tenant="t1") == citations
    conn.close()


def test_an_erased_members_future_shares_stop_while_history_stands(tmp_path):
    gateway, conn, ident, press, ledger = _host(tmp_path)
    _topic_post(press)
    _research_rows(gateway)
    gateway.handle(
        _req(
            "POST",
            "/v1/adhouse/campaigns",
            token=ident.token("shop"),
            body={
                "name": "Kettle week",
                "creative": "Kettles for every kitchen.",
                "offer_ref": "listing-42",
                "genres": ["products"],
                "bid_micros": 10_000,
                "budget_micros": 1_000_000,
            },
        )
    )
    first_pid = _impress(gateway, ident, "bob")
    gateway.handle(
        _req("POST", "/v1/adhouse/settle", token=ident.token("shop"))
    )
    first_event = ad_event_id(first_pid)
    assert "carol" in {
        e.noder_principal for e in ledger.entries_for_event(first_event)
    }

    # Carol's account erasure deletes her survey rows (the N3 law); the
    # next settle simply no longer finds her — no denylist, no flag.
    press.surveys.store.erase(tenant="t1", principal="carol")
    second_pid = _impress(gateway, ident, "gina")
    gateway.handle(
        _req("POST", "/v1/adhouse/settle", token=ident.token("shop"))
    )
    second_event = ad_event_id(second_pid)
    second = ledger.entries_for_event(second_event)
    assert "carol" not in {e.noder_principal for e in second}
    # Dave now carries the survey tranche alone — the split
    # renormalized over who remains, conserved as ever.
    placement = gateway._ad_placements.get(second_pid, tenant="t1")
    assert sum(e.amount_micros for e in second) == (
        placement.price_micros
        - round(placement.price_micros * AD_COMMISSION_ALPHA)
    )
    citations = {
        c["principal"]: c
        for c in press.rewards.of(second_event, tenant="t1")
    }
    assert citations["dave"]["weight"] == TRANCHE_SURVEY
    # History stays balanced: the settled first event keeps carol's
    # money AND her citation — the financial record, retained.
    assert "carol" in {
        e.noder_principal for e in ledger.entries_for_event(first_event)
    }
    assert "carol" in {
        c["principal"] for c in press.rewards.of(first_event, tenant="t1")
    }
    conn.close()
