"""M3: milestones — each tranche releases alone; a failure freezes the rest.

Exit gates (docs/marketplace-build-plan.md §5 M3): a milestone releases
only its own escrow tranche; a failed milestone freezes the remainder
where it sits — in escrow — and resolution refunds exactly the unreleased
part; the schedule is a digest-material term; a milestone offer cannot
place at all on a host without escrow.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from test_marketplace_escrow import _placed, _rig
from test_marketplace_spine import _offer

from oolu.billing.doubleentry import DoubleEntryLedger
from oolu.billing.psp import FakePsp
from oolu.durable import DurableConnection
from oolu.durable.audit import DurableAuditLog
from oolu.marketplace import MarketplaceSpine, OrderService, WrongState

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
M = 1_000_000

MILESTONES = (("deposit", 40 * M), ("build", 40 * M), ("final", 20 * M))


def _service_offer():
    # subtotal 100 + tax 8 = 108 total, scheduled in three tranches.
    return _offer(milestones=MILESTONES)


def test_each_tranche_releases_alone_and_the_last_carries_remainders(tmp_path):
    conn, audit, spine, orders, ledger, _ = _rig(tmp_path)
    order = _placed(spine, orders, offer=_service_offer())
    order_id = order.record.order_id
    assert order.record.escrow_held  # a schedule IS an escrow arrangement

    # First evidenced milestone captures the WHOLE total into escrow.
    orders.deliver_milestone(
        order_id, tenant="t1", actor="user-1", index=0, evidence="deposit-doc",
        now=NOW,
    )
    assert ledger.balance("marketplace_cash") == 108 * M
    assert ledger.balance("escrow_liability") == -108 * M

    # Accepting the deposit releases ITS tranche: 40 subtotal + its 3.2 tax
    # share — nothing of the siblings moves.
    orders.accept_milestone(order_id, tenant="t1", actor="user-1", index=0, now=NOW)
    assert ledger.balance("escrow_liability") == -(108 * M - 43_200_000)
    assert ledger.take_micros() == 2 * M  # 5% of the 40 tranche
    assert orders.orders.get(order_id, tenant="t1").state == "fulfilling"

    orders.deliver_milestone(
        order_id, tenant="t1", actor="user-1", index=1, evidence="build-doc",
        now=NOW,
    )
    orders.accept_milestone(order_id, tenant="t1", actor="user-1", index=1, now=NOW)
    orders.deliver_milestone(
        order_id, tenant="t1", actor="user-1", index=2, evidence="final-doc",
        now=NOW,
    )
    orders.accept_milestone(order_id, tenant="t1", actor="user-1", index=2, now=NOW)

    # The final tranche carried the tax remainder: escrow empties exactly.
    assert ledger.balance("escrow_liability") == 0
    assert ledger.take_micros() == 5 * M
    assert sum(ledger.trial_balance().values()) == 0
    done = orders.orders.get(order_id, tenant="t1")
    assert done.state == "completed"
    assert orders._invoices.for_order(order_id) is not None
    conn.close()


def test_a_failed_milestone_freezes_the_remainder_and_resolution_refunds_it(
    tmp_path,
):
    conn, audit, spine, orders, ledger, _ = _rig(tmp_path)
    order = _placed(spine, orders, offer=_service_offer())
    order_id = order.record.order_id
    orders.deliver_milestone(
        order_id, tenant="t1", actor="user-1", index=0, evidence="deposit-doc",
        now=NOW,
    )
    orders.accept_milestone(order_id, tenant="t1", actor="user-1", index=0, now=NOW)
    orders.deliver_milestone(
        order_id, tenant="t1", actor="user-1", index=1, evidence="build-doc",
        now=NOW,
    )
    orders.fail_milestone(
        order_id, tenant="t1", actor="user-1", index=1, now=NOW,
        reason="the build does not match the specification",
    )
    assert orders.orders.get(order_id, tenant="t1").state == "disputed"
    # Frozen: no sibling releases, no further deliveries.
    with pytest.raises(WrongState):
        orders.accept_milestone(
            order_id, tenant="t1", actor="user-1", index=1, now=NOW
        )
    with pytest.raises(WrongState):
        orders.deliver_milestone(
            order_id, tenant="t1", actor="user-1", index=2, evidence="x", now=NOW
        )
    frozen = ledger.balance("escrow_liability")
    assert frozen == -(108 * M - 43_200_000)

    # Resolution: exactly the unreleased remainder returns to the buyer;
    # the accepted deposit stands.
    resolved = orders.refund_unreleased(
        order_id, tenant="t1", actor="user-1", now=NOW, reason="build failed"
    )
    assert resolved.state == "resolved"
    assert ledger.balance("escrow_liability") == 0
    assert ledger.balance("marketplace_cash") == 43_200_000
    assert ledger.take_micros() == 2 * M  # the accepted tranche's fee stands
    assert sum(ledger.trial_balance().values()) == 0
    assert any(
        call[0] == "refund" and call[2] == 108 * M - 43_200_000
        for call in orders._psp.calls
    )
    conn.close()


def test_milestone_discipline_refuses_shortcuts(tmp_path):
    conn, audit, spine, orders, ledger, _ = _rig(tmp_path)
    order = _placed(spine, orders, offer=_service_offer())
    order_id = order.record.order_id
    # No acceptance before delivery; no delivery without evidence.
    with pytest.raises(WrongState):
        orders.accept_milestone(
            order_id, tenant="t1", actor="user-1", index=0, now=NOW
        )
    with pytest.raises(WrongState):
        orders.deliver_milestone(
            order_id, tenant="t1", actor="user-1", index=0, evidence="", now=NOW
        )
    conn.close()


def test_milestone_offers_require_an_escrow_policy(tmp_path):
    conn = DurableConnection(tmp_path / "durable.db")
    audit = DurableAuditLog(conn)
    spine = MarketplaceSpine(conn, audit=audit)
    orders = OrderService(
        conn,
        audit=audit,
        spine=spine,
        psp=FakePsp(),
        ledger=DoubleEntryLedger(conn),
        escrow=None,  # a host that never configured escrow
    )
    from test_marketplace_spine import _SAFE_RISK, _delegation

    spine.grant_delegation(_delegation())
    stored, _ = spine.create_purchase_intent(
        tenant="t1",
        principal="user-1",
        agent="oolu",
        offer=_service_offer(),
        idempotency_key="no-escrow",
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
    with pytest.raises(WrongState, match="escrow"):
        orders.place_order(
            intent_id=stored.intent.intent_id,
            tenant="t1",
            now=NOW,
            customer_ref="cus_1",
            payment_method_ref="pm_ok",
        )
    conn.close()
