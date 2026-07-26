"""M2: escrow, tax, and invoices — money waits for the world to confirm.

Exit gates (docs/marketplace-build-plan.md §5 M2): missing delivery
evidence keeps escrow unreleased and files an exception; verified evidence
captures into the escrow liability and acceptance (or the automatic
timeout) releases it with balanced postings; the spec's exceptions settle
directly; a refund reverses BOTH settlement transactions and the trial
balance returns to zero; every completed order carries a generated invoice
and a tax posting; an unconfigured jurisdiction refuses to transact at
the catalog door, before any intent exists.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from test_marketplace_spine import _SAFE_RISK, _delegation, _offer

from oolu.billing.doubleentry import DoubleEntryLedger
from oolu.billing.escrow import EscrowPolicy
from oolu.billing.psp import FakePsp
from oolu.billing.tax import InvoiceBook, JurisdictionModule, TaxRegistry
from oolu.durable import DurableConnection
from oolu.durable.audit import DurableAuditLog
from oolu.marketplace import (
    CatalogService,
    InventoryService,
    ListingUnavailable,
    MarketplaceSpine,
    OrderService,
    WrongState,
)

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
M = 1_000_000
US = JurisdictionModule(code="US", tax_rate_bps=800, invoice_prefix="US")


def _rig(tmp_path, *, escrow=None):
    conn = DurableConnection(tmp_path / "durable.db")
    audit = DurableAuditLog(conn)
    spine = MarketplaceSpine(conn, audit=audit)
    ledger = DoubleEntryLedger(conn)
    inventory = InventoryService(conn)
    orders = OrderService(
        conn,
        audit=audit,
        spine=spine,
        psp=FakePsp(),
        ledger=ledger,
        take_rate_bps=500,
        escrow=escrow or EscrowPolicy(),
        inventory=inventory,
        invoices=InvoiceBook(conn),
        jurisdiction=US,
    )
    spine.grant_delegation(_delegation())
    return conn, audit, spine, orders, ledger, inventory


def _placed(spine, orders, *, key="k1", offer=None, digital=False):
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
    order, _ = orders.place_order(
        intent_id=stored.intent.intent_id,
        tenant="t1",
        now=NOW,
        customer_ref="cus_1",
        payment_method_ref="pm_ok",
        digital=digital,
    )
    return order


def test_missing_evidence_holds_escrow_and_files_an_exception(tmp_path):
    conn, audit, spine, orders, ledger, _ = _rig(tmp_path)
    order = _placed(spine, orders)
    order_id = order.record.order_id
    orders.mark_shipped(order_id, tenant="t1", actor="user-1", now=NOW)
    bare = orders.mark_delivered(
        order_id, tenant="t1", actor="user-1", now=NOW, evidence=""
    )
    # Nothing captured, an exception filed, acceptance refused.
    assert bare.record.charge_ref is None
    assert ledger.transactions(order_id=order_id) == []
    assert any(
        r.event_type == "market.escrow.exception" for r in audit.records()
    )
    with pytest.raises(WrongState):
        orders.accept(order_id, tenant="t1", actor="user-1", now=NOW)
    # Late evidence heals the exception: capture into escrow, then release.
    orders.attach_evidence(
        order_id, tenant="t1", actor="user-1", now=NOW, evidence="signed-photo"
    )
    done = orders.accept(order_id, tenant="t1", actor="user-1", now=NOW)
    assert done.state == "completed"
    kinds = [t.kind for t in ledger.transactions(order_id=order_id)]
    assert kinds == ["capture", "release"]
    conn.close()


def test_escrow_holds_between_capture_and_release_with_balanced_postings(tmp_path):
    conn, audit, spine, orders, ledger, _ = _rig(tmp_path)
    order = _placed(spine, orders)
    order_id = order.record.order_id
    orders.mark_shipped(order_id, tenant="t1", actor="user-1", now=NOW)
    orders.mark_delivered(
        order_id, tenant="t1", actor="user-1", now=NOW, evidence="photo"
    )
    # Captured and HELD: cash in, all of it owed to escrow, nothing split.
    assert ledger.balance("marketplace_cash") == 108 * M
    assert ledger.balance("escrow_liability") == -108 * M
    assert ledger.balance("seller_payable") == 0
    assert ledger.take_micros() == 0
    orders.accept(order_id, tenant="t1", actor="user-1", now=NOW)
    # Released: escrow empty, the split posted (fee 5 on subtotal 100).
    assert ledger.balance("escrow_liability") == 0
    assert ledger.balance("seller_payable") == -95 * M
    assert ledger.take_micros() == 5 * M
    assert sum(ledger.trial_balance().values()) == 0
    conn.close()


def test_the_acceptance_timeout_releases_on_its_own(tmp_path):
    conn, audit, spine, orders, ledger, _ = _rig(tmp_path)
    order = _placed(spine, orders)
    order_id = order.record.order_id
    orders.mark_shipped(order_id, tenant="t1", actor="user-1", now=NOW)
    orders.mark_delivered(
        order_id, tenant="t1", actor="user-1", now=NOW, evidence="photo"
    )
    # Too early: the sweep does nothing.
    assert orders.sweep_acceptance_timeouts(now=NOW + timedelta(days=6)) == []
    released = orders.sweep_acceptance_timeouts(now=NOW + timedelta(days=8))
    assert released == [order_id]
    assert orders.orders.get(order_id, tenant="t1").state == "completed"
    assert ledger.balance("escrow_liability") == 0
    assert any(
        r.event_type == "market.order.auto_accepted" for r in audit.records()
    )
    # The sweep is idempotent — a second pass finds nothing.
    assert orders.sweep_acceptance_timeouts(now=NOW + timedelta(days=9)) == []
    conn.close()


def test_the_specs_exceptions_settle_directly(tmp_path):
    conn, audit, spine, orders, ledger, _ = _rig(
        tmp_path,
        escrow=EscrowPolicy(
            low_value_micros=200 * M, trusted_sellers=("acme",)
        ),
    )
    order = _placed(spine, orders)  # 108 total, trusted acme: direct
    assert not order.record.escrow_held
    order_id = order.record.order_id
    orders.mark_shipped(order_id, tenant="t1", actor="user-1", now=NOW)
    orders.mark_delivered(order_id, tenant="t1", actor="user-1", now=NOW)
    orders.accept(order_id, tenant="t1", actor="user-1", now=NOW)
    # One transaction, the M1 split — no escrow leg at all.
    kinds = [t.kind for t in ledger.transactions(order_id=order_id)]
    assert kinds == ["capture"]
    assert ledger.balance("escrow_liability") == 0
    conn.close()


def test_a_refund_reverses_both_settlement_transactions(tmp_path):
    conn, audit, spine, orders, ledger, _ = _rig(tmp_path)
    order = _placed(spine, orders)
    order_id = order.record.order_id
    orders.mark_shipped(order_id, tenant="t1", actor="user-1", now=NOW)
    orders.mark_delivered(
        order_id, tenant="t1", actor="user-1", now=NOW, evidence="photo"
    )
    orders.accept(order_id, tenant="t1", actor="user-1", now=NOW)
    orders.refund(order_id, tenant="t1", actor="user-1", now=NOW, reason="damaged")
    kinds = [t.kind for t in ledger.transactions(order_id=order_id)]
    assert kinds == ["capture", "release", "refund", "refund"]
    assert all(v == 0 for v in ledger.trial_balance().values())
    assert ledger.gmv_micros() == 0 and ledger.take_micros() == 0
    conn.close()


def test_every_completed_order_has_an_invoice_and_a_tax_posting(tmp_path):
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
        seller_id="acme",
        title="Steel bottle",
        unit_price_micros=100 * M,
        quantity_available=2,
        now=NOW,
    )
    catalog.publish(listing.listing_id, tenant="t1", seller="seller-1", now=NOW)
    offer = catalog.offer_for(listing.listing_id, quantity=1, now=NOW)
    assert offer.tax_estimate_micros == 8 * M  # 800 bps on 100
    order = _placed(spine, orders, key="taxed", offer=offer)
    order_id = order.record.order_id
    # The reservation held the unit at placement.
    assert inventory.available(listing.listing_id, now=NOW) == 1
    orders.mark_shipped(order_id, tenant="t1", actor="user-1", now=NOW)
    orders.mark_delivered(
        order_id, tenant="t1", actor="user-1", now=NOW, evidence="photo"
    )
    orders.accept(order_id, tenant="t1", actor="user-1", now=NOW)
    assert ledger.balance("tax_payable") == -8 * M
    invoice = orders._invoices.for_order(order_id)
    assert invoice is not None
    assert invoice.number == "US-000001"
    assert invoice.tax_micros == 8 * M
    assert invoice.total_micros == 108 * M
    # Completion committed the reservation: the unit is gone for good.
    assert inventory.available(
        listing.listing_id, now=NOW + timedelta(days=2)
    ) == 1
    conn.close()


def test_an_unconfigured_jurisdiction_refuses_to_transact(tmp_path):
    conn, audit, spine, orders, ledger, inventory = _rig(tmp_path)
    catalog = CatalogService(
        conn,
        audit=audit,
        seller_verified=lambda t, s: True,
        inventory=inventory,
        tax=TaxRegistry((US,)),
        jurisdiction="DE",  # nobody configured Germany
    )
    listing = catalog.create_draft(
        tenant="t1",
        seller_principal="seller-1",
        title="Steel bottle",
        unit_price_micros=100 * M,
        quantity_available=2,
        now=NOW,
    )
    catalog.publish(listing.listing_id, tenant="t1", seller="seller-1", now=NOW)
    with pytest.raises(ListingUnavailable, match="jurisdiction"):
        catalog.offer_for(listing.listing_id, quantity=1, now=NOW)
    conn.close()
