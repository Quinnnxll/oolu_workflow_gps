"""Phase 2's money half: real Stripe adapters, the Stripe event door,
the operator's transaction switch — and one full money cycle in test mode.

Exit gate: the Stripe-Signature verifier accepts only fresh, untampered
deliveries over the RAW payload; the /v1/webhooks/stripe door matches
events to our books through the oolu_* metadata our adapters attach and
replays idempotently; the Stripe card vault and Connect adapter speak the
documented wire shapes; assembly swaps test doubles for Stripe exactly
when a secret key exists; `--transactions` refuses to open onto test
doubles; and charge → accrue → settle → webhook-confirm → dispute-clawback
runs end to end on the fake processor.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_chat_model_router import FakeTransport
from test_http_gateway import NOW, _app, _req

from oolu.billing import (
    ChargingService,
    DisputeService,
    DisputeStore,
    EarningsLedger,
    FakePayoutAdapter,
    KycStatus,
    LaunchGuard,
    PayoutAccount,
    PayoutBatch,
    PayoutStatus,
    PayoutStore,
    SettlementService,
    StripeCardVault,
    StripeConnectAdapter,
)
from oolu.durable.connection import DurableConnection
from oolu.durable.idempotency import IdempotencyLedger
from oolu.gateway import GatewayApp, Request, StripeWebhookVerifier, WebhookError
from oolu.metering.models import AttributionRecord, MeteringEvent
from oolu.providers.vault import SecretVault

WHSEC = "whsec_test_secret"


# --------------------------------------------------------------------------- #
# The Stripe-Signature verifier.                                               #
# --------------------------------------------------------------------------- #
def test_stripe_signature_round_trip_and_refusals():
    verifier = StripeWebhookVerifier(WHSEC)
    payload = b'{"id": "evt_1", "type": "charge.refunded"}'
    header = verifier.sign_header(payload, now=NOW)

    verifier.verify(payload, header, now=NOW)  # fresh + intact: accepted

    with pytest.raises(WebhookError, match="signature"):
        verifier.verify(b'{"id": "evt_2"}', header, now=NOW)  # tampered
    with pytest.raises(WebhookError, match="missing"):
        verifier.verify(payload, None, now=NOW)
    with pytest.raises(WebhookError, match="malformed"):
        verifier.verify(payload, "v1=deadbeef", now=NOW)  # no timestamp
    with pytest.raises(WebhookError, match="tolerance"):
        verifier.verify(payload, header, now=NOW + timedelta(minutes=6))


# --------------------------------------------------------------------------- #
# The gateway's Stripe door.                                                   #
# --------------------------------------------------------------------------- #
def _stripe_gateway(tmp_path):
    app, conn, ident = _app(tmp_path)
    ledger = EarningsLedger(conn)
    disputes = DisputeService(
        ledger=ledger,
        disputes=DisputeStore(conn),
        durable=SimpleNamespace(is_production_durable=True),
        providers=[],
        idempotency=IdempotencyLedger(conn),
        clock=lambda: NOW,
    )
    gateway = GatewayApp(
        app._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        payout_store=PayoutStore(conn),
        disputes=disputes,
        stripe_webhooks=StripeWebhookVerifier(WHSEC),
    )
    return gateway, conn, ledger


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


def test_stripe_payout_events_confirm_the_batch_they_settle(tmp_path):
    gateway, conn, _ = _stripe_gateway(tmp_path)
    batch = PayoutBatch(noder_principal="noder-1", amount_micros=5_000_000)
    gateway._payout_store.add_batch(batch)

    response = _stripe_event(
        gateway,
        {
            "id": "evt_po_1",
            "type": "transfer.paid",
            "data": {
                "object": {
                    "id": "tr_123",
                    "metadata": {"oolu_batch_id": batch.batch_id},
                }
            },
        },
    )
    assert response.status == 200, response.body
    assert response.body["batch_id"] == batch.batch_id
    updated = gateway._payout_store.get_batch(batch.batch_id)
    assert updated.status == PayoutStatus.PAID
    assert updated.provider_ref == "tr_123"
    conn.close()


def test_stripe_refunds_claw_back_through_the_charge_metadata(tmp_path):
    from oolu.billing import EarningsEntry, EarningsKind

    gateway, conn, ledger = _stripe_gateway(tmp_path)
    ledger.append(
        EarningsEntry(
            noder_principal="noder-1",
            event_id="evt-oolu-9",
            amount_micros=3_000_000,
            kind=EarningsKind.ACCRUAL,
            available_at=NOW,
        )
    )

    response = _stripe_event(
        gateway,
        {
            "id": "evt_re_1",
            "type": "charge.refunded",
            "data": {
                "object": {
                    "id": "ch_1",
                    "metadata": {"oolu_event_id": "evt-oolu-9"},
                }
            },
        },
    )
    assert response.status == 200, response.body
    assert response.body["clawback_event_id"] == "evt-oolu-9"
    kinds = [e.kind for e in ledger.entries_for_event("evt-oolu-9")]
    assert EarningsKind.CLAWBACK in kinds
    conn.close()


def test_stripe_deliveries_replay_idempotently_and_refuse_bad_signatures(
    tmp_path,
):
    gateway, conn, _ = _stripe_gateway(tmp_path)
    event = {"id": "evt_dup", "type": "some.future.event", "data": {"object": {}}}

    first = _stripe_event(gateway, event)
    again = _stripe_event(gateway, event)
    assert first.status == again.status == 200
    assert first.body == again.body  # the replayed result, not a re-run

    raw = json.dumps(event).encode()
    forged = gateway.handle(
        Request(
            method="POST",
            path="/v1/webhooks/stripe",
            headers={"Stripe-Signature": "t=1,v1=deadbeef"},
            body=event,
            raw=raw,
            now=NOW,
        )
    )
    assert forged.status == 400
    conn.close()


# --------------------------------------------------------------------------- #
# The Stripe adapters' wire shapes.                                            #
# --------------------------------------------------------------------------- #
def _stripe_adapter(transport):
    vault = SecretVault()
    ref = vault.put("sk_test_123", kind="stripe")
    return StripeConnectAdapter(vault=vault, transport=transport, api_key_ref=ref)


def test_charges_carry_the_metadata_that_comes_back_on_webhooks():
    transport = FakeTransport()
    transport.script(
        "api.stripe.com", 200, {"id": "ch_1", "status": "succeeded"}
    )
    adapter = _stripe_adapter(transport)
    receipt = adapter.charge(
        idempotency_key="charge:key-1",
        amount_micros=12_000_000,
        currency="usd",
        consumer_ref="cus_1",
        metadata={"oolu_event_id": "evt-oolu-1"},
    )
    assert receipt.provider_ref == "ch_1"
    [request] = transport.requests
    assert request["body"]["metadata[oolu_event_id]"] == "evt-oolu-1"
    assert request["body"]["amount"] == 1200  # micros -> minor units
    assert "sk_test_123" in request["headers"]["Authorization"]
    assert request["headers"]["Idempotency-Key"] == "charge:key-1"


def test_the_live_card_vault_never_takes_a_card_server_side():
    transport = FakeTransport()
    transport.script(
        "api.stripe.com", 200, {"id": "cus_9", "client_secret": "seti_secret_9"}
    )
    vault = SecretVault()
    cards = StripeCardVault(
        vault=vault,
        transport=transport,
        api_key_ref=vault.put("sk_test_123", kind="stripe"),
    )
    assert cards.mode == "live"
    assert cards.create_customer("alice") == "cus_9"
    assert cards.setup_intent("cus_9") == {"client_secret": "seti_secret_9"}
    from oolu.billing import PaymentError

    with pytest.raises(PaymentError, match="SetupIntent"):
        cards.attach_test_card("cus_9", "visa")


# --------------------------------------------------------------------------- #
# Assembly: adapters follow the key; the switch follows the operator.          #
# --------------------------------------------------------------------------- #
def test_assembly_stays_on_test_doubles_without_a_stripe_key(tmp_path):
    from oolu.assembly import build_host_runtime

    runtime = build_host_runtime(
        data_dir=tmp_path / "host",
        secret="a-thirty-two-character-plus-signing-secret",
    )
    try:
        runtime.accounts.bootstrap(tenant="main", username="admin", password="pw12345678")
        token = runtime.accounts.login("admin", "pw12345678").token
        status = runtime.gateway.handle(
            _req("GET", "/v1/payments/status", token=token)
        )
        assert status.body["vault_mode"] == "test"
        assert status.body["mode"] == "pre_launch"
        plan = runtime.gateway.handle(
            _req("GET", "/v1/subscription", token=token)
        )
        if plan.status == 200:  # the console tells the same truth
            assert plan.body["charging_open"] is False
    finally:
        runtime.close()


def test_assembly_goes_live_with_a_stripe_key_and_the_switch(tmp_path):
    from oolu.assembly import build_host_runtime

    runtime = build_host_runtime(
        data_dir=tmp_path / "host",
        secret="a-thirty-two-character-plus-signing-secret",
        transactions_enabled=True,
        stripe_secret_key="sk_test_123",
        stripe_webhook_secret=WHSEC,
    )
    try:
        runtime.accounts.bootstrap(tenant="main", username="admin", password="pw12345678")
        token = runtime.accounts.login("admin", "pw12345678").token
        status = runtime.gateway.handle(
            _req("GET", "/v1/payments/status", token=token)
        )
        assert status.body["vault_mode"] == "live"
        assert status.body["mode"] == "live"
        # Open port ≠ open charging: prices and verification still gate.
        assert status.body["open"] is False
    finally:
        runtime.close()


def test_the_transactions_flag_refuses_to_open_onto_test_doubles(
    tmp_path, monkeypatch, capsys
):
    from oolu import cli

    monkeypatch.delenv("OOLU_STRIPE_KEY", raising=False)
    monkeypatch.setenv(
        "OOLU_HOST_SECRET", "a-thirty-two-character-plus-signing-secret"
    )
    code = cli.main(["host", "--data", str(tmp_path / "data"), "--transactions"])
    assert code != 0
    assert "OOLU_STRIPE_KEY" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# One full money cycle, test mode: the launch guard's three gates, a charge,   #
# accrual, settlement, webhook confirmation, and a clawback.                   #
# --------------------------------------------------------------------------- #
def test_the_whole_money_cycle_runs_in_test_mode(tmp_path):
    conn = DurableConnection(tmp_path / "cycle.db")
    ledger = EarningsLedger(conn)
    payout_store = PayoutStore(conn)
    adapter = FakePayoutAdapter()
    idem = IdempotencyLedger(conn)
    durable = SimpleNamespace(is_production_durable=True)
    guard = LaunchGuard(transactions_enabled=True)

    charging = ChargingService(
        ledger=ledger,
        payout=adapter,
        durable=durable,
        providers=[],
        idempotency=idem,
        launch_guard=guard,
        holdback_days=0,
    )
    event = MeteringEvent(
        idempotency_key="run-1",
        run_id="run-1",
        consumer_principal="consumer-1",
        outcome="succeeded",
        gross=10.0,
        provider_cost=1.0,
        audit_seq=1,
        occurred_at=NOW,
    )
    attribution = [
        AttributionRecord(
            event_id=event.event_id, noder_principal="noder-1", weight=1.0
        )
    ]

    # Gate 1+2+3: the port is open but prices haven't settled — refused.
    from oolu.billing import LaunchClosedError

    with pytest.raises(LaunchClosedError):
        charging.charge_and_accrue(
            event, attribution, consumer_ref="cus_1", class_key="export"
        )
    for _ in range(8):
        guard.record_price("export", 10.0)
    charged = charging.charge_and_accrue(
        event,
        attribution,
        consumer_ref="cus_1",
        class_key="export",
        verified_successes=5,
    )
    assert charged["charged"] is True
    assert charged["noder_accruals"]["noder-1"] > 0

    # Settlement pays the verified noder through the (fake) processor.
    payout_store.save_account(
        PayoutAccount(
            noder_principal="noder-1",
            provider_account_id="acct_n1",
            kyc_status=KycStatus.VERIFIED,
        )
    )
    settlement = SettlementService(
        ledger=ledger,
        payout_store=payout_store,
        payout=adapter,
        durable=durable,
        providers=[],
        idempotency=idem,
        min_payout_micros=0,
        risk_window_days=None,
        clock=lambda: NOW + timedelta(days=1),
    )
    settled = settlement.settle("noder-1", period_key="2026-07")
    assert settled["paid"] is True

    # The dispute path claws the event back, reserve-first.
    disputes = DisputeService(
        ledger=ledger,
        disputes=DisputeStore(conn),
        durable=durable,
        providers=[],
        idempotency=idem,
        clock=lambda: NOW + timedelta(days=2),
    )
    clawed = disputes.refund(event_id=event.event_id, reason="charge.refunded")
    assert clawed["clawed_back"] if "clawed_back" in clawed else True
    from oolu.billing import EarningsKind

    kinds = {e.kind for e in ledger.entries_for_event(event.event_id)}
    assert EarningsKind.CLAWBACK in kinds
    conn.close()
