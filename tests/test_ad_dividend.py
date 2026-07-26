"""The contributor dividend (A5): verified delivery pays — conserved, gated.

Exit gate (agents-expansion plan, phase A5): `net == platform + Σ
contributor` to the micro, property-tested; self-dealing produces no
event, replays double-count nothing, and multipliers redistribute
without inflating; an upheld dispute claws back reserve-first through
the STANDING machinery; earnings render with source labels and ride the
standing balance/holdback maths; no ad money moves on local-only infra
(the guard, at the service and at the door); and the A4 funding plus the
A5 recognition plus an ordinary order fee live on ONE book that trial-
balances to zero — no double counting.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_http_gateway import _app, _req

from oolu.adhouse import Placement
from oolu.billing import (
    AdDividendService,
    BillingService,
    DisputeService,
    DisputeStore,
    EarningsLedger,
    MoneyModeError,
    ad_event_id,
)
from oolu.billing.doubleentry import (
    DoubleEntryLedger,
    LedgerEntry,
    LedgerTransaction,
)
from oolu.billing.models import EarningsKind
from oolu.durable.audit import DurableAuditLog
from oolu.durable.connection import DurableConnection
from oolu.durable.idempotency import IdempotencyLedger
from oolu.gateway import GatewayApp
from oolu.identity import LocalAccountService, LocalUserStore
from oolu.legal import LEGAL_VERSIONS
from oolu.press import (
    LICENSES,
    ContributionStore,
    PairwiseStore,
    PollDesk,
    PollStore,
    PreferenceStore,
    PressDesk,
    StoryStore,
)
from oolu.settings_node import SettingsNode, SettingsStore

NOW = datetime(2026, 7, 16, 9, 0, tzinfo=UTC)


def _placement(price=7_000, viewer="reader", placement_id="pl-1"):
    return Placement(
        placement_id=placement_id,
        tenant_id="t1",
        campaign_id="camp-1",
        surface="edition",
        content_ref="story-1",
        viewer=viewer,
        price_micros=price,
        breakdown={},
        at=NOW,
    )


def _service(tmp_path, *, production=True):
    conn = DurableConnection(tmp_path / "dividend.db")
    ledger = EarningsLedger(conn)
    book = DoubleEntryLedger(conn)
    durable = SimpleNamespace(
        is_production_durable=production, audit=DurableAuditLog(conn)
    )
    service = AdDividendService(
        ledger=ledger,
        book=book,
        durable=durable,
        providers=[],  # no HMAC offenders: production identity passes
        idempotency=IdempotencyLedger(conn),
        clock=lambda: NOW,
    )
    return conn, service, ledger, book, durable


# --------------------------------------------------------------------------- #
# Conservation, replay, holdback.                                              #
# --------------------------------------------------------------------------- #
def test_a_verified_impression_pays_conserved_to_the_micro(tmp_path):
    conn, service, ledger, book, _ = _service(tmp_path)
    rng = random.Random(11)
    for i in range(25):  # the property, across odd prices and share sets
        price = rng.randrange(1_000, 50_000)
        shares = [("alice", 0.6), ("bob", 0.3), ("carol", 0.1)]
        outcome = service.settle_impression(
            _placement(price=price, placement_id=f"pl-{i}"), shares=shares
        )
        assert outcome["settled"] is True
        assert (
            outcome["platform_micros"]
            + sum(outcome["contributor_accruals"].values())
            == outcome["net_micros"]
            == price
        )
    # The accruals landed on the STANDING ledger with the holdback.
    [first, *_] = ledger.entries("alice")
    assert first.kind is EarningsKind.ACCRUAL
    assert first.available_at == NOW + timedelta(days=14)
    conn.close()


def test_replays_double_count_nothing(tmp_path):
    conn, service, ledger, _, _ = _service(tmp_path)
    shares = [("alice", 1.0)]
    first = service.settle_impression(_placement(), shares=shares)
    again = service.settle_impression(_placement(), shares=shares)
    assert again == first
    assert len(ledger.entries_for_event(ad_event_id("pl-1"))) == 1
    conn.close()


# --------------------------------------------------------------------------- #
# Fraud: self-dealing, arms-length, multipliers.                               #
# --------------------------------------------------------------------------- #
def test_self_dealing_produces_no_event(tmp_path):
    conn, service, ledger, book, _ = _service(tmp_path)
    # The viewer is the ONLY contributor: no arms-length audience — no
    # accrual, no recognition, nothing.
    outcome = service.settle_impression(
        _placement(viewer="alice"), shares=[("alice", 1.0)]
    )
    assert outcome["settled"] is False
    assert "no_arms_length_share" in outcome["reasons"]
    assert ledger.entries("alice") == []
    assert book.transactions() == []
    # The viewer among OTHERS: their share is excluded by name; the rest
    # still conserves — platform + bob == the whole net.
    shared = service.settle_impression(
        _placement(viewer="alice", placement_id="pl-2"),
        shares=[("alice", 0.6), ("bob", 0.4)],
    )
    assert shared["settled"] is True
    assert "self_dealing:alice" in shared["excluded"]
    assert "alice" not in shared["contributor_accruals"]
    assert (
        shared["platform_micros"] + shared["contributor_accruals"]["bob"]
        == shared["net_micros"]
    )
    conn.close()


def test_multipliers_redistribute_never_inflate(tmp_path):
    conn, service, _, _, _ = _service(tmp_path)
    neutral = service.settle_impression(
        _placement(placement_id="pl-n"),
        shares=[("alice", 0.5), ("bob", 0.5)],
    )
    # A wild reputation multiplier is clamped to mu_max and shifts the
    # SLICE — the pool (and the platform's α) never move.
    tilted = service.settle_impression(
        _placement(placement_id="pl-t"),
        shares=[("alice", 0.5), ("bob", 0.5)],
        multiplier_of=lambda author: 99.0 if author == "alice" else 1.0,
    )
    assert tilted["platform_micros"] == neutral["platform_micros"]
    assert sum(tilted["contributor_accruals"].values()) == sum(
        neutral["contributor_accruals"].values()
    )
    assert (
        tilted["contributor_accruals"]["alice"]
        > tilted["contributor_accruals"]["bob"]
    )
    # mu_max = 2.0 against 1.0: alice holds exactly 2/3 of the pool.
    pool = sum(tilted["contributor_accruals"].values())
    assert abs(tilted["contributor_accruals"]["alice"] - (pool * 2) // 3) <= 1
    conn.close()


# --------------------------------------------------------------------------- #
# The guard: no ad money on local-only infra.                                  #
# --------------------------------------------------------------------------- #
def test_local_infra_refuses_every_settle(tmp_path):
    conn, service, ledger, book, _ = _service(tmp_path, production=False)
    with pytest.raises(MoneyModeError, match="production PostgreSQL"):
        service.settle_impression(_placement(), shares=[("alice", 1.0)])
    assert ledger.entries("alice") == [] and book.transactions() == []
    conn.close()


# --------------------------------------------------------------------------- #
# One book: funding, recognition, and an ordinary order fee — balanced.        #
# --------------------------------------------------------------------------- #
def test_recognition_balances_the_one_book_without_double_counting(tmp_path):
    conn, service, _, book, _ = _service(tmp_path)
    budget = 1_000_000
    book.post(
        LedgerTransaction(
            txn_id="fund-1",
            idempotency_key="ad-fund:camp-1",
            kind="ad.campaign_funded",
            entries=(
                LedgerEntry(account="marketplace_cash", amount_micros=budget),
                LedgerEntry(
                    account="ad_budget_liability", amount_micros=-budget
                ),
            ),
            created_at=NOW,
        )
    )
    outcome = service.settle_impression(
        _placement(price=7_000), shares=[("alice", 1.0)]
    )
    # An ordinary marketplace order fee lands beside it — the §4 rule:
    # two revenue streams, one book, separate postings.
    book.post(
        LedgerTransaction(
            txn_id="cap-1",
            idempotency_key="order-capture:o1",
            kind="capture",
            entries=(
                LedgerEntry(account="marketplace_cash", amount_micros=100_000),
                LedgerEntry(account="seller_payable", amount_micros=-95_000),
                LedgerEntry(
                    account="marketplace_fee_revenue", amount_micros=-5_000
                ),
            ),
            created_at=NOW,
        )
    )
    balances = book.trial_balance()
    assert sum(balances.values()) == 0  # one book, in balance
    # The liability shrank by exactly what was recognized...
    assert balances["ad_budget_liability"] == -(budget - outcome["net_micros"])
    # ...the fee account holds the ad α AND the order take, additively —
    # two postings, no overlap, no double counting.
    assert (
        balances["marketplace_fee_revenue"]
        == -(outcome["platform_micros"] + 5_000)
    )
    assert balances["contributor_payable"] == -sum(
        outcome["contributor_accruals"].values()
    )
    conn.close()


# --------------------------------------------------------------------------- #
# Disputes: the standing clawback, reserve-first, on ad events.                #
# --------------------------------------------------------------------------- #
def test_an_advertiser_dispute_claws_back_through_the_standing_machinery(
    tmp_path,
):
    conn, service, ledger, _, durable = _service(tmp_path)
    outcome = service.settle_impression(
        _placement(), shares=[("alice", 1.0)]
    )
    disputes = DisputeService(
        ledger=ledger,
        disputes=DisputeStore(conn),
        durable=durable,
        providers=[],
        idempotency=IdempotencyLedger(conn),
        clock=lambda: NOW + timedelta(days=1),
    )
    dispute = disputes.open(
        event_id=outcome["event_id"], reason="advertiser chargeback"
    )
    upheld = disputes.uphold(dispute.dispute_id)
    kinds = [e.kind for e in ledger.entries("alice")]
    assert EarningsKind.CLAWBACK in kinds
    clawed = sum(
        e.amount_micros
        for e in ledger.entries_for_event(outcome["event_id"])
    )
    assert clawed == 0  # the accrual and its clawback cancel exactly
    assert (
        upheld["by_noder"]["alice"]["clawed_back_micros"]
        == outcome["contributor_accruals"]["alice"]
    )
    # Held back, never paid out: the clawback lands as debt the next
    # accruals repay first — the standing shortfall discipline.
    assert upheld["by_noder"]["alice"]["outstanding_debt_micros"] >= 0
    conn.close()


# --------------------------------------------------------------------------- #
# The gateway: the door refuses on local infra; entries carry sources.         #
# --------------------------------------------------------------------------- #
def _host(tmp_path):
    app, conn, ident = _app(tmp_path)
    users = LocalUserStore(":memory:")
    accounts = LocalAccountService(users, ident.store, ident._signer)
    for name in ("alice", "bob", "shop"):
        accounts.create_user(name, f"{name}-password-1", tenant="t1")
    ledger = EarningsLedger(conn)
    press = PressDesk(
        ContributionStore(conn),
        stories=StoryStore(conn),
        preferences=PreferenceStore(conn),
        polls=PollDesk(PollStore(conn), PairwiseStore(conn), rng=random.Random(7)),
    )
    gateway = GatewayApp(
        app._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        accounts=accounts,
        settings_node=SettingsNode(SettingsStore(conn)),
        press=press,
        billing=BillingService(ledger),
        # Wired over the LOCAL durable connection on purpose: the door
        # must refuse, not silently skip (invariant 11 at the edge).
        ad_dividend=AdDividendService(
            ledger=ledger,
            book=DoubleEntryLedger(conn),
            durable=conn,
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
    return gateway, conn, ident, ledger


def test_the_settle_door_refuses_on_local_infra_after_a_real_impression(
    tmp_path,
):
    gateway, conn, ident, _ = _host(tmp_path)
    alice, bob, shop = (
        ident.token("alice"),
        ident.token("bob"),
        ident.token("shop"),
    )
    # A real story, a real campaign, a consented viewer, a real
    # impression — the full A4 flow up to the money line.
    assert (
        gateway.handle(
            _req(
                "POST",
                "/v1/press/contributions",
                token=alice,
                body={
                    "title": "The steel kettle, tested",
                    "body": "Four minutes to a litre; the handle stayed cool.",
                    "genres": ["products"],
                    "license": next(iter(LICENSES)),
                    "consent": True,
                },
            )
        ).status
        == 201
    )
    story_id = gateway.handle(
        _req("POST", "/v1/press/newsroom/run", token=alice)
    ).body["items"][0]["story_id"]
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
    gateway.handle(
        _req(
            "POST",
            "/v1/legal/consent",
            token=bob,
            body={"document": "privacy", "version": LEGAL_VERSIONS["privacy"]},
        )
    )
    placement = gateway.handle(
        _req(
            "GET",
            "/v1/press/ads",
            token=bob,
            query={"surface": "edition", "content": story_id},
        )
    ).body["placement"]
    assert placement is not None
    gateway.handle(
        _req(
            "POST",
            f"/v1/adhouse/placements/{placement['placement_id']}/impression",
            token=bob,
        )
    )
    # The money line: a local host answers with the honest refusal —
    # named, never a silent skip.
    refused = gateway.handle(_req("POST", "/v1/adhouse/settle", token=alice))
    assert refused.status == 403
    assert "production PostgreSQL" in refused.body["error"]["message"]
    conn.close()


def test_earnings_entries_carry_their_source_labels(tmp_path):
    gateway, conn, ident, ledger = _host(tmp_path)
    from oolu.billing.models import EarningsEntry

    ledger.append(
        EarningsEntry(
            noder_principal="alice",
            event_id=ad_event_id("pl-9"),
            amount_micros=4_900,
            kind=EarningsKind.ACCRUAL,
            available_at=NOW,
        )
    )
    ledger.append(
        EarningsEntry(
            noder_principal="alice",
            event_id="run-event-1",
            amount_micros=10_000,
            kind=EarningsKind.ACCRUAL,
            available_at=NOW,
        )
    )
    entries = gateway.handle(
        _req("GET", "/v1/earnings/entries", token=ident.token("alice"))
    )
    sources = {e["event_id"]: e["source"] for e in entries.body["items"]}
    assert sources[ad_event_id("pl-9")] == "ads"
    assert sources["run-event-1"] == "nodeplace"
    # And the standing balance maths reads both, one ledger — the rows
    # sit inside the holdback window, so they are pending, honestly.
    balance = gateway.handle(
        _req("GET", "/v1/earnings", token=ident.token("alice"))
    )
    assert (
        balance.body["available_micros"] + balance.body["pending_micros"]
        == 14_900
    )
    conn.close()
