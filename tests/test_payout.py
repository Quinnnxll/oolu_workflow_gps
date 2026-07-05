from __future__ import annotations

import os

import httpx
import pytest

from workflow_gps.billing import (
    ChargeStatus,
    FakePayoutAdapter,
    KycStatus,
    PaymentError,
    PayoutAccount,
    PayoutAdapter,
    PayoutBatch,
    PayoutStatus,
    PayoutStore,
    StripeConnectAdapter,
)
from workflow_gps.durable import DurableConnection
from workflow_gps.durable.postgres import PostgresDurableConnection
from workflow_gps.providers.transport import HttpxTransport
from workflow_gps.providers.vault import SecretVault

PG_DSN = os.environ.get("WFGPS_TEST_PG_DSN") or os.environ.get("DATABASE_URL")


# --------------------------------------------------------------------------- #
# Stripe adapter over a mocked transport (no network).                        #
# --------------------------------------------------------------------------- #
def _stripe(handler):
    vault = SecretVault()
    ref = vault.put("sk_test_secret", kind="stripe_key")
    transport = HttpxTransport(
        client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    return StripeConnectAdapter(vault=vault, transport=transport, api_key_ref=ref)


def test_stripe_adapter_satisfies_the_port():
    assert isinstance(_stripe(lambda r: httpx.Response(200, json={})), PayoutAdapter)


def test_charge_sends_bearer_auth_form_and_idempotency_key():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["idem"] = request.headers.get("idempotency-key")
        seen["ctype"] = request.headers.get("content-type")
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"id": "ch_1", "status": "succeeded"})

    receipt = _stripe(handler).charge(
        idempotency_key="run-1",
        amount_micros=500000,
        currency="usd",
        consumer_ref="cus_1",
    )
    assert receipt.provider_ref == "ch_1"
    assert receipt.status == ChargeStatus.SUCCEEDED
    assert seen["auth"] == "Bearer sk_test_secret"
    assert seen["idem"] == "run-1"
    assert "x-www-form-urlencoded" in seen["ctype"]
    assert "amount=50" in seen["body"]  # 500000 micros -> 50 minor units (cents)


def test_payout_maps_transfer_response():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/transfers"
        return httpx.Response(200, json={"id": "tr_1", "status": "paid"})

    receipt = _stripe(handler).payout(
        idempotency_key="b1",
        provider_account_id="acct_1",
        amount_micros=294000,
        currency="usd",
    )
    assert receipt.provider_ref == "tr_1"
    assert receipt.status == PayoutStatus.PAID


def test_account_kyc_requires_charges_and_payouts_enabled():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"id": "acct_1", "charges_enabled": True, "payouts_enabled": True}
        )

    account = _stripe(handler).create_account(
        noder_principal="noder-1", country="US", currency="usd"
    )
    assert account.provider_account_id == "acct_1"
    assert account.kyc_status == KycStatus.VERIFIED


def test_pending_kyc_when_not_fully_enabled():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"id": "acct_2", "charges_enabled": True, "payouts_enabled": False},
        )

    account = _stripe(handler).create_account(
        noder_principal="noder-1", country="US", currency="usd"
    )
    assert account.kyc_status == KycStatus.PENDING


def test_processor_error_is_raised_and_redacts_the_secret():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            402, json={"error": "card_declined", "key": "sk_test_secret"}
        )

    with pytest.raises(PaymentError) as excinfo:
        _stripe(handler).charge(
            idempotency_key="x",
            amount_micros=1000,
            currency="usd",
            consumer_ref="cus_1",
        )
    assert "sk_test_secret" not in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Fake adapter: deterministic, dedups by idempotency key.                     #
# --------------------------------------------------------------------------- #
def test_fake_adapter_dedups_charge_and_payout():
    adapter = FakePayoutAdapter()
    a = adapter.charge(
        idempotency_key="k", amount_micros=500000, currency="usd", consumer_ref="c"
    )
    b = adapter.charge(
        idempotency_key="k", amount_micros=999999, currency="usd", consumer_ref="c"
    )
    assert a == b  # same idempotency key -> identical receipt
    account = adapter.create_account(noder_principal="n", country="US", currency="usd")
    p1 = adapter.payout(
        idempotency_key="p",
        provider_account_id=account.provider_account_id,
        amount_micros=100,
        currency="usd",
    )
    p2 = adapter.payout(
        idempotency_key="p",
        provider_account_id=account.provider_account_id,
        amount_micros=100,
        currency="usd",
    )
    assert p1 == p2
    assert adapter.account_status(account.provider_account_id) == KycStatus.VERIFIED


# --------------------------------------------------------------------------- #
# PayoutStore persistence over both durable backends.                         #
# --------------------------------------------------------------------------- #
def _new_pg() -> PostgresDurableConnection:
    if not PG_DSN:
        pytest.skip("no PostgreSQL DSN configured")
    try:
        conn = PostgresDurableConnection(PG_DSN)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL unavailable: {exc}")
    with conn.transaction() as db:
        db.execute("DROP TABLE IF EXISTS payout_accounts")
        db.execute("DROP TABLE IF EXISTS payout_batches")
    return conn


@pytest.fixture(params=["sqlite", "postgres"])
def conn(request):
    if request.param == "sqlite":
        connection = DurableConnection(":memory:")
    else:
        connection = _new_pg()
    try:
        yield connection
    finally:
        connection.close()


def test_account_upsert_and_kyc_gate(conn):
    store = PayoutStore(conn)
    store.save_account(
        PayoutAccount(
            noder_principal="n",
            provider_account_id="acct_1",
            kyc_status=KycStatus.PENDING,
        )
    )
    assert store.is_verified("n") is False
    store.save_account(
        PayoutAccount(
            noder_principal="n",
            provider_account_id="acct_1",
            kyc_status=KycStatus.VERIFIED,
        )
    )
    assert store.is_verified("n") is True
    assert store.get_account("n").provider_account_id == "acct_1"


def test_batch_persistence_and_status_query(conn):
    store = PayoutStore(conn)
    batch = PayoutBatch(
        noder_principal="n", amount_micros=294000, status=PayoutStatus.PENDING
    )
    store.add_batch(batch)
    store.update_batch(
        batch.model_copy(update={"status": PayoutStatus.PAID, "provider_ref": "tr_1"})
    )
    assert store.get_batch(batch.batch_id).status == PayoutStatus.PAID
    assert [b.batch_id for b in store.batches_by_status(PayoutStatus.PAID)] == [
        batch.batch_id
    ]
    assert store.batches_by_status(PayoutStatus.PENDING) == []
