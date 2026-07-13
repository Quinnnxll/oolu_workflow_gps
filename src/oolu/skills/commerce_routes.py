"""The two roads to an order, as candidate routes the optimizer scores.

Given one shopping intent, this builds the competing blueprints the engine
ranks: the general web route (many fragile steps, reaches any site, higher
cost) and a per-site route (one structured call, lower cost) for a site we
have an adapter for. Both end in a RESERVED order action — the money step
the payment-consent + 2FA gate governs. The optimizer excludes any route
whose capability isn't grounded (its adapter isn't installed) and, among
the survivors, picks the cheapest — so the per-site adapter wins when it's
present and the general driver is the fallback that always works.

Costs are deliberate, not magic: a step is a unit of fragility and time,
so the general route's cost rises with its step count while a one-call
adapter stays cheap. Tune the constants as real reliability data arrives.
"""

from __future__ import annotations

from ..orchestrator.state import Blueprint, ReservedAction
from .commerce import AMAZON_ORDER, WEB_CHECKOUT
from .models import ActionEvent

# Per-step cost of the general driver (each browser action can fail); the
# adapter's flat cost is far lower because it's one reliable call.
_WEB_STEP_COST = 2.0
_ADAPTER_ORDER_COST = 2.0


def _action(correlation: str, adapter: str, operation: str, params: dict) -> ActionEvent:
    return ActionEvent(
        correlation_id=correlation,
        adapter=adapter,
        operation=operation,
        parameters=params,
    )


def general_web_route(
    *,
    correlation: str,
    url: str,
    query: str,
    order_params: dict,
) -> Blueprint:
    """The road to ANY storefront: open, search, add to cart, check out."""
    steps = [
        ("open", {"url": url}),
        ("search", {"query": query}),
        ("add_to_cart", {"query": query}),
        (WEB_CHECKOUT, order_params),
    ]
    actions = [
        ReservedAction(
            action=_action(correlation, "web", op, params),
            required_capabilities=frozenset({op}),
            # Only the checkout spends money — mark it reserved (gated).
            reserved=(op == WEB_CHECKOUT),
            risk="irreversible" if op == WEB_CHECKOUT else "write",
        )
        for op, params in steps
    ]
    return Blueprint(
        name="web: drive the storefront",
        actions=actions,
        estimated_cost=_WEB_STEP_COST * len(steps),
    )


def amazon_route(*, correlation: str, order_params: dict) -> Blueprint:
    """The private road to Amazon: place the order in one call."""
    return Blueprint(
        name="amazon: one-call order",
        actions=[
            ReservedAction(
                action=_action(correlation, "amazon", AMAZON_ORDER, order_params),
                required_capabilities=frozenset({AMAZON_ORDER}),
                reserved=True,
                risk="irreversible",
            )
        ],
        estimated_cost=_ADAPTER_ORDER_COST,
    )


def commerce_routes(
    *,
    correlation: str,
    url: str,
    query: str,
    order_params: dict,
    amazon: bool,
) -> list[Blueprint]:
    """Every candidate road for this order. The general route is always
    offered; the Amazon route is added when the intent names Amazon (a
    per-site adapter only competes for the site it knows)."""
    routes = [
        general_web_route(
            correlation=correlation, url=url, query=query, order_params=order_params
        )
    ]
    if amazon:
        routes.insert(0, amazon_route(correlation=correlation, order_params=order_params))
    return routes
