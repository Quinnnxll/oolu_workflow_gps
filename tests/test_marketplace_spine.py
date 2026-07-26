"""M0: the commercial spine — intents, digests, the ladder, delegation.

Exit gates (docs/marketplace-build-plan.md §5 M0), each pinned here: the
digest changes iff a material field changes; approval of digest A can never
execute digest B; expired approvals and approval reuse are refused; policy
evaluation is pure and every decision (including deny) lands on the audit
chain with reasons and policy version; revoking a delegation blocks every
unexecuted intent immediately; and no code path in the marketplace package
can move money — it does not even import the modules that know how.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from oolu.durable import DurableConnection
from oolu.durable.audit import DurableAuditLog
from oolu.marketplace import (
    AgentDelegation,
    ApprovalExpired,
    CommercialIntent,
    Decision,
    DelegationBlocked,
    DigestMismatch,
    DuplicateApprover,
    IntentAction,
    MarketplaceSpine,
    Offer,
    PurchaseFacts,
    PurchasePolicy,
    SaleFacts,
    SalesPolicy,
    StrongAuthenticationRequired,
    WrongState,
    evaluate_purchase,
    evaluate_sale,
    intent_digest,
)

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
M = 1_000_000


def _spine(tmp_path):
    conn = DurableConnection(tmp_path / "durable.db")
    audit = DurableAuditLog(conn)
    return MarketplaceSpine(conn, audit=audit), audit, conn


def _offer(**overrides) -> Offer:
    base = dict(
        offer_id="of-1",
        seller_id="acme",
        offer_version=1,
        item_id="steel-bottle",
        quantity=1,
        subtotal_micros=100 * M,
        currency="USD",
        tax_estimate_micros=8 * M,
        fees_micros=0,
        fulfillment_terms="ships in 2 days",
        refund_terms="30-day returns",
        refundable=True,
    )
    base.update(overrides)
    return Offer(**base)


def _delegation(**overrides) -> AgentDelegation:
    base = dict(
        delegation_id="d1",
        tenant_id="t1",
        principal_id="user-1",
        agent_id="oolu",
        allowed_actions=("purchase",),
        category_scope=("*",),
        maximum_single_micros=5_000 * M,
        daily_limit_micros=5_000 * M,
        monthly_limit_micros=50_000 * M,
        valid_from=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=30),
    )
    base.update(overrides)
    return AgentDelegation(**base)


_SAFE_RISK = dict(
    first_time_counterparty=False,
    new_delivery_destination=False,
    price_benchmark_micros=108 * M,
    seller_identity_verified=True,
)


def _create(spine, *, offer=None, key="k1", risk=None, **kwargs):
    return spine.create_purchase_intent(
        tenant="t1",
        principal="user-1",
        agent="oolu",
        offer=offer or _offer(),
        idempotency_key=key,
        now=NOW,
        category="household",
        delivery_destination="home",
        risk_facts=dict(_SAFE_RISK if risk is None else risk),
        **kwargs,
    )


def _intent(**overrides) -> CommercialIntent:
    base = dict(
        intent_id="i1",
        tenant_id="t1",
        principal_id="user-1",
        agent_id="oolu",
        action=IntentAction.PURCHASE,
        offer_snapshot=_offer(),
        category="household",
        delivery_destination="home",
        data_permissions=("shipping_address",),
        maximum_total_micros=200 * M,
        approval_policy_id="default-purchase",
        idempotency_key="k1",
        created_at=NOW,
        expires_at=NOW + timedelta(days=1),
    )
    base.update(overrides)
    return CommercialIntent(**base)


# --------------------------------------------------------------------------- #
# The digest: changes iff a material field changes.                            #
# --------------------------------------------------------------------------- #
def test_digest_changes_for_every_material_field_and_only_those():
    base = intent_digest(_intent(), policy_version="purchase-v1")

    material_offer = dict(
        offer_version=2,
        seller_id="other-seller",
        item_id="glass-bottle",
        quantity=2,
        subtotal_micros=101 * M,
        tax_estimate_micros=9 * M,
        fees_micros=1 * M,
        fulfillment_terms="ships in 5 days",
        refund_terms="no returns",
        recurring_terms="monthly",
        currency="EUR",
        # A payment schedule is a term: milestones ride the digest.
        milestones=(("deposit", 40 * M), ("balance", 60 * M)),
    )
    for field, value in material_offer.items():
        changed = _intent(offer_snapshot=_offer(**{field: value}))
        assert intent_digest(changed, policy_version="purchase-v1") != base, field

    material_intent = dict(
        principal_id="user-2",
        agent_id="other-agent",
        action=IntentAction.LISTING,
        data_permissions=("full_address", "phone"),
        expires_at=NOW + timedelta(days=2),
    )
    for field, value in material_intent.items():
        changed = _intent(**{field: value})
        assert intent_digest(changed, policy_version="purchase-v1") != base, field

    assert intent_digest(_intent(), policy_version="purchase-v2") != base

    # Non-material bookkeeping holds the digest steady.
    immaterial = dict(
        intent_id="i2",
        tenant_id="t2",
        category="gifts",
        delivery_destination="office",
        maximum_total_micros=999 * M,
        approval_policy_id="another-policy",
        idempotency_key="k2",
        created_at=NOW + timedelta(hours=1),
    )
    for field, value in immaterial.items():
        same = _intent(**{field: value})
        assert intent_digest(same, policy_version="purchase-v1") == base, field


def test_data_permission_order_is_not_material():
    a = _intent(data_permissions=("phone", "shipping_address"))
    b = _intent(data_permissions=("shipping_address", "phone"))
    assert intent_digest(a, policy_version="v") == intent_digest(
        b, policy_version="v"
    )


# --------------------------------------------------------------------------- #
# The ladder: pure, every decision reachable, reasons always named.            #
# --------------------------------------------------------------------------- #
def test_purchase_ladder_reaches_every_decision_deterministically():
    policy = PurchasePolicy(
        trusted_categories=("household",),
        trusted_counterparties=("acme",),
        preapproved_destinations=("home",),
        prohibited_categories=("weapons",),
        regulated_categories=("pharma",),
    )
    trusted = dict(
        category="household",
        counterparty="acme",
        delivery_destination="home",
        first_time_counterparty=False,
        new_delivery_destination=False,
        price_benchmark_micros=30 * M,
        seller_identity_verified=True,
    )
    cases = [
        (PurchaseFacts(amount_micros=30 * M, **trusted), Decision.AUTO_EXECUTE),
        (
            PurchaseFacts(
                amount_micros=30 * M,
                **{**trusted, "counterparty": "newco"},
            ),
            Decision.EXECUTE_AND_NOTIFY,
        ),
        (
            PurchaseFacts(amount_micros=80 * M, **trusted),
            Decision.REQUIRE_APPROVAL,
        ),
        (
            PurchaseFacts(
                amount_micros=30 * M, **{**trusted, "category": "pharma"}
            ),
            Decision.REQUIRE_STRONG_APPROVAL,
        ),
        (
            PurchaseFacts(
                amount_micros=30 * M,
                **{**trusted, "payout_destination_change": True},
            ),
            Decision.REQUIRE_MULTIPLE_APPROVERS,
        ),
        (
            PurchaseFacts(
                amount_micros=30 * M, **{**trusted, "sanctions_match": True}
            ),
            Decision.DENY,
        ),
    ]
    for facts, expected in cases:
        first = evaluate_purchase(policy, facts)
        second = evaluate_purchase(policy, facts)
        assert first == second  # purity: same inputs, same verdict
        assert first.decision is expected
        assert first.reasons  # every verdict names its reasons
        assert first.policy_version == policy.policy_version


def test_purchase_reasons_accumulate_within_a_rung():
    verdict = evaluate_purchase(
        PurchasePolicy(),
        PurchaseFacts(
            amount_micros=80 * M,
            first_time_counterparty=True,
            refundable=False,
            recurring=True,
            seller_identity_verified=True,
        ),
    )
    assert verdict.decision is Decision.REQUIRE_APPROVAL
    assert len(verdict.reasons) >= 4


def test_unverified_seller_is_denied_by_default():
    verdict = evaluate_purchase(
        PurchasePolicy(), PurchaseFacts(amount_micros=10 * M)
    )
    assert verdict.decision is Decision.DENY
    assert any("unverified" in reason for reason in verdict.reasons)


def test_sales_ladder_denies_below_absolute_floor_without_discretion():
    policy = SalesPolicy(
        price_floor_micros=80 * M,
        absolute_floor_micros=50 * M,
        auto_accept_price_micros=90 * M,
    )
    denied = evaluate_sale(
        policy,
        SaleFacts(
            final_price_micros=40 * M,
            inventory_available=True,
            fulfillment_capacity_available=True,
            buyer_risk_acceptable=True,
        ),
    )
    assert denied.decision is Decision.DENY
    accepted = evaluate_sale(
        policy,
        SaleFacts(
            final_price_micros=95 * M,
            inventory_available=True,
            fulfillment_capacity_available=True,
            buyer_risk_acceptable=True,
        ),
    )
    assert accepted.decision is Decision.AUTO_EXECUTE
    discounted = evaluate_sale(
        policy,
        SaleFacts(
            final_price_micros=95 * M,
            list_price_micros=200 * M,
            inventory_available=True,
            fulfillment_capacity_available=True,
            buyer_risk_acceptable=True,
        ),
    )
    assert discounted.decision is Decision.REQUIRE_APPROVAL


# --------------------------------------------------------------------------- #
# The spine: idempotent creation; every decision audited, including deny.      #
# --------------------------------------------------------------------------- #
def test_same_idempotency_key_returns_the_existing_intent(tmp_path):
    spine, audit, conn = _spine(tmp_path)
    spine.grant_delegation(_delegation())
    first, created = _create(spine, key="same-key")
    second, again = _create(spine, key="same-key")
    assert created and not again
    assert second.intent.intent_id == first.intent.intent_id
    created_events = [
        r for r in audit.records() if r.event_type == "market.intent.created"
    ]
    assert len(created_events) == 1
    conn.close()


def test_every_decision_lands_on_the_audit_chain_including_deny(tmp_path):
    spine, audit, conn = _spine(tmp_path)
    spine.grant_delegation(_delegation())
    stored, _ = _create(
        spine, risk={**_SAFE_RISK, "sanctions_match": True}, key="denied"
    )
    assert stored.state == "denied"
    decisions = [
        r.payload
        for r in audit.records()
        if r.event_type == "market.policy.decision"
    ]
    assert decisions and decisions[-1]["decision"] == "deny"
    assert decisions[-1]["reasons"]
    assert decisions[-1]["policy_version"]
    assert audit.verify()
    conn.close()


def test_an_agent_without_delegation_is_denied(tmp_path):
    spine, _, conn = _spine(tmp_path)
    stored, _ = _create(spine)  # no delegation granted
    assert stored.state == "denied"
    assert any("delegation" in r for r in stored.verdict.reasons)
    conn.close()


def test_offer_total_above_the_declared_maximum_is_denied(tmp_path):
    spine, _, conn = _spine(tmp_path)
    spine.grant_delegation(_delegation())
    stored, _ = _create(spine, maximum_total_micros=50 * M)
    assert stored.state == "denied"
    assert any("budget" in r for r in stored.verdict.reasons)
    conn.close()


# --------------------------------------------------------------------------- #
# Approval binds the digest; digest B can never execute under approval A.      #
# --------------------------------------------------------------------------- #
def test_approved_terms_execute_and_changed_terms_never_do(tmp_path):
    spine, audit, conn = _spine(tmp_path)
    spine.grant_delegation(_delegation())
    stored, _ = _create(spine)
    assert stored.state == "approval_pending"
    spine.record_approval(
        stored.intent.intent_id,
        tenant="t1",
        approver_id="user-1",
        assurance_level=1,
        approve=True,
        now=NOW,
    )

    # The seller bumps the price (a new offer version): digest B.
    repriced = _offer(offer_version=2, subtotal_micros=140 * M)
    with pytest.raises(DigestMismatch):
        spine.authorize_execution(
            stored.intent.intent_id,
            tenant="t1",
            current_offer=repriced,
            now=NOW + timedelta(minutes=5),
        )
    assert any(
        r.event_type == "market.digest.mismatch" for r in audit.records()
    )
    # Even an invisible term change — the same version with a new tax line —
    # is a different digest.
    with pytest.raises(DigestMismatch):
        spine.authorize_execution(
            stored.intent.intent_id,
            tenant="t1",
            current_offer=_offer(tax_estimate_micros=9 * M),
            now=NOW + timedelta(minutes=5),
        )
    # The intent survives to execute its EXACT approved terms.
    authorization = spine.authorize_execution(
        stored.intent.intent_id,
        tenant="t1",
        current_offer=_offer(),
        now=NOW + timedelta(minutes=10),
    )
    assert authorization.intent_digest == stored.digest
    assert len(authorization.approval_ids) == 1
    conn.close()


def test_an_authorization_cannot_be_minted_twice(tmp_path):
    spine, _, conn = _spine(tmp_path)
    spine.grant_delegation(_delegation())
    stored, _ = _create(spine)
    spine.record_approval(
        stored.intent.intent_id,
        tenant="t1",
        approver_id="user-1",
        assurance_level=1,
        approve=True,
        now=NOW,
    )
    spine.authorize_execution(
        stored.intent.intent_id, tenant="t1", current_offer=_offer(), now=NOW
    )
    with pytest.raises(WrongState):
        spine.authorize_execution(
            stored.intent.intent_id, tenant="t1", current_offer=_offer(), now=NOW
        )
    # The consumed approval itself can never be spent again.
    records = spine.approvals.for_intent(stored.intent.intent_id, tenant="t1")
    assert all(consumed for _, consumed in records)
    assert not spine.approvals.consume(
        records[0][0].approval_id, tenant="t1", now=NOW
    )
    conn.close()


def test_expired_approvals_and_expired_intents_are_refused(tmp_path):
    spine, _, conn = _spine(tmp_path)
    spine.grant_delegation(_delegation())
    # Intent outlives the approval TTL (24h) so the approval expires first.
    stored, _ = _create(spine, expires_in_minutes=72 * 60)
    spine.record_approval(
        stored.intent.intent_id,
        tenant="t1",
        approver_id="user-1",
        assurance_level=1,
        approve=True,
        now=NOW,
    )
    with pytest.raises(ApprovalExpired):
        spine.authorize_execution(
            stored.intent.intent_id,
            tenant="t1",
            current_offer=_offer(),
            now=NOW + timedelta(hours=25),
        )
    # An intent past its own window expires instead of authorizing.
    second, _ = _create(spine, key="short-lived", expires_in_minutes=30)
    from oolu.marketplace import IntentExpired

    with pytest.raises(IntentExpired):
        spine.record_approval(
            second.intent.intent_id,
            tenant="t1",
            approver_id="user-1",
            assurance_level=1,
            approve=True,
            now=NOW + timedelta(hours=1),
        )
    assert spine.get_intent(second.intent.intent_id, tenant="t1").state == "expired"
    conn.close()


def test_strong_verdicts_demand_step_up_and_dual_control_two_humans(tmp_path):
    spine, _, conn = _spine(tmp_path)
    spine.grant_delegation(_delegation())
    spine.set_purchase_policy(
        PurchasePolicy(dual_control_limit_micros=50 * M),
        tenant="t1",
        principal="user-1",
    )
    stored, _ = _create(spine)
    assert stored.verdict.decision is Decision.REQUIRE_MULTIPLE_APPROVERS
    with pytest.raises(StrongAuthenticationRequired):
        spine.record_approval(
            stored.intent.intent_id,
            tenant="t1",
            approver_id="user-1",
            assurance_level=1,
            approve=True,
            now=NOW,
        )
    spine.record_approval(
        stored.intent.intent_id,
        tenant="t1",
        approver_id="user-1",
        assurance_level=2,
        approve=True,
        now=NOW,
    )
    # One human cannot count twice.
    with pytest.raises(DuplicateApprover):
        spine.record_approval(
            stored.intent.intent_id,
            tenant="t1",
            approver_id="user-1",
            assurance_level=2,
            approve=True,
            now=NOW,
        )
    assert spine.get_intent(stored.intent.intent_id, tenant="t1").state == (
        "approval_pending"
    )
    spine.record_approval(
        stored.intent.intent_id,
        tenant="t1",
        approver_id="user-2",
        assurance_level=2,
        approve=True,
        now=NOW,
    )
    authorization = spine.authorize_execution(
        stored.intent.intent_id, tenant="t1", current_offer=_offer(), now=NOW
    )
    assert len(authorization.approval_ids) == 2
    conn.close()


def test_rejecting_never_needs_step_up(tmp_path):
    spine, _, conn = _spine(tmp_path)
    spine.grant_delegation(_delegation())
    stored, _ = _create(spine, offer=_offer(subtotal_micros=1_500 * M))
    assert stored.verdict.decision is Decision.REQUIRE_STRONG_APPROVAL
    record = spine.record_approval(
        stored.intent.intent_id,
        tenant="t1",
        approver_id="user-1",
        assurance_level=1,
        approve=False,
        now=NOW,
    )
    assert record.decision == "rejected"
    assert spine.get_intent(stored.intent.intent_id, tenant="t1").state == "rejected"
    conn.close()


# --------------------------------------------------------------------------- #
# Revocation blocks every unexecuted intent, immediately and live.             #
# --------------------------------------------------------------------------- #
def test_revoking_a_delegation_blocks_all_unexecuted_intents(tmp_path):
    spine, audit, conn = _spine(tmp_path)
    spine.grant_delegation(_delegation())
    waiting, _ = _create(spine, key="k-wait")
    approved, _ = _create(spine, key="k-approved")
    spine.record_approval(
        approved.intent.intent_id,
        tenant="t1",
        approver_id="user-1",
        assurance_level=1,
        approve=True,
        now=NOW,
    )
    blocked = spine.revoke_delegation(
        "d1", tenant="t1", principal="user-1", now=NOW
    )
    assert set(blocked) == {waiting.intent.intent_id, approved.intent.intent_id}
    for intent_id in blocked:
        assert spine.get_intent(intent_id, tenant="t1").state == "blocked"
    with pytest.raises(WrongState):
        spine.authorize_execution(
            approved.intent.intent_id, tenant="t1", current_offer=_offer(), now=NOW
        )
    assert any(
        r.event_type == "market.delegation.revoked" for r in audit.records()
    )
    conn.close()


def test_a_dead_delegation_blocks_at_the_moment_of_execution(tmp_path):
    spine, _, conn = _spine(tmp_path)
    spine.grant_delegation(_delegation(expires_at=NOW + timedelta(hours=1)))
    stored, _ = _create(spine)
    spine.record_approval(
        stored.intent.intent_id,
        tenant="t1",
        approver_id="user-1",
        assurance_level=1,
        approve=True,
        now=NOW,
    )
    # The delegation dies between approval and execution: still blocked.
    with pytest.raises(DelegationBlocked):
        spine.authorize_execution(
            stored.intent.intent_id,
            tenant="t1",
            current_offer=_offer(),
            now=NOW + timedelta(hours=2),
        )
    assert spine.get_intent(stored.intent.intent_id, tenant="t1").state == "blocked"
    conn.close()


# --------------------------------------------------------------------------- #
# Tenancy, the inbox, and the phase's defining absence: no money path.         #
# --------------------------------------------------------------------------- #
def test_cross_tenant_reads_find_nothing(tmp_path):
    spine, _, conn = _spine(tmp_path)
    spine.grant_delegation(_delegation())
    stored, _ = _create(spine)
    from oolu.marketplace import MarketNotFound

    with pytest.raises(MarketNotFound):
        spine.get_intent(stored.intent.intent_id, tenant="t2")
    assert spine.pending_approvals(tenant="t2", now=NOW) == []
    conn.close()


def test_the_inbox_shows_exact_terms_and_sweeps_expiry(tmp_path):
    spine, _, conn = _spine(tmp_path)
    spine.grant_delegation(_delegation())
    stored, _ = _create(spine)
    items = spine.pending_approvals(tenant="t1", now=NOW)
    assert len(items) == 1
    summary = items[0]
    assert summary["total"] == "108.00 USD"
    assert summary["intent_digest"] == stored.digest
    assert summary["risks"]
    # Past the intent's window the inbox is empty and the intent expired.
    assert spine.pending_approvals(tenant="t1", now=NOW + timedelta(days=2)) == []
    assert spine.get_intent(stored.intent.intent_id, tenant="t1").state == "expired"
    conn.close()


def test_the_commercial_spine_imports_no_money_path():
    """M0's standing gate: the SPINE — intents, digest, policy, delegation,
    approvals — imports no billing, provider, or payment machinery. Money
    enters only through the M1 order machine (`orders.py`), which consumes
    the spine's authorization; the law stays structurally separate from
    the market."""
    package = Path(__file__).resolve().parents[1] / "src" / "oolu" / "marketplace"
    spine = (
        "errors.py",
        "models.py",
        "digest.py",
        "policy.py",
        "delegation.py",
        "store.py",
        "review.py",
        "service.py",
    )
    forbidden = ("billing", "providers", "stripe", "psp", "payment")
    for name in spine:
        source = package / name
        for line_number, line in enumerate(
            source.read_text().splitlines(), start=1
        ):
            stripped = line.strip()
            if not (stripped.startswith("from ") or stripped.startswith("import ")):
                continue
            for word in forbidden:
                assert word not in stripped, (
                    f"{name}:{line_number} imports a money path: {stripped}"
                )
