"""Intent → blueprint: a plain-language ask becomes a commerce route.

The unit tests pin the parser and the blueprint builder; the last test drives
the WHOLE pipeline through a real WorkflowOrchestrator — intent → brief →
commerce route → execution stamps run_id + scope → the payment resolver files
consent → authorize → the order runs — the end-to-end path that was impossible
before the planner existed.
"""

from __future__ import annotations

from oolu.assembly import PlanningOnlyOptimizer
from oolu.orchestrator.adapters import (
    CommerceRouteOptimizer,
    stamp_order_context,
)
from oolu.orchestrator.state import (
    Blueprint,
    ReservedAction,
    RoutePlan,
    SemanticGrounding,
)
from oolu.skills.commerce import AMAZON_ORDER, WEB_CHECKOUT
from oolu.skills.commerce_intent import (
    order_params_for,
    parse_order_intent,
    plan_commerce_blueprints,
)
from oolu.skills.models import ActionEvent
from oolu.skills.requirements import RequirementBrief


# --------------------------------------------------------------------------- #
# The parser: conservative, never invents an amount.                           #
# --------------------------------------------------------------------------- #
def test_parses_an_amazon_purchase_with_an_exact_price():
    intent = parse_order_intent("buy me a stainless steel water bottle on Amazon for $24.99")
    assert intent is not None
    assert intent.is_amazon and intent.merchant == "Amazon"
    assert intent.query == "stainless steel water bottle"
    assert intent.amount_micros == 24_990_000
    assert intent.currency == "USD"


def test_parses_a_named_non_amazon_merchant_and_currency():
    intent = parse_order_intent("order a mechanical keyboard from NewEgg for 89.50 USD")
    assert intent is not None
    assert not intent.is_amazon
    assert intent.merchant == "NewEgg"
    assert intent.query == "mechanical keyboard"
    assert intent.amount_micros == 89_500_000


def test_a_budget_ceiling_is_not_an_amount_to_authorize():
    # "under $30" is a budget, not a price — refuse to treat it as exact.
    assert parse_order_intent("get me a coffee grinder under $30 on amazon") is None
    assert parse_order_intent("buy a lamp for no more than $50") is None


def test_a_non_purchase_ask_is_not_an_order():
    assert parse_order_intent("what's the weather in Nairobi today") is None
    assert parse_order_intent("summarize this document") is None


def test_a_purchase_with_no_price_is_underspecified():
    # No amount at all -> None (the caller should ask for the price).
    assert parse_order_intent("buy me a water bottle on amazon") is None


# --------------------------------------------------------------------------- #
# The blueprint builder: order actions carry the consent intent, not the run.  #
# --------------------------------------------------------------------------- #
def test_order_params_carry_the_consent_intent_but_not_the_run():
    intent = parse_order_intent("buy a desk lamp on Amazon for $30")
    params = order_params_for(intent)
    assert params["merchant"] == "Amazon"
    assert params["amount_micros"] == 30_000_000
    # The run and scope are stamped later, at execution — not here.
    assert "run_id" not in params
    assert "authorization_scope" not in params


def test_amazon_ask_offers_the_amazon_road_and_the_web_fallback():
    intent = parse_order_intent("buy running shoes on Amazon for $60")
    blueprints = plan_commerce_blueprints(intent, correlation="c")
    names = [b.name for b in blueprints]
    assert any("amazon" in n for n in names)
    assert any("web" in n for n in names)
    # The Amazon order action needs the AMAZON_ORDER capability.
    amazon = next(b for b in blueprints if "amazon" in b.name)
    assert AMAZON_ORDER in amazon.actions[0].required_capabilities


def test_non_amazon_ask_offers_only_the_web_road():
    intent = parse_order_intent("order a notebook from Kifaru for $12")
    blueprints = plan_commerce_blueprints(intent, correlation="c")
    assert all("amazon" not in b.name for b in blueprints)
    checkout = blueprints[0].actions[-1]
    assert WEB_CHECKOUT in checkout.required_capabilities


# --------------------------------------------------------------------------- #
# The optimizer seat: a purchase brief yields commerce routes; else passthrough.
# --------------------------------------------------------------------------- #
def test_commerce_optimizer_routes_a_purchase_brief():
    optimizer = CommerceRouteOptimizer(
        PlanningOnlyOptimizer(),
        capabilities=frozenset({AMAZON_ORDER, WEB_CHECKOUT, "open", "search", "add_to_cart"}),
    )
    brief = RequirementBrief(intent="buy a water bottle on Amazon for $20")
    plan = optimizer.optimize(brief, SemanticGrounding())
    # Amazon road (cheapest) chosen, and it is NOT excluded (self-grounded).
    assert "amazon" in plan.chosen.name
    assert not plan.chosen.excluded


def test_commerce_optimizer_passes_non_purchase_briefs_through():
    optimizer = CommerceRouteOptimizer(PlanningOnlyOptimizer())
    brief = RequirementBrief(intent="summarize my inbox")
    plan = optimizer.optimize(brief, SemanticGrounding())
    assert plan.chosen.name == "unconfigured"  # the fallback's route


