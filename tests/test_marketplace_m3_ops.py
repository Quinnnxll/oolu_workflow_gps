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


def test_the_worker_lease_dispatcher_hands_jobs_to_capable_workers_only(
    tmp_path,
):
    """The M3 closer: a job rides the worker control plane's signed,
    single-use lease — assigned only to a worker holding the node's
    capability, verifiable by that worker alone; no capable worker means
    a LOUD failure, never a silent promise."""
    from oolu.marketplace import WorkerLeaseDispatcher
    from oolu.worker.control_plane import ControlPlane, WorkerInfo
    from oolu.worker.leases import LeaseSigner, LeaseVerifier
    from oolu.worker.ledger import InMemoryLeaseLedger

    conn, audit, spine, orders, ledger, _ = _rig(tmp_path)
    secret = "a-thirty-two-character-plus-lease-secret"
    lease_ledger = InMemoryLeaseLedger()
    control = ControlPlane(LeaseSigner(secret), ledger=lease_ledger)
    control.register_worker(
        WorkerInfo(worker_id="w1", capabilities=frozenset({"execute:drone-7"}))
    )
    desk = JobDesk(
        conn, audit=audit, dispatcher=WorkerLeaseDispatcher(control, audit=audit)
    )
    order = _placed(spine, orders)
    job = desk.dispatch(order, node_id="drone-7", now=NOW)
    assignment = control.poll("w1")
    assert assignment is not None
    assert assignment.payload["job_id"] == job.job_id
    lease = LeaseVerifier(secret, audience="w1", ledger=lease_ledger).verify(
        assignment.lease_token
    )
    assert lease.tenant_id == "t1"
    leased = [
        r.payload for r in audit.records() if r.event_type == "market.job.leased"
    ]
    assert leased and leased[-1]["worker_id"] == "w1"
    # The credential itself never rides the chain.
    assert assignment.lease_token not in str(leased)

    # No worker holds the crane capability: the dispatch fails loudly.
    with pytest.raises(WrongState, match="crane-1"):
        desk.dispatch(order, node_id="crane-1", now=NOW)
    failed = [
        job_row
        for job_row in [desk.get(j, tenant="t1") for j in _job_ids(conn)]
        if job_row.state == "failed"
    ]
    assert failed
    assert any(
        r.event_type == "market.job.dispatch_failed" for r in audit.records()
    )
    conn.close()


def _job_ids(conn) -> list[str]:
    with conn.lock:
        rows = conn.db.execute("SELECT job_id FROM market_jobs").fetchall()
    return [row["job_id"] for row in rows]


# --------------------------------------------------------------------------- #
# Adjudication: the marketplace decides, the book obeys.                       #
# --------------------------------------------------------------------------- #
def test_partial_awards_come_from_the_sellers_side_and_balance(tmp_path):
    conn, audit, spine, orders, ledger, _ = _rig(tmp_path)
    order = _placed(spine, orders)
    order_id = order.record.order_id
    _complete(orders, order_id)
    orders.open_dispute(
        order_id, tenant="t1", actor="user-1", now=NOW, reason="scratched"
    )
    resolved = orders.adjudicate(
        order_id,
        tenant="t1",
        adjudicator="operator-1",
        outcome="partial_refund",
        amount_micros=30 * M,
        now=NOW,
        note="cosmetic damage",
    )
    assert resolved.state == "resolved"
    assert ledger.balance("marketplace_cash") == 78 * M
    assert ledger.balance("seller_payable") == -65 * M  # the seller bears it
    assert ledger.take_micros() == 5 * M  # the platform's fee stands
    assert sum(ledger.trial_balance().values()) == 0
    # An award beyond what was ever captured is refused.
    other = _placed(spine, orders, key="over-award")
    _complete(orders, other.record.order_id)
    orders.open_dispute(
        other.record.order_id, tenant="t1", actor="user-1", now=NOW,
        reason="broken",
    )
    with pytest.raises(WrongState, match="award"):
        orders.adjudicate(
            other.record.order_id,
            tenant="t1",
            adjudicator="operator-1",
            outcome="partial_refund",
            amount_micros=200 * M,
            now=NOW,
        )
    conn.close()


def test_full_refund_and_reject_close_a_dispute_with_balanced_books(tmp_path):
    conn, audit, spine, orders, ledger, _ = _rig(tmp_path)
    refunded_order = _placed(spine, orders, key="buyer-wins")
    _complete(orders, refunded_order.record.order_id)
    orders.open_dispute(
        refunded_order.record.order_id, tenant="t1", actor="user-1", now=NOW,
        reason="never worked",
    )
    verdict = orders.adjudicate(
        refunded_order.record.order_id,
        tenant="t1",
        adjudicator="operator-1",
        outcome="full_refund",
        now=NOW,
    )
    assert verdict.state == "refunded"
    assert all(v == 0 for v in ledger.trial_balance().values())
    assert ledger.gmv_micros() == 0 and ledger.take_micros() == 0

    # Reject with frozen escrow: the remainder releases to the seller.
    from test_marketplace_milestones import _service_offer

    frozen = _placed(spine, orders, key="seller-wins", offer=_service_offer())
    frozen_id = frozen.record.order_id
    orders.deliver_milestone(
        frozen_id, tenant="t1", actor="user-1", index=0, evidence="doc", now=NOW
    )
    orders.accept_milestone(
        frozen_id, tenant="t1", actor="user-1", index=0, now=NOW
    )
    orders.fail_milestone(
        frozen_id, tenant="t1", actor="user-1", index=1, now=NOW,
        reason="alleged defect",
    )
    upheld = orders.adjudicate(
        frozen_id,
        tenant="t1",
        adjudicator="operator-1",
        outcome="reject",
        now=NOW,
        note="the work met the specification",
    )
    assert upheld.state == "resolved"
    assert ledger.balance("escrow_liability") == 0
    # The seller ends whole: the full 95 share plus tax and fee settled.
    assert ledger.balance("seller_payable") == -95 * M
    assert ledger.take_micros() == 5 * M
    assert sum(ledger.trial_balance().values()) == 0
    conn.close()


def test_replacement_sends_the_order_back_to_fulfillment(tmp_path):
    conn, audit, spine, orders, ledger, _ = _rig(tmp_path)
    order = _placed(spine, orders)
    order_id = order.record.order_id
    _complete(orders, order_id)
    orders.open_dispute(
        order_id, tenant="t1", actor="user-1", now=NOW, reason="wrong color"
    )
    replaced = orders.adjudicate(
        order_id,
        tenant="t1",
        adjudicator="operator-1",
        outcome="replacement",
        now=NOW,
        note="seller re-ships",
    )
    assert replaced.state == "fulfilling"
    # The money stands untouched while the replacement ships.
    assert ledger.balance("marketplace_cash") == 108 * M
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
