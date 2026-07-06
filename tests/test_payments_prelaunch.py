"""Card on file + the launch guard: saving cards works, charging waits.

Pre-launch properties under test: cards save and manage through the real
flow (customer ref, pm refs, default selection) but only NAMED TEST cards
exist — no route or class accepts a card number; the fake vault cannot
move money (it has no charge). Real charging is refused until the
transaction port opens AND the class's price settles AND the function has
verified successes — and a violent price tick reopens the wait.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from test_http_gateway import _app, _req

from oolu.billing import (
    FakeCardVault,
    LaunchClosedError,
    LaunchGuard,
    PaymentError,
    PaymentMethodsService,
    PaymentProfileStore,
)
from oolu.durable import DurableConnection
from oolu.gateway import GatewayApp
from oolu.identity import Hs256Signer

_IDP = "idp-secret"
_ISSUER = "https://idp"
_AUDIENCE = "oolu"


def _service(tmp_path):
    conn = DurableConnection(tmp_path / "d.db")
    return (
        PaymentMethodsService(PaymentProfileStore(conn), FakeCardVault()),
        conn,
    )


def _token(subject="user-1", tenant="t1"):
    signer = Hs256Signer(secret=_IDP, issuer=_ISSUER, audience=_AUDIENCE)
    return signer.mint(subject=subject, tenant_id=tenant, now=datetime.now(UTC))


# --------------------------------------------------------------------------- #
# Saving cards — the flow is real, the cards are test cards.                   #
# --------------------------------------------------------------------------- #
def test_add_list_default_remove_lifecycle(tmp_path):
    service, conn = _service(tmp_path)
    try:
        visa = service.add_test_card("alice", "visa")
        master = service.add_test_card("alice", "mastercard")
        profile = service.profile("alice")
        assert profile.customer_ref.startswith("cus_test_")
        assert [c.brand for c in profile.cards] == ["visa", "mastercard"]
        assert profile.default_pm == visa.pm_ref  # first card became default

        assert service.set_default("alice", master.pm_ref) is True
        assert service.profile("alice").default_pm == master.pm_ref

        assert service.remove_card("alice", master.pm_ref) is True
        after = service.profile("alice")
        assert [c.pm_ref for c in after.cards] == [visa.pm_ref]
        assert after.default_pm == visa.pm_ref  # default fell back sanely
    finally:
        conn.close()


def test_prelaunch_vault_takes_no_card_numbers_and_cannot_charge(tmp_path):
    service, conn = _service(tmp_path)
    try:
        with pytest.raises(PaymentError, match="test cards only"):
            service.add_test_card("alice", "4111111111111111")  # a PAN is not a brand
        # The structural guarantee: the pre-launch vault has no charge path.
        assert not hasattr(FakeCardVault(), "charge")
    finally:
        conn.close()


def test_profiles_are_per_principal(tmp_path):
    service, conn = _service(tmp_path)
    try:
        service.add_test_card("alice", "visa")
        assert service.profile("bob").cards == []
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The launch guard: three doors in series.                                     #
# --------------------------------------------------------------------------- #
def test_prelaunch_port_closed_refuses_regardless_of_prices():
    guard = LaunchGuard(transactions_enabled=False, window=3)
    for price in (1.0, 1.0, 1.0):
        guard.record_price("workflow:invoices", price)
    state = guard.status("workflow:invoices", verified_successes=100)
    assert state.open is False and state.mode == "pre_launch"
    assert any("transaction port" in r for r in state.reasons)


def test_volatile_prices_hold_the_gate_until_they_settle():
    guard = LaunchGuard(
        transactions_enabled=True, window=4, min_verified_successes=0
    )
    # An ICO-shaped open: violent swings — not settled.
    for price in (1.0, 3.0, 0.5, 2.5):
        guard.record_price("workflow:invoices", price)
    assert guard.price_settled("workflow:invoices") is False

    # Prices calm into a narrow band: the window fills with quiet ticks.
    for price in (1.00, 1.05, 0.98, 1.02):
        guard.record_price("workflow:invoices", price)
    assert guard.price_settled("workflow:invoices") is True
    assert guard.status("workflow:invoices", verified_successes=0).open is True

    # One violent tick reopens the wait.
    guard.record_price("workflow:invoices", 4.0)
    assert guard.price_settled("workflow:invoices") is False


def test_unproven_functions_hold_the_gate():
    guard = LaunchGuard(
        transactions_enabled=True, window=2, min_verified_successes=5
    )
    guard.record_price("k", 1.0)
    guard.record_price("k", 1.0)
    state = guard.status("k", verified_successes=2)
    assert state.open is False
    assert any("not proven" in r for r in state.reasons)
    with pytest.raises(LaunchClosedError):
        guard.assert_chargeable("k", verified_successes=2)
    assert guard.status("k", verified_successes=5).open is True


# --------------------------------------------------------------------------- #
# The routes.                                                                  #
# --------------------------------------------------------------------------- #
def _payments_app(tmp_path):
    base, conn, ident = _app(tmp_path)
    app = GatewayApp(
        base._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        payments=PaymentMethodsService(PaymentProfileStore(conn), FakeCardVault()),
        launch_guard=LaunchGuard(transactions_enabled=False),
    )
    return app, conn


def test_payment_method_routes_end_to_end(tmp_path):
    app, conn = _payments_app(tmp_path)
    try:
        token = _token()
        added = app.handle(
            _req("POST", "/v1/payment-methods", token=token, body={"brand": "visa"})
        )
        assert added.status == 201
        assert added.body["mode"] == "test"  # honest about pre-launch
        assert added.body["last4"] == "4242"
        pm = added.body["pm_ref"]

        listed = app.handle(_req("GET", "/v1/payment-methods", token=token))
        assert listed.body["mode"] == "test"
        assert listed.body["default_pm"] == pm
        assert listed.body["cards"][0]["brand"] == "visa"

        # Another principal sees an empty wallet.
        other = app.handle(
            _req("GET", "/v1/payment-methods", token=_token(subject="user-2"))
        )
        assert other.body["cards"] == []

        removed = app.handle(
            _req("DELETE", f"/v1/payment-methods/{pm}", token=token)
        )
        assert removed.status == 200
        assert app.handle(_req("GET", "/v1/payment-methods", token=token)).body[
            "cards"
        ] == []
    finally:
        conn.close()


def test_payments_status_spells_out_why_charging_is_closed(tmp_path):
    app, conn = _payments_app(tmp_path)
    try:
        status = app.handle(
            _req(
                "GET",
                "/v1/payments/status",
                token=_token(),
                query={"class_key": "workflow:invoices"},
            )
        )
        assert status.status == 200
        assert status.body["open"] is False
        assert status.body["mode"] == "pre_launch"
        assert status.body["vault_mode"] == "test"
        assert any("transaction port" in r for r in status.body["reasons"])
        assert any("not settled" in r for r in status.body["reasons"])
    finally:
        conn.close()
