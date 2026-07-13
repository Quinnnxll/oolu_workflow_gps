"""Can OoLu plan an order, route it, and score the BEST road? Yes — proven.

The engine treats "buy this" as a road network: a general driver reaches
any storefront (many fragile steps), and a per-site adapter is a fast
private road to a site it knows (one call). This exercises the real
optimizer end to end:

* both roads are viable → the scoring picks the cheaper per-site adapter;
* the adapter isn't installed → its route is EXCLUDED and the general
  driver is chosen, so the order still routes (graceful fallback);
* the chosen route actually executes through the right executor;
* and neither road spends a cent without a released payment authorization
  (the Issue-6 consent + 2FA gate holds whichever road is taken).

Fakes stand in for the browser and Amazon so planning, routing, and
scoring are proven without a network.
"""

from __future__ import annotations

from oolu.orchestrator.adapters import (
    ActionExecutorRouteRunner,
    LeastCostRouteOptimizer,
)
from oolu.orchestrator.state import SemanticGrounding
from oolu.skills.commerce import AmazonExecutor, SiteDriverExecutor
from oolu.skills.commerce_routes import amazon_route, commerce_routes, general_web_route
from oolu.skills.models import ExecutionStatus
from oolu.skills.requirements import RequirementBrief

_ORDER = {
    "merchant": "Amazon",
    "item": "Deep Learning (hardcover)",
    "amount_micros": 42_000_000,
    "currency": "USD",
    "authorization_id": "auth-1",
}


class _FakeBrowser:
    def __init__(self):
        self.steps: list[str] = []

    def step(self, operation, parameters):
        self.steps.append(operation)
        return {"step": operation, "ok": True, "order_id": "web-order-9"}


class _FakeAmazon:
    def __init__(self):
        self.orders: list[dict] = []

    def place_order(self, parameters):
        self.orders.append(parameters)
        return {"order_id": "111-2223334", "total_micros": parameters["amount_micros"]}


def _grounding(runner: ActionExecutorRouteRunner) -> SemanticGrounding:
    # A capability is grounded exactly when an installed executor provides
    # it — the real "is this road drivable here?" question.
    return SemanticGrounding(resolved_capabilities=runner.capabilities())


_BRIEF = RequirementBrief(intent="order Deep Learning on Amazon")


# --------------------------------------------------------------------------- #
# Scoring: the per-site adapter is cheaper, and the optimizer picks it.        #
# --------------------------------------------------------------------------- #
def test_the_adapter_route_is_cheaper_and_scoring_picks_it():
    amazon = amazon_route(correlation="c", order_params=_ORDER)
    web = general_web_route(
        correlation="c", url="https://amazon.com", query="deep learning", order_params=_ORDER
    )
    # The private road really is cheaper — fewer, more reliable steps.
    assert amazon.estimated_cost < web.estimated_cost
    assert len(amazon.actions) < len(web.actions)

    both = SiteDriverExecutor(_FakeBrowser()), AmazonExecutor(_FakeAmazon())
    runner = ActionExecutorRouteRunner({e.name: e for e in both})
    plan = LeastCostRouteOptimizer([amazon, web]).optimize(_BRIEF, _grounding(runner))

    # Both roads are viable here — and the scoring chose the adapter.
    assert plan.chosen.name.startswith("amazon")
    assert plan.total_cost == amazon.estimated_cost
    assert all(not alt.excluded for alt in plan.alternatives)  # general still offered
    # The money step is reserved (gated) whichever road wins.
    assert plan.chosen.actions[-1].reserved is True


def test_without_the_adapter_the_general_road_is_chosen_not_excluded_away():
    # Only the general driver is installed: the Amazon capability isn't
    # grounded, so its route is excluded — but the order still routes.
    runner = ActionExecutorRouteRunner({"web": SiteDriverExecutor(_FakeBrowser())})
    routes = commerce_routes(
        correlation="c", url="https://shop.example", query="widget",
        order_params={**_ORDER, "merchant": "Example"}, amazon=True,
    )
    plan = LeastCostRouteOptimizer(routes).optimize(_BRIEF, _grounding(runner))
    assert plan.chosen.name.startswith("web")
    # The Amazon route is present but excluded, with the reason named.
    amazon_alt = next(a for a in plan.alternatives if a.name.startswith("amazon"))
    assert amazon_alt.excluded and "order" in (amazon_alt.exclusion_reason or "")


