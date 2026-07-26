"""M4: the open market — other markets' offers, our law, our ledger.

Exit gates (docs/marketplace-build-plan.md §5 M4): an external offer with
a broken or unsigned wire contract never reaches the intent door; a
cross-market purchase is still a typed intent with a digest — changed
terms kill the approval across the federation boundary too; the platform's
share posts on cross-market settlements exactly like local ones; the
compliance deployment gate crosses the boundary (a peer jurisdiction with
no configured module refuses import); suspension blocks a peer
immediately; the sourcing sweep is one normalized, eligibility-marked
comparison across every shelf; and a financing partner's product is a
recurring offer that cannot skip the ladder.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from test_marketplace_escrow import _rig
from test_marketplace_spine import _SAFE_RISK, _offer

from oolu.billing.tax import JurisdictionModule, TaxRegistry
from oolu.marketplace import (
    CatalogService,
    Decision,
    DigestMismatch,
    FederationDesk,
    ListingUnavailable,
    MarketNotFound,
    ProtocolViolation,
    RfqSpecification,
    SimpleInterestFinancing,
    WrongState,
    financing_offer,
    sign_offer,
    verify_offer,
)

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
M = 1_000_000
US = JurisdictionModule(code="US", tax_rate_bps=0)
DE = JurisdictionModule(code="DE", tax_rate_bps=1900)
SECRET = "the-partner-markets-shared-secret"


def _desk(conn, audit, *, jurisdictions=(US, DE), secrets=None):
    return FederationDesk(
        conn,
        audit=audit,
        tax=TaxRegistry(jurisdictions),
        secrets=secrets if secrets is not None else {"partner-market": SECRET},
    )


def _signed(**overrides):
    return sign_offer(
        _offer(**overrides), peer_id="partner-market", secret=SECRET
    )


# --------------------------------------------------------------------------- #
# The wire contract.                                                           #
# --------------------------------------------------------------------------- #
def test_the_signature_binds_every_material_term():
    offer = _signed()
    assert verify_offer(offer, peer_id="partner-market", secret=SECRET)
    repriced = offer.model_copy(update={"subtotal_micros": 90 * M})
    assert not verify_offer(repriced, peer_id="partner-market", secret=SECRET)
    assert not verify_offer(offer, peer_id="someone-else", secret=SECRET)
    assert not verify_offer(offer, peer_id="partner-market", secret="wrong")
    assert not verify_offer(_offer(), peer_id="partner-market", secret=SECRET)


def test_tampered_or_unsigned_offers_never_reach_the_shelf(tmp_path):
    conn, audit, spine, orders, ledger, _ = _rig(tmp_path)
    desk = _desk(conn, audit)
    desk.register_peer(
        peer_id="partner-market", name="Partner", jurisdiction="DE", now=NOW
    )
    with pytest.raises(ProtocolViolation):
        desk.import_offer("partner-market", _offer(), now=NOW)  # unsigned
    tampered = _signed().model_copy(update={"subtotal_micros": 90 * M})
    with pytest.raises(ProtocolViolation):
        desk.import_offer("partner-market", tampered, now=NOW)
    assert desk.imported() == []
    assert any(
        r.event_type == "market.federation.offer_rejected"
        for r in audit.records()
    )
    conn.close()


def test_peers_must_be_known_configured_and_active(tmp_path):
    conn, audit, spine, orders, ledger, _ = _rig(tmp_path)
    desk = _desk(conn, audit, jurisdictions=(US,))  # no DE module
    with pytest.raises(MarketNotFound):
        desk.import_offer("partner-market", _signed(), now=NOW)
    desk.register_peer(
        peer_id="partner-market", name="Partner", jurisdiction="DE", now=NOW
    )
    # The compliance deployment gate crosses the federation boundary.
    with pytest.raises(ListingUnavailable, match="jurisdiction"):
        desk.import_offer("partner-market", _signed(), now=NOW)
    # A peer with no shared secret can announce nothing this host trusts.
    bare = _desk(conn, audit, secrets={})
    bare.register_peer(
        peer_id="partner-market", name="Partner", jurisdiction="US", now=NOW
    )
    with pytest.raises(ProtocolViolation, match="secret"):
        bare.import_offer("partner-market", _signed(), now=NOW)
    # Suspension blocks new imports immediately.
    ready = _desk(conn, audit)
    ready.register_peer(
        peer_id="partner-market", name="Partner", jurisdiction="DE", now=NOW
    )
    ready.set_peer_state("partner-market", state="suspended")
    with pytest.raises(WrongState, match="suspended"):
        ready.import_offer("partner-market", _signed(), now=NOW)
    conn.close()


# --------------------------------------------------------------------------- #
# A cross-market purchase is still a typed intent with a digest.               #
# --------------------------------------------------------------------------- #
def test_a_cross_market_purchase_walks_the_spine_and_pays_the_take(tmp_path):
    conn, audit, spine, orders, ledger, _ = _rig(tmp_path)
    desk = _desk(conn, audit)
    desk.register_peer(
        peer_id="partner-market", name="Partner", jurisdiction="DE", now=NOW
    )
    imported = desk.import_offer(
        "partner-market", _signed(), seller_attested=True, now=NOW
    )
    stored, _ = spine.create_purchase_intent(
        tenant="t1",
        principal="user-1",
        agent="oolu",
        offer=imported.offer,
        idempotency_key="x-market-1",
        now=NOW,
        category="household",
        delivery_destination="home",
        risk_facts={
            **_SAFE_RISK,
            "seller_identity_verified": imported.seller_attested,
        },
    )
    spine.record_approval(
        stored.intent.intent_id,
        tenant="t1",
        approver_id="user-1",
        assurance_level=1,
        approve=True,
        now=NOW,
    )
    # The peer re-signs at a higher price: digest law, across the border.
    repriced = sign_offer(
        _offer(offer_version=2, subtotal_micros=140 * M),
        peer_id="partner-market",
        secret=SECRET,
    )
    with pytest.raises(DigestMismatch):
        orders.place_order(
            intent_id=stored.intent.intent_id,
            tenant="t1",
            now=NOW,
            customer_ref="cus_1",
            payment_method_ref="pm_ok",
            current_offer=repriced,
        )
    # The exact approved terms settle — on OUR ledger, with OUR take.
    order, _ = orders.place_order(
        intent_id=stored.intent.intent_id,
        tenant="t1",
        now=NOW,
        customer_ref="cus_1",
        payment_method_ref="pm_ok",
        current_offer=imported.offer,
    )
    order_id = order.record.order_id
    orders.mark_shipped(order_id, tenant="t1", actor="user-1", now=NOW)
    orders.mark_delivered(
        order_id, tenant="t1", actor="user-1", now=NOW, evidence="customs-doc"
    )
    orders.accept(order_id, tenant="t1", actor="user-1", now=NOW)
    assert ledger.take_micros() == 5 * M  # 500 bps on the 100 subtotal
    assert sum(ledger.trial_balance().values()) == 0
    conn.close()


# --------------------------------------------------------------------------- #
# The sourcing sweep: every shelf, one honest comparison.                      #
# --------------------------------------------------------------------------- #
def test_sourcing_compares_local_and_federated_shelves_with_gaps_named(
    tmp_path,
):
    conn, audit, spine, orders, ledger, inventory = _rig(tmp_path)
    catalog = CatalogService(
        conn,
        audit=audit,
        seller_verified=lambda t, s: True,
        inventory=inventory,
        tax=TaxRegistry((US,)),
        jurisdiction="US",
    )
    listing = catalog.create_draft(
        tenant="t1",
        seller_principal="seller-1",
        seller_id="local-acme",
        title="Steel bottle",
        category="household",
        unit_price_micros=110 * M,
        quantity_available=5,
        now=NOW,
    )
    catalog.publish(listing.listing_id, tenant="t1", seller="seller-1", now=NOW)

    desk = _desk(conn, audit)
    desk.register_peer(
        peer_id="partner-market", name="Partner", jurisdiction="DE", now=NOW
    )
    desk.import_offer(
        "partner-market",
        _signed(offer_id="of-cheap", subtotal_micros=90 * M, tax_estimate_micros=0),
        attributes={"material": "stainless-steel"},
        now=NOW,
    )
    desk.import_offer(
        "partner-market",
        _signed(
            offer_id="of-plastic",
            seller_id="planet-plastics",
            subtotal_micros=50 * M,
            tax_estimate_micros=0,
        ),
        attributes={"material": "plastic"},
        now=NOW,
    )
    rows = desk.source(
        RfqSpecification(
            category="household",
            required_attributes=(("material", "stainless-steel"),),
            quantity=1,
        ),
        catalog=catalog,
        now=NOW,
    )
    # Exactly one row is eligible: the attested steel offer leads, and no
    # ineligible row outranks it on price.
    assert rows[0].origin == "peer:partner-market"
    assert rows[0].eligible
    assert [row.eligible for row in rows[1:]] == [False, False]
    # The local shelf attests no attributes: it is judged by the same bar.
    local = next(r for r in rows if r.origin.startswith("local:"))
    assert any("material" in gap for gap in local.gaps)
    substitute = next(r for r in rows if r.offer.offer_id == "of-plastic")
    assert not substitute.eligible
    assert any("material" in gap for gap in substitute.gaps)
    conn.close()


# --------------------------------------------------------------------------- #
# Partners: a financing plan is a recurring offer — the ladder stands.         #
# --------------------------------------------------------------------------- #
def test_financing_enters_as_a_recurring_offer_and_cannot_skip_the_ladder(
    tmp_path,
):
    partner = SimpleInterestFinancing(partner_id="lend-co", rate_bps=1200)
    quote = partner.quote(
        principal_micros=1_200 * M, installments=12, period_days=30
    )
    assert quote.total_repay_micros >= 1_344 * M  # 12% flat, ceil rounding
    offer = financing_offer(quote)
    assert offer.recurring_terms
    assert not offer.refundable

    conn, audit, spine, orders, ledger, _ = _rig(tmp_path)
    stored, _ = spine.create_purchase_intent(
        tenant="t1",
        principal="user-1",
        agent="oolu",
        offer=offer,
        idempotency_key="fin-1",
        now=NOW,
        category="financing",
        delivery_destination="home",
        risk_facts={**_SAFE_RISK, "seller_identity_verified": True},
    )
    assert stored.state == "approval_pending"
    assert stored.verdict.decision is Decision.REQUIRE_APPROVAL
    assert any("recurring" in r for r in stored.verdict.reasons)
    assert any("non-refundable" in r for r in stored.verdict.reasons)
    conn.close()
