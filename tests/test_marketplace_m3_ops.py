"""M3: org controls, execution jobs, and the reconciliation desk.

Exit gates (docs/marketplace-build-plan.md §5 M3): a payout-destination
change takes the multi-approver path — self-approval refused, step-up
required, one approver never enough — and applies only after the delay
window; a physical job's changed terms invalidate the prior approval by
the same digest law as purchases; reconciliation closes matched orders and
files unmatched ones as exceptions, and a duplicate charge surfaces as a
dispute with the evidence trail attached.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from test_marketplace_escrow import _placed, _rig

from oolu.billing.doubleentry import LedgerEntry, LedgerTransaction
from oolu.marketplace import (
    DelayNotElapsed,
    DigestMismatch,
    DuplicateApprover,
    JobDesk,
    PayoutChangeDesk,
    ReconciliationDesk,
    SelfApproval,
    StrongAuthenticationRequired,
    WrongState,
)

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
M = 1_000_000


def _complete(orders, order_id):
    orders.mark_shipped(order_id, tenant="t1", actor="user-1", now=NOW)
    orders.mark_delivered(
        order_id, tenant="t1", actor="user-1", now=NOW, evidence="photo"
    )
    orders.accept(order_id, tenant="t1", actor="user-1", now=NOW)


# --------------------------------------------------------------------------- #
# Payout changes: four eyes, strong hands, and a slow clock.                   #
# --------------------------------------------------------------------------- #
def test_payout_changes_need_two_strangers_with_strong_hands_and_patience(
    tmp_path,
):
    conn, audit, spine, orders, ledger, _ = _rig(tmp_path)
    desk = PayoutChangeDesk(conn, audit=audit, delay_hours=48)
    request = desk.request(
        tenant="t1",
        principal="seller-1",
        current_destination="acct_old",
        new_destination="acct_new",
        now=NOW,
    )
    # The requester can never be one of their own four eyes.
    with pytest.raises(SelfApproval):
        desk.approve(
            request.request_id,
            tenant="t1",
            approver="seller-1",
            assurance_level=2,
            now=NOW,
        )
    # A normal session cannot approve at all.
    with pytest.raises(StrongAuthenticationRequired):
        desk.approve(
            request.request_id,
            tenant="t1",
            approver="cfo",
            assurance_level=1,
            now=NOW,
        )
    first = desk.approve(
        request.request_id,
        tenant="t1",
        approver="cfo",
        assurance_level=2,
        now=NOW,
    )
    assert first.state == "pending"  # one approver is never enough
    with pytest.raises(WrongState):
        desk.apply(request.request_id, tenant="t1", now=NOW)
    with pytest.raises(DuplicateApprover):
        desk.approve(
            request.request_id,
            tenant="t1",
            approver="cfo",
            assurance_level=2,
            now=NOW,
        )
    second = desk.approve(
        request.request_id,
        tenant="t1",
        approver="controller",
        assurance_level=2,
        now=NOW,
    )
    assert second.state == "approved"
    # Approved is not applied: the protection window still stands.
    with pytest.raises(DelayNotElapsed):
        desk.apply(request.request_id, tenant="t1", now=NOW + timedelta(hours=47))
    applied = desk.apply(
        request.request_id, tenant="t1", now=NOW + timedelta(hours=49)
    )
    assert applied.state == "applied"
    assert applied.new_destination == "acct_new"
    conn.close()


def test_the_real_owner_can_kill_a_hostile_change_inside_the_window(tmp_path):
    conn, audit, spine, orders, ledger, _ = _rig(tmp_path)
    desk = PayoutChangeDesk(conn, audit=audit)
    request = desk.request(
        tenant="t1",
        principal="seller-1",
        current_destination="acct_old",
        new_destination="acct_attacker",
        now=NOW,
    )
    desk.approve(
        request.request_id, tenant="t1", approver="a", assurance_level=2, now=NOW
    )
    desk.approve(
        request.request_id, tenant="t1", approver="b", assurance_level=2, now=NOW
    )
    killed = desk.reject(
        request.request_id, tenant="t1", actor="seller-1", now=NOW
    )
    assert killed.state == "rejected"
    with pytest.raises(WrongState):
        desk.apply(
            request.request_id, tenant="t1", now=NOW + timedelta(days=30)
        )
    conn.close()


# --------------------------------------------------------------------------- #
# Execution jobs: the node's terms match the approved order, or nothing runs.  #
# --------------------------------------------------------------------------- #
def test_a_jobs_changed_terms_invalidate_the_prior_approval(tmp_path):
    conn, audit, spine, orders, ledger, _ = _rig(tmp_path)
    dispatched: list = []
    desk = JobDesk(conn, audit=audit, dispatcher=dispatched.append)
    order = _placed(spine, orders)
    job = desk.dispatch(
        order, node_id="drone-7", parameters={"site": "yard"}, now=NOW
    )
    assert dispatched and dispatched[0].job_id == job.job_id
    assert job.price_micros == order.record.offer.total_micros
    # The node answers with a HIGHER price: not a negotiation — an
    # invalidation, audited, refused deterministically.
    with pytest.raises(DigestMismatch):
        desk.acknowledge(
            job.job_id,
            tenant="t1",
            price_micros=job.price_micros + 10 * M,
            scheduled_at=NOW + timedelta(days=1),
            now=NOW,
        )
    assert desk.get(job.job_id, tenant="t1").state == "terms_changed"
    assert any(
        r.event_type == "market.job.terms_changed" for r in audit.records()
    )
    # The exact approved terms run: acknowledge, execute, evidence home.
    second = desk.dispatch(
        order, node_id="drone-7", parameters={"site": "yard"}, now=NOW
    )
    desk.acknowledge(
        second.job_id,
        tenant="t1",
        price_micros=second.price_micros,
        scheduled_at=NOW + timedelta(days=1),
        now=NOW,
    )
    done = desk.complete(
        second.job_id, tenant="t1", evidence="sha256:site-photos", now=NOW
    )
    assert done.state == "completed"
    conn.close()


# --------------------------------------------------------------------------- #
# Reconciliation: matched closes, mismatched files, duplicates dispute.        #
# --------------------------------------------------------------------------- #
def test_reconciliation_closes_matched_and_disputes_duplicate_charges(tmp_path):
    conn, audit, spine, orders, ledger, _ = _rig(tmp_path)
    desk = ReconciliationDesk(
        conn,
        audit=audit,
        orders=orders.orders,
        ledger=ledger,
        invoices=orders._invoices,
    )
    clean = _placed(spine, orders, key="clean")
    _complete(orders, clean.record.order_id)
    crooked = _placed(spine, orders, key="crooked")
    _complete(orders, crooked.record.order_id)
    # A second capture sneaks onto the crooked order's book — somebody's
    # money, twice.
    ledger.post(
        LedgerTransaction(
            txn_id=uuid4().hex,
            idempotency_key=f"rogue:{uuid4().hex}",
            kind="capture",
            order_id=crooked.record.order_id,
            entries=(
                LedgerEntry(account="marketplace_cash", amount_micros=108 * M),
                LedgerEntry(
                    account="escrow_liability", amount_micros=-108 * M
                ),
            ),
            created_at=NOW,
        )
    )
    result = desk.sweep(now=NOW)
    assert clean.record.order_id in result["closed"]
    assert crooked.record.order_id in result["exceptions"]
    assert crooked.record.order_id in result["disputes"]
    assert (
        orders.orders.get(crooked.record.order_id, tenant="t1").state
        == "disputed"
    )
    filed = desk.exceptions()
    assert len(filed) == 1
    assert any("duplicate charge" in issue for issue in filed[0].issues)
    # The dispute carries its evidence trail: ledger transactions and refs.
    assert any(item.startswith("ledger:") for item in filed[0].evidence)
    disputed_events = [
        r.payload
        for r in audit.records()
        if r.event_type == "market.order.disputed"
    ]
    assert disputed_events and disputed_events[-1]["evidence"]
    # A second sweep re-examines nothing.
    assert desk.sweep(now=NOW) == {
        "closed": [],
        "exceptions": [],
        "disputes": [],
    }
    conn.close()


def test_a_refunded_order_reconciles_to_a_zero_net(tmp_path):
    conn, audit, spine, orders, ledger, _ = _rig(tmp_path)
    desk = ReconciliationDesk(
        conn,
        audit=audit,
        orders=orders.orders,
        ledger=ledger,
        invoices=orders._invoices,
    )
    order = _placed(spine, orders, key="refunded")
    _complete(orders, order.record.order_id)
    orders.refund(
        order.record.order_id, tenant="t1", actor="user-1", now=NOW,
        reason="damaged",
    )
    result = desk.sweep(now=NOW)
    assert order.record.order_id in result["closed"]
    conn.close()