# --------------------------------------------------------------------------- #
# Routing: the chosen road actually executes through the right executor.       #
# --------------------------------------------------------------------------- #
def test_the_chosen_route_executes_through_its_executor():
    browser, amazon_client = _FakeBrowser(), _FakeAmazon()
    runner = ActionExecutorRouteRunner(
        {"web": SiteDriverExecutor(browser), "amazon": AmazonExecutor(amazon_client)}
    )
    routes = commerce_routes(
        correlation="c", url="https://amazon.com", query="deep learning",
        order_params=_ORDER, amazon=True,
    )
    plan = LeastCostRouteOptimizer(routes).optimize(_BRIEF, _grounding(runner))
    record = runner.execute(plan, idempotency_key="run-1", attempt=1)

    assert record.status is ExecutionStatus.SUCCEEDED
    # It went down the Amazon road — one order call, no browser steps.
    assert amazon_client.orders and browser.steps == []
    assert record.action_outcomes[-1].evidence["order_id"] == "111-2223334"


def test_the_general_road_executes_every_step_when_it_is_the_route():
    browser = _FakeBrowser()
    runner = ActionExecutorRouteRunner({"web": SiteDriverExecutor(browser)})
    routes = commerce_routes(
        correlation="c", url="https://shop.example", query="widget",
        order_params={**_ORDER, "merchant": "Example"}, amazon=False,
    )
    plan = LeastCostRouteOptimizer(routes).optimize(_BRIEF, _grounding(runner))
    record = runner.execute(plan, idempotency_key="run-2", attempt=1)
    assert record.status is ExecutionStatus.SUCCEEDED
    assert browser.steps == ["open", "search", "add_to_cart", "checkout"]


# --------------------------------------------------------------------------- #
# The gate holds on both roads: no release, no order.                          #
# --------------------------------------------------------------------------- #
def test_no_road_spends_without_a_released_authorization():
    released: set[str] = set()
    is_authorized = lambda auth_id: auth_id in released  # noqa: E731

    for executor, route in (
        (
            AmazonExecutor(_FakeAmazon(), is_authorized=is_authorized),
            amazon_route(correlation="c", order_params=_ORDER),
        ),
        (
            SiteDriverExecutor(_FakeBrowser(), is_authorized=is_authorized),
            general_web_route(
                correlation="c", url="https://amazon.com", query="dl",
                order_params=_ORDER,
            ),
        ),
    ):
        runner = ActionExecutorRouteRunner({executor.name: executor})
        grounding = SemanticGrounding(resolved_capabilities=executor.capabilities())
        plan = LeastCostRouteOptimizer([route]).optimize(_BRIEF, grounding)

        # Unreleased: the money step is BLOCKED at the executor, so the
        # run fails without spending — the order never reaches the site.
        blocked = runner.execute(plan, idempotency_key=f"{executor.name}-x", attempt=1)
        assert blocked.status is ExecutionStatus.FAILED
        assert blocked.action_outcomes[-1].status is ExecutionStatus.BLOCKED
        assert "authorization" in (blocked.error or "")

        # Released: the same order goes through.
        released.add("auth-1")
        ok = runner.execute(plan, idempotency_key=f"{executor.name}-y", attempt=1)
        assert ok.status is ExecutionStatus.SUCCEEDED
        released.discard("auth-1")


def test_an_order_with_no_authorization_id_at_all_is_refused():
    runner = ActionExecutorRouteRunner({"amazon": AmazonExecutor(_FakeAmazon())})
    route = amazon_route(
        correlation="c", order_params={k: v for k, v in _ORDER.items()
                                       if k != "authorization_id"},
    )
    grounding = SemanticGrounding(resolved_capabilities=runner.capabilities())
    plan = LeastCostRouteOptimizer([route]).optimize(_BRIEF, grounding)
    record = runner.execute(plan, idempotency_key="z", attempt=1)
    assert record.status is ExecutionStatus.FAILED
    assert record.action_outcomes[-1].status is ExecutionStatus.BLOCKED
    assert "consent" in (record.error or "")
