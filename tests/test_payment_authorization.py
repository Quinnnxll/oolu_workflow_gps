"""OoLu can spend money — but only through the consent + 2FA gate.

Exit gate (Issue 6): TOTP is a real RFC-6238 second factor (an app's code
verifies, drift and all); a payment authorization releases ONLY when the
account re-confirms the exact amount AND presents a valid code; either
lock failing leaves the order pending and unspent; an unenrolled account
cannot authorize at all; and the whole flow rides the gateway routes with
the account, never the tenant, as the scope.
"""

from __future__ import annotations

import pytest
from test_http_gateway import NOW as REQ_NOW
from test_http_gateway import _app, _req

from oolu.billing import (
    OrderRequest,
    PaymentAuthorizationError,
    PaymentAuthorizationStore,
)
from oolu.durable.connection import DurableConnection
from oolu.gateway import GatewayApp
from oolu.identity import TotpStore, totp

NOW = 1_700_000_000.0
# The gateway drives TOTP at the request's clock; codes there use this.
_GW_NOW = REQ_NOW.timestamp()


# --------------------------------------------------------------------------- #
# The TOTP primitive.                                                          #
# --------------------------------------------------------------------------- #
def test_totp_verifies_the_apps_code_with_drift_and_rejects_junk():
    secret = totp.generate_secret()
    assert totp.verify(secret, totp.code_at_time(secret, now=NOW), now=NOW)
    # One step of clock skew each way is tolerated…
    assert totp.verify(secret, totp.code_at_time(secret, now=NOW - 30), now=NOW)
    # …but a stale or wrong code is not.
    assert not totp.verify(secret, totp.code_at_time(secret, now=NOW - 300), now=NOW)
    assert not totp.verify(secret, "000000", now=NOW)
    assert not totp.verify(secret, "abc", now=NOW)
    uri = totp.provisioning_uri(secret, account="alice")
    # The label's colon is percent-encoded, per the otpauth spec.
    assert uri.startswith("otpauth://totp/OoLu%3Aalice?") and secret in uri


def test_the_totp_store_enrolls_confirms_and_seals(tmp_path):
    conn = DurableConnection(tmp_path / "d.db")
    store = TotpStore(conn, key_path=tmp_path / "machine.key")
    assert store.is_enrolled("alice") is False
    # A code can't verify before enrollment — no door was set up.
    assert store.verify("alice", "000000", now=NOW) is False

    begun = store.begin_enroll("alice")
    secret = begun["secret"]
    # Provisional until confirmed: verify() refuses an unconfirmed secret.
    assert store.verify("alice", totp.code_at_time(secret, now=NOW), now=NOW) is False
    assert store.confirm_enroll("alice", "000000", now=NOW) is False
    assert store.confirm_enroll(
        "alice", totp.code_at_time(secret, now=NOW), now=NOW
    )
    assert store.is_enrolled("alice") is True
    assert store.verify("alice", totp.code_at_time(secret, now=NOW), now=NOW)

    # The secret is sealed, not stored in the clear.
    row = conn.db.execute(
        "SELECT ciphertext FROM totp_secrets WHERE principal = 'alice'"
    ).fetchone()
    assert secret not in row["ciphertext"]
    conn.close()


# --------------------------------------------------------------------------- #
# The consent gate: exact amount + a valid code, or nothing moves.            #
# --------------------------------------------------------------------------- #
def _gate(tmp_path):
    conn = DurableConnection(tmp_path / "d.db")
    totp_store = TotpStore(conn, key_path=tmp_path / "machine.key")
    enrolled = {"alice": False}

    def enroll(principal):
        begun = totp_store.begin_enroll(principal)
        totp_store.confirm_enroll(
            principal, totp.code_at_time(begun["secret"], now=NOW), now=NOW
        )
        enrolled[principal] = begun["secret"]

    store = PaymentAuthorizationStore(
        conn,
        verify_second_factor=lambda scope, code: totp_store.verify(
            scope, code, now=NOW
        ),
        second_factor_enrolled=totp_store.is_enrolled,
    )
    return conn, store, totp_store, enroll, enrolled


_ORDER = OrderRequest(
    merchant="Kifaru Books",
    amount_micros=24_990_000,
    currency="USD",
    description="1× 'Deep Learning' hardcover, ship to home",
)


def test_an_order_releases_only_on_exact_amount_and_a_valid_code(tmp_path):
    conn, store, _totp, enroll, secrets = _gate(tmp_path)
    auth = store.request("alice", _ORDER)
    assert auth.status == "pending"

    # No 2FA yet: authorizing is refused before anything else.
    with pytest.raises(PaymentAuthorizationError, match="two-factor"):
        store.authorize(
            "alice", auth.auth_id, confirm_amount_micros=24_990_000, code="000000"
        )
    enroll("alice")
    good = totp.code_at_time(secrets["alice"], now=NOW)

    # A wrong amount is refused even with a good code (the amount lock).
    with pytest.raises(PaymentAuthorizationError, match="amount you confirmed"):
        store.authorize(
            "alice", auth.auth_id, confirm_amount_micros=2_499_000, code=good
        )
    # A wrong code is refused even with the right amount (the 2FA lock).
    with pytest.raises(PaymentAuthorizationError, match="code is wrong"):
        store.authorize(
            "alice", auth.auth_id, confirm_amount_micros=24_990_000, code="000000"
        )
    # Still pending, unspent, after both refusals.
    assert store.get(auth.auth_id).status == "pending"
    assert store.is_authorized(auth.auth_id) is False

    # Both locks satisfied: released.
    released = store.authorize(
        "alice", auth.auth_id, confirm_amount_micros=24_990_000, code=good
    )
    assert released.status == "authorized"
    assert store.is_authorized(auth.auth_id) is True
    # Spent once: a second release is refused.
    with pytest.raises(PaymentAuthorizationError, match="already authorized"):
        store.authorize(
            "alice", auth.auth_id, confirm_amount_micros=24_990_000, code=good
        )
    conn.close()


