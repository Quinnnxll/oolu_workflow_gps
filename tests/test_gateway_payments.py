from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from test_http_gateway import NOW, _app, _req

from oolu.billing import (
    BalanceProjection,
    DisputeService,
    DisputeStore,
    EarningsEntry,
    EarningsKind,
    EarningsLedger,
    FakePayoutAdapter,
    PayoutStore,
)
from oolu.durable.idempotency import IdempotencyLedger
from oolu.durable.postgres import PostgresDurableConnection
from oolu.gateway import GatewayApp, WebhookSigner, WebhookVerifier
from oolu.identity import ProviderConfig

PG_DSN = os.environ.get("OOLU_TEST_PG_DSN") or os.environ.get("DATABASE_URL")
WEBHOOK_SECRET = "whsec_test"


class _AsymmetricVerifier:
    algorithm = "RS256"

    def verify(self, signing_input, signature, header) -> bool:
        return True


_ASYMMETRIC = [
    ProviderConfig(
        issuer="https://idp",
        audiences=frozenset({"oolu"}),
        verifier=_AsymmetricVerifier(),
    )
]


def _build(tmp_path, *, billing_conn=None):
    base, conn, ident = _app(tmp_path)
    bconn = billing_conn or conn
    disputes = DisputeService(
        ledger=EarningsLedger(bconn),
        disputes=DisputeStore(bconn),
        durable=bconn,
        providers=_ASYMMETRIC,
        idempotency=IdempotencyLedger(bconn),
    )
    app = GatewayApp(
        base._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        payout_store=PayoutStore(bconn),
        payout_adapter=FakePayoutAdapter(),
        disputes=disputes,
        webhook_verifier=WebhookVerifier(WEBHOOK_SECRET),
    )
    return app, conn, bconn, ident


def _signed(payload, *, delivery_id):
    return WebhookSigner(WEBHOOK_SECRET).sign(payload, delivery_id=delivery_id, now=NOW)


def test_create_and_get_payout_account(tmp_path):
    app, _, _, ident = _build(tmp_path)
    created = app.handle(
        _req(
            "POST",
            "/v1/payout-accounts",
            token=ident.token("noder-1", "t1"),
            body={"country": "US"},
        )
    )
    assert created.status == 201
    assert created.body["kyc_status"] == "verified"
    fetched = app.handle(
        _req("GET", "/v1/payout-accounts", token=ident.token("noder-1", "t1"))
    )
    assert fetched.status == 200
    assert fetched.body["provider_account_id"] == created.body["provider_account_id"]


def test_get_payout_account_404_when_absent(tmp_path):
    app, _, _, ident = _build(tmp_path)
    resp = app.handle(
        _req("GET", "/v1/payout-accounts", token=ident.token("nobody", "t1"))
    )
    assert resp.status == 404


def test_list_disputes_for_event(tmp_path):
    app, _, _, ident = _build(tmp_path)
    app._disputes.open(event_id="evt-1", reason="chargeback")
    resp = app.handle(
        _req("GET", "/v1/disputes/evt-1", token=ident.token("noder-1", "t1"))
    )
    assert resp.status == 200
    assert len(resp.body["items"]) == 1


def test_webhook_without_signature_is_rejected(tmp_path):
    app, _, _, _ = _build(tmp_path)
    resp = app.handle(
        _req("POST", "/v1/webhooks/processor", body={"type": "payout.paid"})
    )
    assert resp.status == 400


def test_webhook_tampered_payload_is_rejected(tmp_path):
    app, _, _, _ = _build(tmp_path)
    headers = _signed({"type": "payout.paid"}, delivery_id="d1")
    resp = app.handle(
        _req(
            "POST",
            "/v1/webhooks/processor",
            body={"type": "charge.refunded"},
            headers=headers,
        )
    )
    assert resp.status == 400


def test_webhook_replay_is_rejected(tmp_path):
    app, _, _, _ = _build(tmp_path)
    payload = {"type": "payout.paid", "batch_id": "unknown"}
    headers = _signed(payload, delivery_id="d1")
    first = app.handle(
        _req("POST", "/v1/webhooks/processor", body=payload, headers=headers)
    )
    assert first.status == 200
    replay = app.handle(
        _req("POST", "/v1/webhooks/processor", body=payload, headers=headers)
    )
    assert replay.status == 400


def _pg():
    if not PG_DSN:
        pytest.skip("no PostgreSQL DSN configured")
    try:
        conn = PostgresDurableConnection(PG_DSN)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL unavailable: {exc}")
    with conn.transaction() as db:
        db.execute("DROP TABLE IF EXISTS earnings_entries")
        db.execute("DROP TABLE IF EXISTS disputes")
        db.execute("DELETE FROM idempotency")
    return conn


@pytest.mark.needs_postgres
def test_refund_webhook_triggers_clawback(tmp_path):
    conn = _pg()
    try:
        app, _, bconn, _ = _build(tmp_path, billing_conn=conn)
        ledger = EarningsLedger(bconn)
        ledger.append(
            EarningsEntry(
                noder_principal="noder-B",
                event_id="evt-1",
                amount_micros=294000,
                kind=EarningsKind.ACCRUAL,
                available_at=datetime(2020, 1, 1, tzinfo=UTC),
            )
        )
        payload = {"type": "charge.refunded", "event_id": "evt-1"}
        headers = _signed(payload, delivery_id="refund-1")
        resp = app.handle(
            _req("POST", "/v1/webhooks/processor", body=payload, headers=headers)
        )
        assert resp.status == 200
        assert resp.body["clawback_event_id"] == "evt-1"
        assert BalanceProjection(ledger).balance("noder-B").available_micros == 0
    finally:
        conn.close()
