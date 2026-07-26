"""M2: trust — reputation's one lever, and risk facts derived from the book.

Exit gates (docs/marketplace-build-plan.md §5 M2): reputation never relaxes
an explicit spending/category/legal constraint (property test over the
trigger matrix — its ONLY effect is the auto-execution trust bar); an
elevated derived fraud score forces the strong path and the deny bar
refuses deterministically; spend totals, counterparty familiarity, and
risk signals derive from the durable order book instead of arriving on
faith — and a caller's explicit fact always wins over a derivation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from test_marketplace_spine import _SAFE_RISK, _delegation, _offer

from oolu.durable import DurableConnection
from oolu.durable.audit import DurableAuditLog
from oolu.marketplace import (
    Decision,
    MarketplaceSpine,
    OrderHistory,
    OrderRecord,
    OrderStore,
    PurchaseFacts,
    PurchasePolicy,
    evaluate_purchase,
)

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
M = 1_000_000


# --------------------------------------------------------------------------- #
# Reputation: one lever, never an override.                                    #
# --------------------------------------------------------------------------- #
def test_reputation_never_relaxes_an_explicit_constraint():
    policy = PurchasePolicy(
        prohibited_categories=("weapons",),
        regulated_categories=("pharma",),
        dual_control_limit_micros=500 * M,
    )
    base = dict(
        category="household",
        counterparty="acme",
        delivery_destination="home",
        first_time_counterparty=False,
        new_delivery_destination=False,
        price_benchmark_micros=30 * M,
        seller_identity_verified=True,
    )
    explicit_triggers: list[dict] = [
        {"amount_micros": 80 * M},  # above the auto limit
        {"amount_micros": 30 * M, "refundable": False},
        {"amount_micros": 30 * M, "recurring": True},
        {"amount_micros": 30 * M, "sensitive_data_required": True},
        {"amount_micros": 30 * M, "category": "pharma"},
        {"amount_micros": 30 * M, "category": "weapons"},
        {"amount_micros": 30 * M, "sanctions_match": True},
        {"amount_micros": 600 * M},  # dual control
        {"amount_micros": 30 * M, "irreversible_delivery": True},
        {"amount_micros": 30 * M, "daily_total_micros": 190 * M},
        {"amount_micros": 30 * M, "fraud_score": 0.9},
    ]
    for trigger in explicit_triggers:
        plain = evaluate_purchase(policy, PurchaseFacts(**{**base, **trigger}))
        sainted = evaluate_purchase(
            policy,
            PurchaseFacts(**{**base, **trigger, "counterparty_reputation": 1.0}),
        )
        # A perfect reputation changes NOTHING an explicit constraint set.
        assert sainted == plain, trigger


def test_reputations_only_effect_is_the_auto_trust_bar():
    policy = PurchasePolicy(
        trusted_categories=("household",),
        preapproved_destinations=("home",),
    )
    facts = dict(
        amount_micros=30 * M,
        category="household",
        counterparty="newcomer",
        delivery_destination="home",
        first_time_counterparty=False,
        new_delivery_destination=False,
        price_benchmark_micros=30 * M,
        seller_identity_verified=True,
    )
    unknown = evaluate_purchase(policy, PurchaseFacts(**facts))
    assert unknown.decision is Decision.EXECUTE_AND_NOTIFY
    earned = evaluate_purchase(
        policy, PurchaseFacts(**{**facts, "counterparty_reputation": 0.95})
    )
    assert earned.decision is Decision.AUTO_EXECUTE
    middling = evaluate_purchase(
        policy, PurchaseFacts(**{**facts, "counterparty_reputation": 0.5})
    )
    assert middling.decision is Decision.EXECUTE_AND_NOTIFY


# --------------------------------------------------------------------------- #
# Derivations from the order book.                                             #
# --------------------------------------------------------------------------- #
def _order(
    store: OrderStore,
    *,
    state: str,
    total_micros: int = 108 * M,
    seller: str = "acme",
    buyer: str = "user-1",
    created_at: datetime = NOW,
    tenant: str = "t1",
) -> None:
    key = uuid4().hex
    record = OrderRecord(
        order_id=uuid4().hex,
        tenant_id=tenant,
        buyer_principal=buyer,
        seller_id=seller,
        intent_id=key,
        authorization_id=uuid4().hex,
        intent_digest="d" * 64,
        offer=_offer(subtotal_micros=total_micros, tax_estimate_micros=0),
        idempotency_key=key,
        take_rate_bps=500,
        created_at=created_at,
    )
    store.add(record, state=state)


def _spine(tmp_path):
    conn = DurableConnection(tmp_path / "durable.db")
    audit = DurableAuditLog(conn)
    history = OrderHistory(conn)
    spine = MarketplaceSpine(conn, audit=audit, history=history)
    spine.grant_delegation(_delegation())
    return conn, spine, OrderStore(conn), history


def test_spend_totals_and_familiarity_derive_from_the_book(tmp_path):
    conn, spine, store, history = _spine(tmp_path)
    _order(store, state="completed", total_micros=150 * M)
    _order(
        store,
        state="completed",
        total_micros=100 * M,
        created_at=NOW - timedelta(days=10),
    )
    _order(store, state="cancelled", total_micros=999 * M)  # never happened
    daily, monthly = history.spend_totals(
        tenant="t1", principal="user-1", now=NOW + timedelta(hours=1)
    )
    assert daily == 150 * M
    assert monthly == 250 * M
    assert history.seen_counterparty(
        tenant="t1", principal="user-1", counterparty="acme"
    )
    # The derivation lands in the verdict: a familiar seller is no longer
    # first-time, but today's spend pushes past the daily limit.
    risk = {
        k: v for k, v in _SAFE_RISK.items() if k != "first_time_counterparty"
    }
    stored, _ = spine.create_purchase_intent(
        tenant="t1",
        principal="user-1",
        agent="oolu",
        offer=_offer(),
        idempotency_key="derived",
        now=NOW + timedelta(hours=1),
        category="household",
        delivery_destination="home",
        risk_facts=risk,
    )
    assert stored.verdict.decision is Decision.REQUIRE_APPROVAL
    assert not any("first purchase" in r for r in stored.verdict.reasons)
    assert any("daily limit" in r for r in stored.verdict.reasons)
    conn.close()


def test_self_dealing_is_derived_and_denied(tmp_path):
    conn, spine, store, history = _spine(tmp_path)
    signals = history.assess(
        tenant="t1",
        principal="acme",
        counterparty="acme",
        amount_micros=10 * M,
        price_benchmark_micros=None,
        now=NOW,
    )
    assert signals.fraud_score >= 0.85
    # Through the spine: the derived score meets the deny bar.
    spine.grant_delegation(_delegation(principal_id="acme"))
    stored, _ = spine.create_purchase_intent(
        tenant="t1",
        principal="acme",
        agent="oolu",
        offer=_offer(),
        idempotency_key="selfdeal",
        now=NOW,
        category="household",
        delivery_destination="home",
        risk_facts={
            k: v
            for k, v in _SAFE_RISK.items()
        },
    )
    assert stored.state == "denied"
    assert any("fraud score" in r for r in stored.verdict.reasons)
    conn.close()


def test_reputation_derives_from_finished_orders_only(tmp_path):
    conn, _, store, history = _spine(tmp_path)
    assert history.reputation(counterparty="acme") is None  # no history
    for _ in range(3):
        _order(store, state="completed", buyer=uuid4().hex)
    assert history.reputation(counterparty="acme") == 1.0
    _order(store, state="refunded", buyer=uuid4().hex)
    assert history.reputation(counterparty="acme") == 0.75
    conn.close()


def test_an_explicit_fact_always_beats_the_derivation(tmp_path):
    conn, spine, store, history = _spine(tmp_path)
    # The book says this buyer knows acme…
    _order(store, state="completed")
    stored, _ = spine.create_purchase_intent(
        tenant="t1",
        principal="user-1",
        agent="oolu",
        offer=_offer(subtotal_micros=10 * M, tax_estimate_micros=0),
        idempotency_key="explicit",
        now=NOW + timedelta(days=2),
        category="household",
        delivery_destination="home",
        # …but the caller explicitly says first-time: the caller wins.
        risk_facts={**_SAFE_RISK, "first_time_counterparty": True},
    )
    assert any("first purchase" in r for r in stored.verdict.reasons)
    conn.close()