def test_an_order_belongs_to_its_account_and_can_be_cancelled(tmp_path):
    conn, store, _totp, enroll, _secrets = _gate(tmp_path)
    auth = store.request("alice", _ORDER)
    # Another account cannot authorize or even see it.
    with pytest.raises(PaymentAuthorizationError, match="no such order"):
        store.authorize(
            "bob", auth.auth_id, confirm_amount_micros=24_990_000, code="000000"
        )
    assert store.pending("bob") == []
    # Cancelling pins it: it can never be authorized after.
    store.cancel("alice", auth.auth_id)
    enroll("alice")
    with pytest.raises(PaymentAuthorizationError, match="already cancelled"):
        store.authorize(
            "alice", auth.auth_id, confirm_amount_micros=24_990_000, code="123456"
        )
    conn.close()


# --------------------------------------------------------------------------- #
# The gateway flow, end to end.                                               #
# --------------------------------------------------------------------------- #
def _host(tmp_path):
    app, conn, ident = _app(tmp_path)
    totp_store = TotpStore(conn, key_path=tmp_path / "machine.key")
    payment = PaymentAuthorizationStore(
        conn,
        verify_second_factor=lambda scope, code: totp_store.verify(
            scope.split(":", 1)[-1], code, now=_GW_NOW
        ),
        second_factor_enrolled=lambda scope: totp_store.is_enrolled(
            scope.split(":", 1)[-1]
        ),
    )
    gateway = GatewayApp(
        app._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        totp=totp_store,
        payment_authorizations=payment,
    )
    return gateway, conn, ident, totp_store


def test_the_gateway_gates_an_order_behind_consent_and_2fa(tmp_path):
    gateway, conn, ident, _totp = _host(tmp_path)
    alice = ident.token("alice", "t1")

    # OoLu proposes an order; it lands pending in the account's list.
    proposed = gateway.handle(
        _req("POST", "/v1/payment-authorizations", token=alice,
             body={"merchant": "Kifaru Books", "amount_micros": 24_990_000,
                   "currency": "USD", "description": "1 book"})
    )
    assert proposed.status == 201
    auth_id = proposed.body["auth_id"]
    assert gateway.handle(
        _req("GET", "/v1/payment-authorizations", token=alice)
    ).body["items"][0]["auth_id"] == auth_id

    # Authorizing before 2FA exists is refused.
    early = gateway.handle(
        _req("POST", f"/v1/payment-authorizations/{auth_id}", token=alice,
             body={"confirm_amount_micros": 24_990_000, "code": "000000"})
    )
    assert early.status == 400 and "two-factor" in early.body["error"]["message"]

    # Enroll 2FA through the routes (confirm proves the app works).
    assert gateway.handle(
        _req("GET", "/v1/2fa", token=alice)
    ).body["enrolled"] is False
    secret = gateway.handle(
        _req("POST", "/v1/2fa/enroll", token=alice)
    ).body["secret"]
    assert gateway.handle(
        _req("POST", "/v1/2fa/confirm", token=alice, body={"code": "000000"})
    ).status == 400
    code = totp.code_at_time(secret, now=_GW_NOW)
    assert gateway.handle(
        _req("POST", "/v1/2fa/confirm", token=alice, body={"code": code})
    ).body["enrolled"] is True

    # Wrong amount → refused, order still pending.
    wrong = gateway.handle(
        _req("POST", f"/v1/payment-authorizations/{auth_id}", token=alice,
             body={"confirm_amount_micros": 2_499_000, "code": code})
    )
    assert wrong.status == 400 and "amount" in wrong.body["error"]["message"]

    # Exact amount + valid code → authorized, and it leaves the pending list.
    ok = gateway.handle(
        _req("POST", f"/v1/payment-authorizations/{auth_id}", token=alice,
             body={"confirm_amount_micros": 24_990_000, "code": code})
    )
    assert ok.status == 200 and ok.body["status"] == "authorized"
    assert gateway.handle(
        _req("GET", "/v1/payment-authorizations", token=alice)
    ).body["items"] == []

    # Another account on the same host can neither see nor spend it.
    bob = ident.token("bob", "t1")
    assert gateway.handle(
        _req("POST", f"/v1/payment-authorizations/{auth_id}", token=bob,
             body={"action": "cancel"})
    ).status == 404
    conn.close()


def test_no_totp_service_is_a_plain_404(tmp_path):
    app, conn, ident = _app(tmp_path)
    token = ident.token("user-1")
    assert app.handle(_req("GET", "/v1/2fa", token=token)).status == 404
    assert app.handle(
        _req("GET", "/v1/payment-authorizations", token=token)
    ).status == 404
    conn.close()