# --------------------------------------------------------------------------- #
# Stamping: the run and scope land on the order action, at execution time.     #
# --------------------------------------------------------------------------- #
def _route_with_order(**params):
    order = ReservedAction(
        action=ActionEvent(
            correlation_id="c", adapter="amazon", operation="order", parameters=params
        ),
        required_capabilities=frozenset({AMAZON_ORDER}),
        reserved=True,
    )
    return RoutePlan(chosen=Blueprint(name="amazon", actions=[order]))


def test_stamp_writes_run_and_scope_onto_the_order_action():
    route = _route_with_order(merchant="Amazon", amount_micros=20_000_000)
    stamped = stamp_order_context(route, run_id="run-9", authorization_scope="main:alice")
    p = stamped.chosen.actions[0].action.parameters
    assert p["run_id"] == "run-9"
    assert p["authorization_scope"] == "main:alice"


def test_stamp_leaves_a_non_order_action_untouched():
    nav = ReservedAction(
        action=ActionEvent(correlation_id="c", adapter="web", operation="open"),
        required_capabilities=frozenset({"open"}),
    )
    route = RoutePlan(chosen=Blueprint(name="web", actions=[nav]))
    stamped = stamp_order_context(route, run_id="r", authorization_scope="s")
    assert "run_id" not in stamped.chosen.actions[0].action.parameters


def test_stamp_does_not_override_an_explicit_scope():
    route = _route_with_order(
        merchant="Amazon", amount_micros=20_000_000, authorization_scope="fixed:bob"
    )
    stamped = stamp_order_context(route, run_id="r", authorization_scope="main:alice")
    # A plan that fixed its own scope keeps it.
    assert stamped.chosen.actions[0].action.parameters["authorization_scope"] == "fixed:bob"


# --------------------------------------------------------------------------- #
# End to end: the whole chain the engine runs, against the REAL consent store. #
# --------------------------------------------------------------------------- #
def test_intent_to_order_runs_end_to_end_through_the_real_consent_store(tmp_path):
    from oolu.billing import PaymentAuthorizationResolver, PaymentAuthorizationStore
    from oolu.durable.connection import DurableConnection
    from oolu.identity import TotpStore, totp
    from oolu.orchestrator.adapters import (
        ActionExecutorRouteRunner,
        bind_brief_parameters,
    )
    from oolu.skills.commerce import AmazonExecutor

    NOW = 1_700_000_000.0
    scope = "main:alice"
    conn = DurableConnection(tmp_path / "d.db")
    totp_store = TotpStore(conn, key_path=tmp_path / "machine.key")
    begun = totp_store.begin_enroll("alice")
    secret = begun["secret"]
    totp_store.confirm_enroll("alice", totp.code_at_time(secret, now=NOW), now=NOW)
    store = PaymentAuthorizationStore(
        conn,
        verify_second_factor=lambda s, code: totp_store.verify(
            s.split(":", 1)[-1], code, now=NOW
        ),
        second_factor_enrolled=lambda s: totp_store.is_enrolled(s.split(":", 1)[-1]),
    )

    class _Amazon:
        def __init__(self):
            self.orders = 0

        def place_order(self, parameters):
            self.orders += 1
            return {"order_id": "A1"}

    client = _Amazon()
    executor = AmazonExecutor(
        client,
        is_authorized=store.is_authorized,
        orders_enabled=lambda: True,
        resolve_authorization=PaymentAuthorizationResolver(store).resolve,
    )
    runner = ActionExecutorRouteRunner({"amazon": executor})

    # 1. The ask becomes a route, exactly as the optimizer seat would produce it.
    brief = RequirementBrief(intent="buy a stainless steel water bottle on Amazon for $24.99")
    plan = CommerceRouteOptimizer(
        PlanningOnlyOptimizer(), capabilities=runner.capabilities()
    ).optimize(brief, SemanticGrounding())
    assert "amazon" in plan.chosen.name

    # 2. Execution binding stamps the run + scope (what _phase_execution does).
    def _bound():
        return stamp_order_context(
            bind_brief_parameters(plan, brief),
            run_id="run-1",
            authorization_scope=scope,
        )

    try:
        # 3. First execution files the consent request and blocks, unspent.
        first = runner.execute(_bound(), idempotency_key="k1", attempt=1)
        assert first.status.value == "failed"  # the run stops on the blocked order
        pending = store.pending(scope)
        assert len(pending) == 1
        assert pending[0].amount_micros == 24_990_000
        assert client.orders == 0

        # 4. The user consents (exact amount + fresh 2FA), and the order runs.
        store.authorize(
            scope,
            pending[0].auth_id,
            confirm_amount_micros=24_990_000,
            code=totp.code_at_time(secret, now=NOW),
        )
        second = runner.execute(_bound(), idempotency_key="k2", attempt=2)
        assert second.status.value == "succeeded"
        assert client.orders == 1
    finally:
        conn.close()
