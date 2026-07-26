"""M1: the order machine — authorize on confirm, capture on accept, refund
as an exact reversal.

Exit gates (docs/marketplace-build-plan.md §5 M1): a duplicate placement
returns the existing order; capture posts the take-rate fee split ONCE even
when a provider webhook replays; a refund posts the capture's exact
negation and the trial balance returns to zero; a declined card cancels
instead of half-placing; a live provider on local-only infra is refused
before anything moves; and the catalog's version bump kills stale
approvals at placement by the digest law itself.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from test_marketplace_spine import _SAFE_RISK, _delegation, _offer

from oolu.billing.doubleentry import DoubleEntryLedger
from oolu.billing.guard import MoneyModeError
from oolu.billing.payout import PaymentError
from oolu.billing.psp import FakePsp
from oolu.durable import DurableConnection
from oolu.durable.audit import DurableAuditLog
from oolu.marketplace import (
    CatalogService,
    DigestMismatch,
    MarketplaceSpine,
    OrderService,
    SellerUnverified,
    WrongState,
)

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
M = 1_000_000


def _rig(tmp_path, *, psp=None, take_rate_bps=500):
    conn = DurableConnection(tmp_path / "durable.db")
    audit = DurableAuditLog(conn)
    spine = MarketplaceSpine(conn, audit=audit)
    ledger = DoubleEntryLedger(conn)
    psp = psp or FakePsp()
    orders = OrderService(
        conn,
        audit=audit,
        spine=spine,
        psp=psp,
        ledger=ledger,
        take_rate_bps=take_rate_bps,
    )
    spine.grant_delegation(_delegation())
    return conn, spine, orders, ledger, psp


def _approved_intent(spine, *, key="k1", offer=None):
    stored, _ = spine.create_purchase_intent(
        tenant="t1",
        principal="user-1",
        agent="oolu",
        offer=offer or _offer(),
        idempotency_key=key,
        now=NOW,
        category="household",
        delivery_destination="home",
        risk_facts=dict(_SAFE_RISK),
    )
    spine.record_approval(
        stored.intent.intent_id,
        tenant="t1",
        approver_id="user-1",
        assurance_level=1,
        approve=True,
        now=NOW,
    )
    return stored


def _place(orders, intent_id):
    return orders.place_order(
        intent_id=intent_id,
        tenant="t1",
        now=NOW,
        customer_ref="cus_1",
        payment_method_ref="pm_ok",
    )


# --------------------------------------------------------------------------- #
# The lifecycle: authorize at confirm, capture only at accept.                 #
# --------------------------------------------------------------------------- #
def test_the_full_lifecycle_captures_once_with_the_take_rate_split(tmp_path):
    conn, spine, orders, ledger, psp = _rig(tmp_path)
    stored = _approved_intent(spine)
    order, created = _place(orders, stored.intent.intent_id)
    assert created and order.state == "confirmed"
    assert order.record.auth_ref is not None
    # Authorization is not capture: nothing on the book yet.
    assert ledger.transactions() == []
    assert not any(call[0] == "capture" for call in psp.calls)

    order_id = order.record.order_id
    orders.mark_shipped(order_id, tenant="t1", actor="user-1", now=NOW, tracking="TRK1")
    orders.mark_delivered(order_id, tenant="t1", actor="user-1", now=NOW, evidence="photo")
    done = orders.accept(order_id, tenant="t1", actor="user-1", now=NOW)
    assert done.state == "completed"
    assert done.record.charge_ref is not None

    # One capture, split G=108 into seller 95, fee 5, tax 8 (500 bps).
    txns = ledger.transactions(order_id=order_id)
    assert len(txns) == 1 and txns[0].kind == "capture"
    assert ledger.balance("marketplace_cash") == 108 * M
    assert ledger.balance("seller_payable") == -95 * M
    assert ledger.take_micros() == 5 * M
    assert ledger.gmv_micros() == 108 * M
    assert sum(ledger.trial_balance().values()) == 0
    conn.close()


def test_a_duplicate_placement_returns_the_existing_order(tmp_path):
    conn, spine, orders, ledger, psp = _rig(tmp_path)
    stored = _approved_intent(spine)
    first, created = _place(orders, stored.intent.intent_id)
    second, again = _place(orders, stored.intent.intent_id)
    assert created and not again
    assert second.record.order_id == first.record.order_id
    assert sum(1 for call in psp.calls if call[0] == "authorize") == 1
    conn.close()


def test_a_duplicate_capture_webhook_changes_nothing(tmp_path):
    conn, spine, orders, ledger, psp = _rig(tmp_path)
    stored = _approved_intent(spine)
    order, _ = _place(orders, stored.intent.intent_id)
    order_id = order.record.order_id
    orders.mark_shipped(order_id, tenant="t1", actor="user-1", now=NOW)
    orders.mark_delivered(order_id, tenant="t1", actor="user-1", now=NOW)
    orders.accept(order_id, tenant="t1", actor="user-1", now=NOW)
    event = {"type": "payment.captured", "tenant": "t1", "order_id": order_id}
    first = orders.process_psp_event(event, now=NOW)
    second = orders.process_psp_event(event, now=NOW)
    assert not first["applied"] and not second["applied"]
    assert len(ledger.transactions(order_id=order_id)) == 1
    assert orders.orders.get(order_id, tenant="t1").state == "completed"
    conn.close()


def test_a_refund_reverses_the_capture_exactly(tmp_path):
    conn, spine, orders, ledger, psp = _rig(tmp_path)
    stored = _approved_intent(spine)
    order, _ = _place(orders, stored.intent.intent_id)
    order_id = order.record.order_id
    orders.mark_shipped(order_id, tenant="t1", actor="user-1", now=NOW)
    orders.mark_delivered(order_id, tenant="t1", actor="user-1", now=NOW)
    orders.accept(order_id, tenant="t1", actor="user-1", now=NOW)
    refunded = orders.refund(
        order_id, tenant="t1", actor="user-1", now=NOW, reason="damaged"
    )
    assert refunded.state == "refunded"
    assert refunded.record.refund_ref is not None
    txns = ledger.transactions(order_id=order_id)
    assert [txn.kind for txn in txns] == ["capture", "refund"]
    assert txns[1].reverses_txn_id == txns[0].txn_id
    assert all(balance == 0 for balance in ledger.trial_balance().values())
    assert ledger.gmv_micros() == 0 and ledger.take_micros() == 0
    # A second refund attempt finds nothing to refund.
    with pytest.raises(WrongState):
        orders.refund(order_id, tenant="t1", actor="user-1", now=NOW)
    conn.close()


def test_a_declined_card_cancels_the_order(tmp_path):
    conn, spine, orders, ledger, psp = _rig(
        tmp_path, psp=FakePsp(decline_pm_refs=frozenset({"pm_bad"}))
    )
    stored = _approved_intent(spine)
    with pytest.raises(PaymentError):
        orders.place_order(
            intent_id=stored.intent.intent_id,
            tenant="t1",
            now=NOW,
            customer_ref="cus_1",
            payment_method_ref="pm_bad",
        )
    order = orders.orders.by_intent(stored.intent.intent_id, tenant="t1")
    assert order is not None and order.state == "cancelled"
    assert ledger.transactions() == []
    conn.close()


def test_cancel_before_capture_voids_the_authorization(tmp_path):
    conn, spine, orders, ledger, psp = _rig(tmp_path)
    stored = _approved_intent(spine)
    order, _ = _place(orders, stored.intent.intent_id)
    cancelled = orders.cancel(
        order.record.order_id, tenant="t1", actor="user-1", now=NOW
    )
    assert cancelled.state == "cancelled"
    assert any(call[0] == "void" for call in psp.calls)
    assert ledger.transactions() == []
    # Too late to ship a cancelled order.
    with pytest.raises(WrongState):
        orders.mark_shipped(
            order.record.order_id, tenant="t1", actor="user-1", now=NOW
        )
    conn.close()


# --------------------------------------------------------------------------- #
# Money mode: a live provider demands the production substrate.                #
# --------------------------------------------------------------------------- #
class _LivePsp(FakePsp):
    @property
    def mode(self) -> str:
        return "live"


def test_a_live_provider_is_refused_on_local_infra(tmp_path):
    conn, spine, orders, ledger, psp = _rig(tmp_path, psp=_LivePsp())
    stored = _approved_intent(spine)
    with pytest.raises(MoneyModeError):
        _place(orders, stored.intent.intent_id)
    assert orders.orders.by_intent(stored.intent.intent_id, tenant="t1") is None
    assert ledger.transactions() == []
    conn.close()


# --------------------------------------------------------------------------- #
# The catalog: verified sellers, versioned terms, the digest law end to end.   #
# --------------------------------------------------------------------------- #
def test_only_verified_sellers_publish_and_offers_track_availability(tmp_path):
    conn = DurableConnection(tmp_path / "durable.db")
    audit = DurableAuditLog(conn)
    verified = {("t1", "seller-1")}
    catalog = CatalogService(
        conn, audit=audit, seller_verified=lambda t, s: (t, s) in verified
    )
    listing = catalog.create_draft(
        tenant="t1",
        seller_principal="seller-1",
        title="Steel bottle",
        unit_price_micros=100 * M,
        quantity_available=2,
        now=NOW,
    )
    stranger = catalog.create_draft(
        tenant="t1",
        seller_principal="seller-2",
        title="Mystery goods",
        unit_price_micros=5 * M,
        now=NOW,
    )
    with pytest.raises(SellerUnverified):
        catalog.publish(
            stranger.listing_id, tenant="t1", seller="seller-2", now=NOW
        )
    active = catalog.publish(
        listing.listing_id, tenant="t1", seller="seller-1", now=NOW
    )
    assert active.status == "active"
    offer = catalog.offer_for(listing.listing_id, quantity=2, now=NOW)
    assert offer.subtotal_micros == 200 * M
    assert offer.offer_version == 1
    from oolu.marketplace import ListingUnavailable

    with pytest.raises(ListingUnavailable):
        catalog.offer_for(listing.listing_id, quantity=3, now=NOW)
    conn.close()


def test_a_price_change_kills_stale_approvals_at_placement(tmp_path):
    """The shelf-to-order digest law: approve at version 1, the seller
    reprices to version 2, and placement against the live offer refuses —
    the spec's 'seller changes price after approval' test, end to end."""
    conn, spine, orders, ledger, psp = _rig(tmp_path)
    audit = DurableAuditLog(conn)
    catalog = CatalogService(conn, audit=audit, seller_verified=lambda t, s: True)
    listing = catalog.create_draft(
        tenant="t1",
        seller_principal="seller-1",
        seller_id="acme",
        title="Steel bottle",
        unit_price_micros=100 * M,
        quantity_available=5,
        now=NOW,
    )
    catalog.publish(listing.listing_id, tenant="t1", seller="seller-1", now=NOW)
    offer_v1 = catalog.offer_for(listing.listing_id, quantity=1, now=NOW)
    stored, _ = spine.create_purchase_intent(
        tenant="t1",
        principal="user-1",
        agent="oolu",
        offer=offer_v1,
        idempotency_key="from-listing",
        now=NOW,
        category="household",
        delivery_destination="home",
        risk_facts=dict(_SAFE_RISK),
    )
    spine.record_approval(
        stored.intent.intent_id,
        tenant="t1",
        approver_id="user-1",
        assurance_level=1,
        approve=True,
        now=NOW,
    )
    catalog.update_terms(
        listing.listing_id,
        tenant="t1",
        seller="seller-1",
        now=NOW,
        unit_price_micros=140 * M,
    )
    offer_v2 = catalog.offer_for(listing.listing_id, quantity=1, now=NOW)
    assert offer_v2.offer_version == 2
    with pytest.raises(DigestMismatch):
        orders.place_order(
            intent_id=stored.intent.intent_id,
            tenant="t1",
            now=NOW,
            customer_ref="cus_1",
            payment_method_ref="pm_ok",
            current_offer=offer_v2,
        )
    # The exact approved terms still place.
    order, created = orders.place_order(
        intent_id=stored.intent.intent_id,
        tenant="t1",
        now=NOW,
        customer_ref="cus_1",
        payment_method_ref="pm_ok",
        current_offer=offer_v1,
    )
    assert created and order.state == "confirmed"
    conn.close()
