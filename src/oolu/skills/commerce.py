"""Placing an order two ways: drive any site, or use a per-site adapter.

The engine treats "buy this" as a road network problem (README): there is
a general road that reaches every site, and faster private roads to the
sites we've learned. Both are ``ActionExecutor``s; the route optimizer
scores them and picks the cheaper viable one, so a per-site adapter WINS
whenever it's present and the general driver is the fallback that always
works.

* :class:`SiteDriverExecutor` (``adapter="web"``) drives an arbitrary
  storefront through a browser: open, search, add to cart, check out —
  many fragile steps. It reaches anywhere, which is exactly why it costs
  more to route.
* :class:`AmazonExecutor` (``adapter="amazon"``) places the order in one
  structured call against Amazon's own surface — fewer steps, higher
  reliability, lower cost. The shape every future per-site adapter copies.

Neither spends a cent on its own: an order action must carry an
``authorization_id`` that the injected ``is_authorized`` confirms was
released through the payment-consent + 2FA gate (Issue 6). No release, no
order — the security layer holds whichever road the route took.

The real drivers (a Playwright browser, Amazon's API) live behind ports so
the whole thing plans, routes, and tests without a network or a browser.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Protocol, runtime_checkable

from .models import ActionEvent, ExecutionOutcome, ExecutionStatus

# The capability an order action needs; the grounder resolves it when the
# matching executor is present, and the optimizer excludes a route whose
# capability nobody provides.
WEB_CHECKOUT = "checkout"
AMAZON_ORDER = "order"


@runtime_checkable
class SiteDriver(Protocol):
    """A browser the general executor drives — one call per fragile step.
    The production driver is Playwright behind the ``browser`` extra; a
    fake satisfies this in tests."""

    def step(self, operation: str, parameters: dict[str, Any]) -> dict[str, Any]: ...


@runtime_checkable
class AmazonClient(Protocol):
    """Amazon's own order surface — one structured call, not a form dance."""

    def place_order(self, parameters: dict[str, Any]) -> dict[str, Any]: ...


def _order_authorized(
    action: ActionEvent, is_authorized: Callable[[str], bool] | None
) -> str | None:
    """None when the order may proceed; otherwise the refusal reason.

    An order (a reserved, money-spending operation) must name an
    authorization the payment gate released. A non-order step needs none."""
    auth_id = action.parameters.get("authorization_id")
    if not auth_id:
        return "no payment authorization — an order needs the user's consent + 2FA"
    if is_authorized is not None and not is_authorized(str(auth_id)):
        return "the payment authorization is not released (consent + 2FA pending)"
    return None


class _BaseCommerceExecutor:
    def __init__(self, *, is_authorized: Callable[[str], bool] | None = None):
        self._is_authorized = is_authorized
        self._completed: dict[str, ExecutionOutcome] = {}
        self._lock = threading.RLock()

    def cancel(self, idempotency_key: str) -> None:
        return None

    def _done(
        self,
        action: ActionEvent,
        idempotency_key: str,
        status: ExecutionStatus,
        *,
        evidence: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> ExecutionOutcome:
        outcome = ExecutionOutcome(
            idempotency_key=idempotency_key,
            skill_id=f"{action.adapter}/{action.operation}",
            status=status,
            evidence=evidence or {},
            error=error,
        )
        with self._lock:
            self._completed[idempotency_key] = outcome
        return outcome


class SiteDriverExecutor(_BaseCommerceExecutor):
    """The general road: drive any storefront, one browser step at a time."""

    name = "web"

    def __init__(
        self,
        driver: SiteDriver,
        *,
        is_authorized: Callable[[str], bool] | None = None,
    ):
        super().__init__(is_authorized=is_authorized)
        self._driver = driver

    def capabilities(self) -> frozenset[str]:
        return frozenset({"open", "search", "add_to_cart", WEB_CHECKOUT})

    def execute(self, action: ActionEvent, *, idempotency_key: str) -> ExecutionOutcome:
        with self._lock:
            if idempotency_key in self._completed:
                return self._completed[idempotency_key]
        if action.adapter != self.name:
            return self._done(
                action, idempotency_key, ExecutionStatus.BLOCKED,
                error="not a web action",
            )
        if action.operation not in self.capabilities():
            return self._done(
                action, idempotency_key, ExecutionStatus.BLOCKED,
                error=f"unsupported web step: {action.operation}",
            )
        # Only the money-spending step is gated; navigation is free.
        if action.operation == WEB_CHECKOUT:
            refusal = _order_authorized(action, self._is_authorized)
            if refusal is not None:
                return self._done(
                    action, idempotency_key, ExecutionStatus.BLOCKED, error=refusal
                )
        try:
            evidence = self._driver.step(action.operation, dict(action.parameters))
        except Exception as exc:  # noqa: BLE001 - a site can fail any step
            return self._done(
                action, idempotency_key, ExecutionStatus.FAILED,
                error=f"web step '{action.operation}' failed: {exc}",
            )
        return self._done(
            action, idempotency_key, ExecutionStatus.SUCCEEDED, evidence=evidence
        )


class AmazonExecutor(_BaseCommerceExecutor):
    """The private road to Amazon: one structured order call."""

    name = "amazon"

    def __init__(
        self,
        client: AmazonClient,
        *,
        is_authorized: Callable[[str], bool] | None = None,
    ):
        super().__init__(is_authorized=is_authorized)
        self._client = client

    def capabilities(self) -> frozenset[str]:
        return frozenset({AMAZON_ORDER})

    def execute(self, action: ActionEvent, *, idempotency_key: str) -> ExecutionOutcome:
        with self._lock:
            if idempotency_key in self._completed:
                return self._completed[idempotency_key]
        if action.adapter != self.name or action.operation != AMAZON_ORDER:
            return self._done(
                action, idempotency_key, ExecutionStatus.BLOCKED,
                error="not an amazon order action",
            )
        refusal = _order_authorized(action, self._is_authorized)
        if refusal is not None:
            return self._done(
                action, idempotency_key, ExecutionStatus.BLOCKED, error=refusal
            )
        try:
            receipt = self._client.place_order(dict(action.parameters))
        except Exception as exc:  # noqa: BLE001
            return self._done(
                action, idempotency_key, ExecutionStatus.FAILED,
                error=f"amazon order failed: {exc}",
            )
        return self._done(
            action, idempotency_key, ExecutionStatus.SUCCEEDED, evidence=receipt
        )
