"""M1's live-Stripe half: the PaymentIntents adapter speaks the documented
wire shapes, the assembly swaps it in exactly when a key exists, and the
Stripe door reconciles order events idempotently.

Exit gate: authorize is a manual-capture PaymentIntent (tokenized refs
only — no field could carry a card number), capture/void/refund hit their
documented endpoints with idempotency keys aboard, provider errors surface
as PaymentError, the secret stays in the vault, a Stripe secret key flips
the order machine's provider to live (and no key leaves it on the fake),
and a signed payment_intent event completes an accepted order exactly once
no matter how many times Stripe retries the delivery.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from test_chat_model_router import FakeTransport
from test_http_gateway import _app
from test_marketplace_spine import _SAFE_RISK, _delegation, _offer

from oolu.billing.payout import PaymentError
from oolu.billing.psp import StripePaymentIntents
from oolu.gateway import GatewayApp, Request, StripeWebhookVerifier
from oolu.providers.vault import SecretVault

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
M = 1_000_000
WHSEC = "whsec_test_secret"
SECRET = "sk_test_abc123"


def _adapter():
    vault = SecretVault()
    ref = vault.put(SECRET, kind="stripe")
    transport = FakeTransport()
    return (
        StripePaymentIntents(vault=vault, transport=transport, api_key_ref=ref),
        transport,
    )


# --------------------------------------------------------------------------- #
# Wire shapes.                                                                 #
# --------------------------------------------------------------------------- #
def test_authorize_is_a_manual_capture_payment_intent():
    psp, transport = _adapter()
    transport.script("api.stripe.com", 200, {"id": "pi_1"})
    auth_ref = psp.authorize(
        amount_micros=108 * M,
        currency="USD",
        customer_ref="cus_1",
        payment_method_ref="pm_1",
        idempotency_key="auth:t1:k1",
        metadata={"oolu_order_id": "o1", "oolu_tenant": "t1"},
    )
    assert auth_ref == "pi_1"
    request = transport.requests[-1]
    assert request["url"].endswith("/v1/payment_intents")
    body = request["body"]
    assert body["amount"] == "10800"  # micros -> minor units
    assert body["currency"] == "usd"
    assert body["capture_method"] == "manual"
    assert body["confirm"] == "true"
    assert body["payment_method"] == "pm_1"
    assert body["metadata[oolu_order_id]"] == "o1"
    assert body["metadata[oolu_tenant]"] == "t1"
    assert request["headers"]["Authorization"] == f"Bearer {SECRET}"
    assert request["headers"]["Idempotency-Key"] == "auth:t1:k1"
    # Tokenized references only: nothing in the body could hold a PAN.
    assert not any("card" in key for key in body)


def test_capture_void_and_refund_hit_their_documented_endpoints():
    psp, transport = _adapter()
    transport.script(
        "api.stripe.com", 200, {"id": "pi_1", "latest_charge": "ch_9"}
    )
    charge_ref = psp.capture(
        "pi_1", amount_micros=108 * M, idempotency_key="capture:t1:k1"
    )
    assert charge_ref == "ch_9"
    capture = transport.requests[-1]
    assert capture["url"].endswith("/v1/payment_intents/pi_1/capture")
    assert capture["body"]["amount_to_capture"] == "10800"

    psp.void("pi_1", idempotency_key="void:t1:k1")
    assert transport.requests[-1]["url"].endswith(
        "/v1/payment_intents/pi_1/cancel"
    )

    transport.script("api.stripe.com", 200, {"id": "re_1"})
    refund_ref = psp.refund(
        "pi_1", amount_micros=108 * M, idempotency_key="refund:t1:k1"
    )
    assert refund_ref == "re_1"
    refund = transport.requests[-1]
    assert refund["url"].endswith("/v1/refunds")
    assert refund["body"]["payment_intent"] == "pi_1"
    assert refund["body"]["amount"] == "10800"


def test_provider_errors_surface_as_payment_errors():
    psp, transport = _adapter()
    transport.script("api.stripe.com", 402, {"error": {"code": "card_declined"}})
    with pytest.raises(PaymentError):
        psp.authorize(
            amount_micros=10 * M,
            currency="USD",
            customer_ref="cus_1",
            payment_method_ref="pm_1",
            idempotency_key="k",
        )


def test_the_secret_stays_in_the_vault():
    psp, transport = _adapter()
    assert SECRET not in repr(vars(psp))
    transport.script("api.stripe.com", 200, {"id": "pi_1"})
    psp.authorize(
        amount_micros=1 * M,
        currency="USD",
        customer_ref="cus_1",
        payment_method_ref="pm_1",
        idempotency_key="k",
    )
    # The secret exists exactly once: in the header the vault minted for
    # the transport. Adapter state still holds only the reference.
    assert SECRET in transport.requests[-1]["headers"]["Authorization"]
    assert SECRET not in repr(vars(psp))


# --------------------------------------------------------------------------- #
# Assembly: the provider follows the key.                                      #
# --------------------------------------------------------------------------- #
def test_assembly_swaps_the_order_machines_provider_with_the_key(tmp_path):
    from oolu.assembly import build_host_runtime

    fake = build_host_runtime(
        data_dir=tmp_path / "fake",
        secret="a-thirty-two-character-plus-signing-secret",
    )
    try:
        assert fake.gateway._commerce_orders.psp_mode == "test"
    finally:
        fake.close()
    live = build_host_runtime(
        data_dir=tmp_path / "live",
        secret="a-thirty-two-character-plus-signing-secret",
        stripe_secret_key="sk_test_123",
        stripe_webhook_secret=WHSEC,
    )
    try:
        assert live.gateway._commerce_orders.psp_mode == "live"
    finally:
        live.close()


# --------------------------------------------------------------------------- #
# The Stripe door: order events reconcile exactly once.                        #
# --------------------------------------------------------------------------- #
def _stripe_gateway(tmp_path):
    app, conn, ident = _app(tmp_path)
    gateway = GatewayApp(
        app._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        stripe_webhooks=StripeWebhookVerifier(WHSEC),
    )
    return gateway, conn


def _stripe_event(gateway, event: dict):
    raw = json.dumps(event).encode()
    header = StripeWebhookVerifier(WHSEC).sign_header(raw, now=NOW)
    return gateway.handle(
        Request(
            method="POST",
            path="/v1/webhooks/stripe",
            headers={"Stripe-Signature": header},
            body=event,
            raw=raw,
            now=NOW,
        )
    )


def test_a_retried_payment_event_completes_the_order_exactly_once(tmp_path):
    gateway, conn = _stripe_gateway(tmp_path)
    spine = gateway._commerce
    orders = gateway._commerce_orders
    spine.grant_delegation(_delegation())
    stored, _ = spine.create_purchase_intent(
        tenant="t1",
        principal="user-1",
        agent="oolu",
        offer=_offer(),
        idempotency_key="wh-1",
        now=NOW,
        category="household",
        delivery_destination="home",
        risk_facts=dict(_SAFE_RISK),
    )
    spine.record_approval(
        stored.intent.intent_id,
        tenant="t1",
        approver_id="user-1",
        assurance_level=1,
        approve=True,
        now=NOW,
    )
    order, _ = orders.place_order(
        intent_id=stored.intent.intent_id,
        tenant="t1",
        now=NOW,
        customer_ref="cus_1",
        payment_method_ref="pm_ok",
    )
    order_id = order.record.order_id
    orders.mark_shipped(order_id, tenant="t1", actor="user-1", now=NOW)
    orders.mark_delivered(order_id, tenant="t1", actor="user-1", now=NOW)
    # Acceptance was recorded but the process died before capture: the
    # provider's delivery is what finishes the order.
    assert orders.orders.set_state(
        order_id, tenant="t1", state="accepted", expected=("delivered",)
    )
    event = {
        "id": "evt_cap_1",
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": order.record.auth_ref,
                "metadata": {"oolu_order_id": order_id, "oolu_tenant": "t1"},
            }
        },
    }
    first = _stripe_event(gateway, event)
    assert first.status == 200, first.body
    assert first.body["order"]["applied"] is True
    assert orders.orders.get(order_id, tenant="t1").state == "completed"
    ledger = gateway._commerce_ledger
    assert len(ledger.transactions(order_id=order_id)) == 1

    # Stripe retries the same delivery: the idempotent envelope replays
    # the cached result and the book gains nothing.
    second = _stripe_event(gateway, event)
    assert second.status == 200
    assert len(ledger.transactions(order_id=order_id)) == 1
    assert sum(ledger.trial_balance().values()) == 0
    conn.close()
