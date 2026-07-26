"""The explorer desk (A6): compare on verified evidence, then the best buy.

Exit gate (agents-expansion plan, phase A6): only verified-buyer
feedback enters scores — a review without a finished order is refused
and self-reviews are refused; same candidates + evidence + mode → same
ranking with every brief exposing its breakdown; nothing sponsored sits
anywhere near the ranking inputs (the A4 import scan covers explorer/
the moment it exists); a best-buy purchase walks the digest-bound
approval path unchanged; lab evidence is a live, author-attached
results contribution — byline and provenance are structural; and a
discount renders only where a real list-price fact exists.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest
from test_http_gateway import NOW as GATEWAY_NOW
from test_http_gateway import _app, _req

from oolu.durable.connection import DurableConnection
from oolu.explorer import (
    ExplorerError,
    ReviewDesk,
    ReviewStore,
    best_buy,
    build_rows,
    discount_fact,
    feedback_of,
    lab_of,
    trust_from_book,
)
from oolu.gateway import GatewayApp
from oolu.identity import LocalAccountService, LocalUserStore
from oolu.marketplace.catalog import Listing
from oolu.marketplace.models import Offer
from oolu.marketplace.orders import OrderRecord, StoredOrder
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
from oolu.social import AssistantHistoryStore

NOW = datetime(2026, 7, 17, 9, 0, tzinfo=UTC)
LICENSE = next(iter(LICENSES))


def _listing(listing_id="l1", *, seller="shop", price=20_000_000,
             list_price=None, status="active", quantity=5, category="kettles"):
    return Listing(
        listing_id=listing_id,
        tenant_id="t1",
        seller_principal=seller,
        seller_id=seller,
        title=f"Listing {listing_id}",
        category=category,
        unit_price_micros=price,
        list_price_micros=list_price,
        quantity_available=quantity,
        status=status,
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )


def _order(order_id="o1", *, buyer="carol", seller="shop", item="l1",
           state="accepted"):
    record = OrderRecord(
        order_id=order_id,
        tenant_id="t1",
        buyer_principal=buyer,
        seller_id=seller,
        seller_principal=seller,
        intent_id="i1",
        authorization_id="a1",
        intent_digest="d1",
        offer=Offer.model_construct(item_id=item),
        idempotency_key=f"k-{order_id}",
        take_rate_bps=500,
        created_at=NOW,
    )
    return StoredOrder(record, state=state)


# --------------------------------------------------------------------------- #
# The discount is a fact or it is nothing.                                     #
# --------------------------------------------------------------------------- #
def test_discounts_derive_only_from_a_real_list_price():
    assert discount_fact(20_000_000, None) is None  # no stated price: nothing
    assert discount_fact(20_000_000, 20_000_000) is None  # equal: nothing
    assert discount_fact(20_000_000, 15_000_000) is None  # "was less": nothing
    assert discount_fact(20_000_000, 25_000_000) == 20  # a real markdown


# --------------------------------------------------------------------------- #
# The verified-buyer gate.                                                     #
# --------------------------------------------------------------------------- #
def _desk(tmp_path):
    conn = DurableConnection(tmp_path / "explorer.db")
    return conn, ReviewDesk(ReviewStore(conn, clock=lambda: NOW))


def test_a_review_needs_a_finished_order_by_its_own_buyer(tmp_path):
    conn, desk = _desk(tmp_path)
    listing = _listing()
    with pytest.raises(ExplorerError, match="needs a finished order"):
        desk.review(tenant="t1", reviewer="carol", listing=listing,
                    order=None, rating=5)
    with pytest.raises(ExplorerError, match="follow acceptance"):
        desk.review(tenant="t1", reviewer="carol", listing=listing,
                    order=_order(state="confirmed"), rating=5)
    with pytest.raises(ExplorerError, match="not your order"):
        desk.review(tenant="t1", reviewer="mallory", listing=listing,
                    order=_order(buyer="carol"), rating=5)
    with pytest.raises(ExplorerError, match="something else"):
        desk.review(tenant="t1", reviewer="carol", listing=listing,
                    order=_order(item="other-listing"), rating=5)
    conn.close()


def test_self_reviews_are_refused_and_one_voice_per_product(tmp_path):
    conn, desk = _desk(tmp_path)
    listing = _listing(seller="shop")
    # The seller bought their own product: still no voice (self-dealing).
    with pytest.raises(ExplorerError, match="no self-reviews"):
        desk.review(tenant="t1", reviewer="shop", listing=listing,
                    order=_order(buyer="shop"), rating=5)
    review = desk.review(tenant="t1", reviewer="carol", listing=listing,
                         order=_order(), rating=4, words="Boils fast.")
    assert review.rating == 4 and review.reviewer == "carol"
    with pytest.raises(ExplorerError, match="already reviewed"):
        desk.review(tenant="t1", reviewer="carol", listing=listing,
                    order=_order(order_id="o2"), rating=2)
    conn.close()


def test_evidence_factors_are_honest_about_absence():
    assert feedback_of([])["factor"] == 0.5  # no voices: neutral, never 0
    assert lab_of([])["factor"] == 0.5
    fresh = trust_from_book(finished=0, refunded=0, disputed=0)
    assert fresh["score"] == 0.5 and fresh["basis"] == "no order history"
    seasoned = trust_from_book(finished=20, refunded=1, disputed=0)
    troubled = trust_from_book(finished=20, refunded=1, disputed=5)
    assert seasoned["score"] > troubled["score"]  # trouble weighs double


# --------------------------------------------------------------------------- #
# The brief: deterministic, mode-weighted, breakdown-first.                    #
# --------------------------------------------------------------------------- #
def _rows(**evidence):
    listings = [
        _listing("cheap", price=10_000_000),
        _listing("premium", price=30_000_000, list_price=40_000_000),
        _listing("sleeping", status="draft"),
    ]
    neutral = {"count": 0, "mean": None, "factor": 0.5}
    lab_neutral = {"count": 0, "mean_score": None, "factor": 0.5}
    trust_neutral = trust_from_book(finished=0, refunded=0, disputed=0)
    return build_rows(
        listings,
        feedback_for=lambda lid: evidence.get("feedback", {}).get(lid, neutral),
        trust_for=lambda s: trust_neutral,
        lab_for=lambda lid: evidence.get("lab", {}).get(lid, lab_neutral),
    )


def test_the_matrix_keeps_ineligible_rows_with_named_gaps():
    rows = _rows()
    sleeping = next(r for r in rows if r.listing_id == "sleeping")
    assert sleeping.eligible is False
    assert any("not on sale" in gap for gap in sleeping.gaps)
    premium = next(r for r in rows if r.listing_id == "premium")
    assert premium.discount_percent == 25  # the fact renders
    cheap = next(r for r in rows if r.listing_id == "cheap")
    assert cheap.discount_percent is None  # no stated price: nothing


def test_the_brief_is_deterministic_and_modes_change_the_crown():
    lab = {"premium": {"count": 2, "mean_score": 95.0, "factor": 0.95}}
    rows = _rows(lab=lab)
    value = best_buy(rows, mode="value")
    assert value == best_buy(rows, mode="value")  # same inputs, same brief
    assert value.winner_listing_id == "cheap"
    measured = best_buy(rows, mode="measured")
    assert measured.winner_listing_id == "premium"
    # Every ranked row carries its whole breakdown and the stance.
    for item in measured.ranked:
        assert set(item["factors"]) == {
            "price", "discount", "feedback", "trust", "lab",
        }
        assert item["weights"] == best_buy(rows, mode="measured").ranked[0]["weights"]
    # The sleeping listing is never crowned.
    assert all(
        item["listing_id"] != "sleeping" for item in measured.ranked
    )
    with pytest.raises(ValueError, match="unknown mode"):
        best_buy(rows, mode="vibes")


# --------------------------------------------------------------------------- #
# The gateway: evidence in, matrix out, the pulse brief, the buy handoff.      #
# --------------------------------------------------------------------------- #
def _host(tmp_path):
    app, conn, ident = _app(tmp_path)
    users = LocalUserStore(":memory:")
    accounts = LocalAccountService(users, ident.store, ident._signer)
    for name in ("alice", "carol", "shop"):
        accounts.create_user(name, f"{name}-password-1", tenant="t1")
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
        assistant_history=AssistantHistoryStore(conn),
        press=press,
    )
    gateway._commerce_seller_kyc.apply(
        tenant="t1", principal="shop", legal_name="Shop Ltd",
        company_email="sales@shop-ltd.example",
    )
    gateway._commerce_seller_kyc.decide(
        tenant="t1", principal="shop", reviewer="rev", approved=True
    )
    return gateway, conn, ident


def _publish_listing(gateway, ident, *, title, price, list_price=None,
                     category="kettles", quantity=5):
    shop = ident.token("shop")
    body = {
        "title": title,
        "unit_price_micros": price,
        "category": category,
        "quantity_available": quantity,
    }
    if list_price is not None:
        body["list_price_micros"] = list_price
    draft = gateway.handle(
        _req("POST", "/v1/commerce/listings", token=shop, body=body)
    )
    assert draft.status == 201, draft.body
    listing_id = draft.body["listing_id"]
    published = gateway.handle(
        _req(
            "POST",
            f"/v1/commerce/listings/{listing_id}/publish",
            token=shop,
        )
    )
    assert published.status == 200, published.body
    return listing_id


def test_the_explorer_flow_end_to_end(tmp_path):
    gateway, conn, ident = _host(tmp_path)
    alice, carol = ident.token("alice"), ident.token("carol")
    steel = _publish_listing(
        gateway, ident, title="Steel kettle", price=20_000_000,
        list_price=25_000_000,
    )
    glass = _publish_listing(
        gateway, ident, title="Glass kettle", price=30_000_000
    )

    # The matrix: the discount fact renders only where it IS a fact.
    compared = gateway.handle(
        _req(
            "GET",
            "/v1/explorer/compare",
            token=carol,
            query={"category": "kettles", "mode": "value"},
        )
    )
    assert compared.status == 200
    rows = {r["listing_id"]: r for r in compared.body["rows"]}
    assert rows[steel]["discount_percent"] == 20
    assert rows[glass]["discount_percent"] is None
    assert compared.body["brief"]["winner_listing_id"] == steel
    assert compared.body["brief"]["ranked"][0]["factors"]  # the reasons

    # A review without a finished order is refused at the door.
    refused = gateway.handle(
        _req(
            "POST",
            "/v1/explorer/reviews",
            token=carol,
            body={"listing_id": steel, "order_id": "nope", "rating": 5},
        )
    )
    assert refused.status == 404
    assert "finished order" in refused.body["error"]["message"]

    # Lab evidence: alice publishes measured results and attaches them.
    contribution = gateway.handle(
        _req(
            "POST",
            "/v1/press/contributions",
            token=alice,
            body={
                "title": "Kettle bench test",
                "body": "Litre-boil times measured across five runs each.",
                "genres": ["results"],
                "license": LICENSE,
                "consent": True,
            },
        )
    )
    assert contribution.status == 201
    attached = gateway.handle(
        _req(
            "POST",
            "/v1/explorer/lab",
            token=alice,
            body={
                "listing_id": glass,
                "contribution_id": contribution.body["contribution_id"],
                "score": 92,
                "metrics": {"litre_boil_seconds": 360},
            },
        )
    )
    assert attached.status == 201
    assert attached.body["author"] == "alice"  # the byline's principal
    # Only the author may attach their results.
    stolen = gateway.handle(
        _req(
            "POST",
            "/v1/explorer/lab",
            token=carol,
            body={
                "listing_id": steel,
                "contribution_id": contribution.body["contribution_id"],
                "score": 10,
            },
        )
    )
    assert stolen.status == 403

    # The evidence reaches the matrix, and the measured mode feels it.
    measured = gateway.handle(
        _req(
            "GET",
            "/v1/explorer/compare",
            token=carol,
            query={"category": "kettles", "mode": "measured"},
        )
    )
    rows = {r["listing_id"]: r for r in measured.body["rows"]}
    assert rows[glass]["lab"]["count"] == 1
    assert measured.body["brief"]["winner_listing_id"] == glass

    # The buy handoff: the brief's winner walks the standing spine —
    # offer minted from the listing, intent through the policy ladder.
    offer = gateway.handle(
        _req(
            "POST",
            f"/v1/commerce/listings/{steel}/offer",
            token=carol,
            body={"quantity": 1},
        )
    )
    assert offer.status == 200
    intent = gateway.handle(
        _req(
            "POST",
            "/v1/commerce/intents",
            token=carol,
            body={
                "offer": offer.body,
                "idempotency_key": "explorer-buy-1",
            },
        )
    )
    assert intent.status in (200, 201), intent.body
    assert intent.body.get("verdict") or intent.body.get("decision")
    conn.close()


def test_a_followed_interest_delivers_the_brief_on_the_pulse(tmp_path):
    gateway, conn, ident = _host(tmp_path)
    carol = ident.token("carol")
    _publish_listing(
        gateway, ident, title="Steel kettle", price=20_000_000,
        list_price=25_000_000,
    )
    followed = gateway.handle(
        _req(
            "POST",
            "/v1/explorer/interests",
            token=carol,
            body={"category": "kettles", "mode": "value", "at_minute": 9 * 60},
            now=GATEWAY_NOW,
        )
    )
    assert followed.status == 200
    assert followed.body["interest"]["goal"] == "explorer.brief:kettles:value"

    morning = (GATEWAY_NOW + timedelta(days=1)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    gateway._pulse_gate = 0.0
    gateway.handle(
        _req("GET", "/v1/explorer/compare", token=carol, now=morning,
             query={"category": "kettles"})
    )
    history = gateway._assistant_history.history(
        tenant="t1", principal="carol", agent="explorer"
    )
    assert history, "the brief should land in the Explorer thread"
    assert "Steel kettle" in history[-1]["body"]

    # Laying the interest down removes the schedule.
    dropped = gateway.handle(
        _req(
            "POST",
            "/v1/explorer/interests",
            token=carol,
            body={"category": "kettles", "mode": "value", "enabled": False},
        )
    )
    assert dropped.body == {"interest": None}
    conn.close()


def test_the_explorer_package_stays_inside_the_walls():
    from pathlib import Path

    package = Path(__file__).resolve().parents[1] / "src" / "oolu" / "explorer"
    forbidden = (
        "adhouse",  # advertising can never buy a ranking (invariant 5)
        "http",
        "urllib",
        "requests",
        "socket",
        "providers",
    )
    for source in sorted(package.glob("*.py")):
        for number, line in enumerate(source.read_text().splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith(("from ", "import ")):
                for word in forbidden:
                    assert word not in stripped, (
                        f"{source.name}:{number}: {stripped}"
                    )
