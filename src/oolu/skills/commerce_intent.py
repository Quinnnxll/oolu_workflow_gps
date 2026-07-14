"""From a plain-language ask to a commerce blueprint carrying its order intent.

This is the piece that was missing above the mint-and-attach seam: nothing
turned "buy me a stainless-steel water bottle on Amazon for $24.99" into a
commerce blueprint whose order action declares what it will spend. This module
does — deterministically, and conservatively about the one field that must
never be guessed: the amount.

The pipeline is three small, pure steps:

1. ``parse_order_intent`` — a plain-text ask (or a brief's already-extracted
   slots) → a typed :class:`OrderIntent`, or ``None`` when the ask is not a
   purchase or is underspecified. It NEVER invents an amount: an ask without
   an exact, stated price returns ``None`` (the caller asks for the price)
   rather than fabricating a number the user would then be asked to authorize.
2. ``order_params_for`` — an intent → the order action's parameters: the
   payee/amount/currency/description ``PaymentAuthorizationResolver``
   reconciles consent against, plus the driver's keys
   (``browser_steps``/``login_probe``). The run context (run_id, account
   scope) is NOT set here — the run this order belongs to is unknown at plan
   time and is stamped at execution (``stamp_order_context``).
3. ``plan_commerce_blueprints`` — the intent → the candidate roads
   (``commerce_routes``): the general web road always, the Amazon road when
   the ask names Amazon. The optimizer scores them exactly as before.

Deliberately deferred, and named so no one mistakes the boundary: fuzzy natural
language beyond these patterns belongs behind an LLM intake port (it produces
the same ``OrderIntent``); the site-specific ``browser_steps`` that actually
click through a given storefront come from site profiles / learned adapters,
not from this parser; and reconciling the *observed* cart total against the
*authorized* amount (so a $25 consent can't become a $40 charge) is a verify
step this module leaves for the checkout to enforce.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..orchestrator.state import Blueprint
from .commerce_routes import commerce_routes

_PURCHASE_VERBS = (
    "buy",
    "order",
    "purchase",
    "reorder",
    "get me",
    "shop for",
    "pick up",
)

# A ceiling ("under $30") is NOT an amount to authorize — it is a budget. If
# one of these precedes the number, we refuse to treat that number as the
# exact price, and the ask is underspecified until the real total is known.
_CEILING_WORDS = ("under", "below", "up to", "max", "less than", "no more than", "around", "about")

# Exact-amount patterns. Group 1 is the number.
_AMOUNT_PATTERNS = (
    re.compile(r"[$€£]\s*(\d+(?:\.\d{1,2})?)"),
    re.compile(r"(\d+(?:\.\d{1,2})?)\s*(?:usd|dollars?|eur|euros?|gbp|pounds?)", re.I),
)

_CURRENCY_BY_SYMBOL = {"$": "USD", "€": "EUR", "£": "GBP"}
_CURRENCY_WORDS = {
    "usd": "USD", "dollar": "USD", "dollars": "USD",
    "eur": "EUR", "euro": "EUR", "euros": "EUR",
    "gbp": "GBP", "pound": "GBP", "pounds": "GBP",
}


@dataclass(frozen=True)
class OrderIntent:
    """A parsed purchase: what to buy, where, and the exact amount to spend."""

    merchant: str
    query: str
    amount_micros: int  # exact — never invented; a stated price only
    currency: str = "USD"
    description: str = ""
    site_url: str | None = None
    is_amazon: bool = False
    # Per-operation browser primitives (site knowledge, supplied by a site
    # profile or a learned adapter — NOT by this parser). Keys are commerce
    # operations ("open"/"search"/"add_to_cart"/"checkout"/"order").
    browser_steps: dict[str, list[dict]] = field(default_factory=dict)
    login_probe: str | None = None


def _amount_and_currency(text: str) -> tuple[int, str] | None:
    for pattern in _AMOUNT_PATTERNS:
        for match in pattern.finditer(text):
            # Refuse a number a ceiling word governs — that is a budget, not
            # a price to authorize.
            prefix = text[: match.start()].rstrip().lower()
            if any(prefix.endswith(word) for word in _CEILING_WORDS):
                continue
            number = float(match.group(1))
            if number <= 0:
                continue
            symbol = match.group(0)[0]
            currency = _CURRENCY_BY_SYMBOL.get(symbol)
            if currency is None:
                tail = text[match.end() : match.end() + 8].lower()
                currency = next(
                    (cur for word, cur in _CURRENCY_WORDS.items() if word in tail),
                    "USD",
                )
            return round(number * 1_000_000), currency
    return None


def _merchant_and_site(text: str) -> tuple[str, str | None, bool]:
    lower = text.lower()
    if "amazon" in lower:
        return "Amazon", "https://www.amazon.com", True
    url = re.search(r"https?://[^\s]+", text)
    if url:
        host = re.sub(r"^https?://(www\.)?", "", url.group(0)).split("/")[0]
        return host, url.group(0), False
    named = re.search(r"\b(?:on|from|at)\s+([A-Z][\w&'-]*(?:\s+[A-Z][\w&'-]*)?)", text)
    if named:
        return named.group(1).strip(), None, False
    return "the store", None, False


def _query(text: str, merchant: str) -> str:
    stripped = text
    for verb in _PURCHASE_VERBS:
        stripped = re.sub(rf"(?i)\b{re.escape(verb)}\b", " ", stripped)
    # Drop merchant / site / amount / filler phrases so what's left is the item.
    if merchant and merchant != "the store":
        stripped = re.sub(
            rf"(?i)\b(?:on|from|at)\s+{re.escape(merchant)}\b", " ", stripped
        )
    stripped = re.sub(r"(?i)\b(?:on|from|at)\s+amazon\b", " ", stripped)
    stripped = re.sub(r"https?://[^\s]+", " ", stripped)
    for pattern in _AMOUNT_PATTERNS:
        stripped = pattern.sub(" ", stripped)
    stripped = re.sub(
        r"(?i)\b(for|me|a|an|the|please|could you|can you)\b", " ", stripped
    )
    return re.sub(r"\s+", " ", stripped).strip(" .,!?")


def parse_order_intent(text: str) -> OrderIntent | None:
    """A purchase ask → a typed intent, or ``None`` if not a well-formed one.

    Returns ``None`` when the text is not a purchase, or when it states no
    exact amount to authorize (a budget ceiling like "under $30" does not
    count) — the caller should then ask for the price rather than proceed.
    """
    if not text or not text.strip():
        return None
    lower = text.lower()
    if not any(verb in lower for verb in _PURCHASE_VERBS):
        return None
    amount = _amount_and_currency(text)
    if amount is None:
        return None  # no exact price — underspecified, never invented
    amount_micros, currency = amount
    merchant, site_url, is_amazon = _merchant_and_site(text)
    query = _query(text, merchant)
    if not query:
        return None
    return OrderIntent(
        merchant=merchant,
        query=query,
        amount_micros=amount_micros,
        currency=currency,
        description=text.strip(),
        site_url=site_url,
        is_amazon=is_amazon,
    )


def order_params_for(intent: OrderIntent) -> dict[str, Any]:
    """The order action's parameters: the consent intent + the driver payload.

    Carries what :class:`~oolu.billing.PaymentAuthorizationResolver` reconciles
    consent against — payee, exact amount, currency, description — and what the
    site driver / Amazon client run (per-op browser steps, the login probe).
    The run context (``run_id``, ``authorization_scope``) and the
    ``authorization_id`` are deliberately ABSENT here: the run this order
    belongs to is not known at planning time, so it is stamped at execution
    (``stamp_order_context``), and the id is filled by the resolver once the
    user has consented.
    """
    op = "order" if intent.is_amazon else "checkout"
    params: dict[str, Any] = {
        "merchant": intent.merchant,
        "amount_micros": intent.amount_micros,
        "currency": intent.currency,
        "description": intent.description or f"{intent.query} from {intent.merchant}",
    }
    if intent.browser_steps.get(op):
        params["browser_steps"] = intent.browser_steps[op]
    if intent.login_probe:
        params["login_probe"] = intent.login_probe
    if intent.is_amazon:
        params["site"] = intent.merchant
    return params


def plan_commerce_blueprints(intent: OrderIntent, *, correlation: str) -> list[Blueprint]:
    """The candidate roads for this order, each ending in a reserved order
    action carrying the consent intent (payee + exact amount). The run context
    is stamped later, at execution binding."""
    return commerce_routes(
        correlation=correlation,
        url=intent.site_url or "",
        query=intent.query,
        order_params=order_params_for(intent),
        amazon=intent.is_amazon,
    )
