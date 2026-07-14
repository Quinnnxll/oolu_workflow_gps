"""The mint-and-attach seam: a released authorization reaches the order action.

Before this, the gateway could mint and release an authorization and the
executor could verify one, but nothing connected them — an order action never
carried an ``authorization_id``, so it was always BLOCKED. These tests drive
the TRUE end-to-end chain against the REAL ``PaymentAuthorizationStore``
(not a hand-supplied id, not a lambda over a set): declare an order intent →
the resolver files the consent request → authorize it with a real TOTP code →
the same action now executes.
"""

from __future__ import annotations

import pytest

from oolu.billing import (
    PaymentAuthorizationResolver,
    PaymentAuthorizationStore,
)
from oolu.durable.connection import DurableConnection
from oolu.identity import TotpStore, totp
from oolu.skills.commerce import AmazonExecutor
from oolu.skills.models import ActionEvent, ExecutionStatus

SCOPE = "main:alice"
NOW = 1_700_000_000.0


@pytest.fixture
def wired(tmp_path):
    """A real store + TOTP, an enrolled account, and the resolver-backed
    Amazon executor — the whole seam, no mocks."""
    conn = DurableConnection(tmp_path / "d.db")
    totp_store = TotpStore(conn, key_path=tmp_path / "machine.key")
    principal = SCOPE.split(":", 1)[-1]
    begun = totp_store.begin_enroll(principal)
    secret = begun["secret"]  # the account's authenticator seed
    totp_store.confirm_enroll(principal, totp.code_at_time(secret, now=NOW), now=NOW)
    store = PaymentAuthorizationStore(
        conn,
        verify_second_factor=lambda scope, code: totp_store.verify(
            scope.split(":", 1)[-1], code, now=NOW
        ),
        second_factor_enrolled=lambda scope: totp_store.is_enrolled(
            scope.split(":", 1)[-1]
        ),
    )
    resolver = PaymentAuthorizationResolver(store)

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
        orders_enabled=lambda: True,  # operator switch on for the test
        resolve_authorization=resolver.resolve,
    )
    try:
        yield store, executor, client, secret
    finally:
        conn.close()


def _order_action(**extra):
    params = {
        "authorization_scope": SCOPE,
        "run_id": "run-1",
        "merchant": "Amazon",
        "amount_micros": 42_000_000,
        "currency": "USD",
        "description": "one widget",
        **extra,
    }
    return ActionEvent(
        correlation_id="c", adapter="amazon", operation="order", parameters=params
    )


def test_the_first_attempt_files_a_pending_request_and_blocks(wired):
    store, executor, client, _ = wired
    outcome = executor.execute(_order_action(), idempotency_key="k1")
    assert outcome.status is ExecutionStatus.BLOCKED
    assert client.orders == 0
    # The user now has exactly one order to consent to — the seam filed it.
    pending = store.pending(SCOPE)
    assert len(pending) == 1
    assert pending[0].merchant == "Amazon"
    assert pending[0].amount_micros == 42_000_000
    assert pending[0].run_id == "run-1"


def test_re_running_before_consent_does_not_file_a_duplicate(wired):
    store, executor, _, _ = wired
    executor.execute(_order_action(), idempotency_key="k1")
    executor.execute(_order_action(), idempotency_key="k2")
    assert len(store.pending(SCOPE)) == 1  # found the pending one, didn't refile


def test_once_authorized_the_same_action_executes(wired):
    store, executor, client, secret = wired
    # First attempt files the request and blocks, unspent.
    executor.execute(_order_action(), idempotency_key="k1")
    auth_id = store.pending(SCOPE)[0].auth_id
    # The user consents: exact amount to the cent + a fresh TOTP code.
    store.authorize(
        SCOPE, auth_id, confirm_amount_micros=42_000_000, code=totp.code_at_time(secret, now=NOW)
    )
    # The very same action now resolves the released auth and places the order.
    outcome = executor.execute(_order_action(), idempotency_key="k2")
    assert outcome.status is ExecutionStatus.SUCCEEDED
    assert client.orders == 1


def test_a_different_amount_is_a_different_order_still_blocked(wired):
    store, executor, client, secret = wired
    executor.execute(_order_action(), idempotency_key="k1")
    auth_id = store.pending(SCOPE)[0].auth_id
    store.authorize(
        SCOPE, auth_id, confirm_amount_micros=42_000_000, code=totp.code_at_time(secret, now=NOW)
    )
    # A run that quietly grew to a bigger amount does NOT ride the old consent:
    # it is a different order, so it files its own request and blocks.
    outcome = executor.execute(
        _order_action(amount_micros=999_000_000), idempotency_key="k3"
    )
    assert outcome.status is ExecutionStatus.BLOCKED
    assert client.orders == 0
    assert len(store.pending(SCOPE)) == 1  # the new, larger order awaits consent


def test_an_action_without_an_intent_stays_blocked_and_files_nothing(wired):
    store, executor, client, _ = wired
    bare = ActionEvent(
        correlation_id="c", adapter="amazon", operation="order", parameters={}
    )
    outcome = executor.execute(bare, idempotency_key="k1")
    assert outcome.status is ExecutionStatus.BLOCKED
    assert store.pending(SCOPE) == []  # nothing to reconcile, nothing filed
    assert client.orders == 0
