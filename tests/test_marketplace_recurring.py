"""M3: the recurring rule — approved once, renewed exactly, changed never.

Exit gate (docs/marketplace-build-plan.md §5 M3): no recurring obligation
is created or materially changed without a fresh policy pass; renewals
inside approved terms proceed. And the standing laws still hold over
renewals: the digest binds placement, and a revoked delegation blocks a
renewal exactly like any other intent.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from test_marketplace_escrow import _rig
from test_marketplace_spine import _SAFE_RISK, _offer

from oolu.marketplace import (
    Decision,
    DelegationBlocked,
    RecurringBook,
    RequiresNewApproval,
    WrongState,
)

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
M = 1_000_000


def _subscription_offer(**overrides):
    return _offer(recurring_terms="monthly", refundable=True, **overrides)


def _founded(tmp_path):
    """A subscription through the FULL ladder: recurring forces approval,
    the human approves, placement authorizes — only then an obligation."""
    conn, audit, spine, orders, ledger, _ = _rig(tmp_path)
    book = RecurringBook(conn, audit=audit)
    stored, _ = spine.create_purchase_intent(
        tenant="t1",
        principal="user-1",
        agent="oolu",
        offer=_subscription_offer(),
        idempotency_key="sub-1",
        now=NOW,
        category="household",
        delivery_destination="home",
        risk_facts=dict(_SAFE_RISK),
    )
    assert stored.verdict.decision is Decision.REQUIRE_APPROVAL
    assert any("recurring" in reason for reason in stored.verdict.reasons)
    spine.record_approval(
        stored.intent.intent_id,
        tenant="t1",
        approver_id="user-1",
        assurance_level=1,
        approve=True,
        now=NOW,
    )
    orders.place_order(
        intent_id=stored.intent.intent_id,
        tenant="t1",
        now=NOW,
        customer_ref="cus_1",
        payment_method_ref="pm_ok",
    )
    obligation = book.create_from_intent(
        spine, stored.intent.intent_id, tenant="t1", period_days=30, now=NOW
    )
    return conn, spine, orders, book, obligation


def test_no_obligation_without_the_full_policy_pass(tmp_path):
    conn, audit, spine, orders, ledger, _ = _rig(tmp_path)
    book = RecurringBook(conn, audit=audit)
    # A one-time offer never founds an obligation.
    stored, _ = spine.create_purchase_intent(
        tenant="t1",
        principal="user-1",
        agent="oolu",
        offer=_offer(),
        idempotency_key="one-time",
        now=NOW,
        category="household",
        delivery_destination="home",
        risk_facts=dict(_SAFE_RISK),
    )
    with pytest.raises(WrongState, match="not recurring"):
        book.create_from_intent(
            spine, stored.intent.intent_id, tenant="t1", period_days=30, now=NOW
        )
    # A recurring intent that was only APPROVED — never authorized —
    # founds nothing either.
    pending, _ = spine.create_purchase_intent(
        tenant="t1",
        principal="user-1",
        agent="oolu",
        offer=_subscription_offer(),
        idempotency_key="approved-only",
        now=NOW,
        category="household",
        delivery_destination="home",
        risk_facts=dict(_SAFE_RISK),
    )
    spine.record_approval(
        pending.intent.intent_id,
        tenant="t1",
        approver_id="user-1",
        assurance_level=1,
        approve=True,
        now=NOW,
    )
    with pytest.raises(WrongState, match="authorized"):
        book.create_from_intent(
            spine, pending.intent.intent_id, tenant="t1", period_days=30, now=NOW
        )
    conn.close()


def test_a_renewal_inside_approved_terms_proceeds_without_a_human(tmp_path):
    conn, spine, orders, book, obligation = _founded(tmp_path)
    renewal = book.renew(
        spine,
        obligation.obligation_id,
        tenant="t1",
        current_offer=obligation.offer,
        now=NOW,
    )
    assert renewal.state == "auto_approved"
    assert renewal.verdict.decision is Decision.AUTO_EXECUTE
    assert any("renewal" in reason for reason in renewal.verdict.reasons)
    # The renewal places like any order — no approval step needed.
    order, created = orders.place_order(
        intent_id=renewal.intent.intent_id,
        tenant="t1",
        now=NOW,
        customer_ref="cus_1",
        payment_method_ref="pm_ok",
    )
    assert created and order.state == "confirmed"
    assert book.get(obligation.obligation_id, tenant="t1").renewals == 1
    conn.close()


def test_a_price_increase_refuses_and_reenters_policy(tmp_path):
    conn, spine, orders, book, obligation = _founded(tmp_path)
    increased = obligation.offer.model_copy(
        update={"subtotal_micros": 130 * M}
    )
    with pytest.raises(RequiresNewApproval, match="subtotal"):
        book.renew(
            spine,
            obligation.obligation_id,
            tenant="t1",
            current_offer=increased,
            now=NOW,
        )
    # The fresh policy pass: the increased subscription is a NEW intent
    # and the ladder demands approval again (recurring trigger).
    fresh, _ = spine.create_purchase_intent(
        tenant="t1",
        principal="user-1",
        agent="oolu",
        offer=increased,
        idempotency_key="sub-1-increased",
        now=NOW,
        category="household",
        delivery_destination="home",
        risk_facts=dict(_SAFE_RISK),
    )
    assert fresh.state == "approval_pending"
    conn.close()


def test_a_revoked_delegation_blocks_renewals_too(tmp_path):
    conn, spine, orders, book, obligation = _founded(tmp_path)
    renewal = book.renew(
        spine,
        obligation.obligation_id,
        tenant="t1",
        current_offer=obligation.offer,
        now=NOW,
    )
    spine.revoke_delegation("d1", tenant="t1", principal="user-1", now=NOW)
    with pytest.raises((DelegationBlocked, WrongState)):
        orders.place_order(
            intent_id=renewal.intent.intent_id,
            tenant="t1",
            now=NOW,
            customer_ref="cus_1",
            payment_method_ref="pm_ok",
        )
    conn.close()
