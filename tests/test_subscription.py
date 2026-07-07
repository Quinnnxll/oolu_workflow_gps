"""The subscription lifecycle: a commitment managed in the console, shown
(and only shown) in Settings.

Three walls under test: no caller — UI, API, or the assistant — can write a
managed setting; the lifecycle allows exactly choose-from-free and
cancel-with-credit (change = cancel first); and what Settings displays is
always the lifecycle's truth, mirrored by the owner through its own door.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from test_http_gateway import _app, _req

from oolu.billing import PLAN_PRICES_MICROS, SubscriptionError, SubscriptionService
from oolu.durable.connection import DurableConnection
from oolu.gateway import GatewayApp
from oolu.settings_node import SettingError, SettingsNode, SettingsStore

T0 = datetime(2026, 7, 1, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


@pytest.fixture()
def rig(tmp_path):
    conn = DurableConnection(tmp_path / "durable.db")
    settings = SettingsNode(SettingsStore(conn))
    clock = Clock()
    service = SubscriptionService(conn, settings=settings, clock=clock)
    yield service, settings, clock
    conn.close()


# --------------------------------------------------------------------------- #
# The settings wall.                                                           #
# --------------------------------------------------------------------------- #
def test_no_caller_can_write_a_managed_setting(rig):
    _, settings, _ = rig
    with pytest.raises(SettingError, match="account console"):
        settings.set("t1", "subscription.plan", "pro")
    with pytest.raises(SettingError, match="account console"):
        settings.set_many("t1", {"app.theme": "dark", "subscription.plan": "pro"})
    # The refused batch changed nothing at all (all-or-nothing).
    assert settings.effective("t1")["app.theme"] == "system"
    # And the catalog tells the UI which fields are display-only.
    described = {f["key"]: f for f in settings.describe("t1")}
    assert described["subscription.plan"]["managed"] is True
    assert described["app.theme"]["managed"] is False


def test_the_owner_mirrors_through_its_own_door(rig):
    service, settings, _ = rig
    service.choose("t1", "plus", "monthly")
    assert settings.effective("t1")["subscription.plan"] == "plus"
    assert settings.effective("t1")["subscription.billing_cycle"] == "monthly"


# --------------------------------------------------------------------------- #
# The lifecycle.                                                               #
# --------------------------------------------------------------------------- #
def test_everyone_starts_free(rig):
    service, _, _ = rig
    view = service.view("t1")
    assert view["plan"] == "free"
    assert view["credit_micros"] == 0
    assert view["charging_open"] is False  # pre-launch, said out loud


def test_choose_from_free_and_only_from_free(rig):
    service, _, _ = rig
    started = service.choose("t1", "plus", "monthly")
    assert started["price_micros"] == PLAN_PRICES_MICROS["plus"]["monthly"]
    assert started["charged"] is False  # the port is closed

    # No in-place change of any term: plan, or cycle.
    with pytest.raises(SubscriptionError, match="cancel your current plan"):
        service.choose("t1", "pro", "monthly")
    with pytest.raises(SubscriptionError, match="cancel your current plan"):
        service.choose("t1", "plus", "yearly")


def test_cancel_returns_unused_time_as_credit(rig):
    service, _, clock = rig
    service.choose("t1", "plus", "monthly")
    clock.now = T0 + timedelta(days=15)  # half the 30-day period used

    result = service.cancel("t1")
    half = PLAN_PRICES_MICROS["plus"]["monthly"] // 2
    assert abs(result["credit_micros"] - half) <= 1
    assert service.view("t1")["plan"] == "free"


def test_upgrade_with_deduction_is_cancel_then_choose(rig):
    service, _, clock = rig
    service.choose("t1", "plus", "monthly")
    clock.now = T0 + timedelta(days=15)
    service.cancel("t1")  # ~half the plus price becomes credit

    upgraded = service.choose("t1", "pro", "yearly")
    credit = upgraded["credit_applied_micros"]
    assert credit > 0  # the deduction, spelled out
    assert upgraded["due_micros"] == (
        PLAN_PRICES_MICROS["pro"]["yearly"] - credit
    )
    assert service.view("t1")["credit_micros"] == 0  # fully consumed


def test_junk_moves_are_refused(rig):
    service, _, _ = rig
    with pytest.raises(SubscriptionError):
        service.cancel("t1")  # nothing to cancel on free
    with pytest.raises(SubscriptionError):
        service.choose("t1", "platinum", "monthly")
    with pytest.raises(SubscriptionError):
        service.choose("t1", "plus", "weekly")
    with pytest.raises(SubscriptionError):
        service.choose("t1", "free", "monthly")


def test_tenants_do_not_share_a_plan(rig):
    service, _, _ = rig
    service.choose("t1", "pro", "monthly")
    assert service.view("t2")["plan"] == "free"


# --------------------------------------------------------------------------- #
# Through the gateway.                                                         #
# --------------------------------------------------------------------------- #
def _gateway(tmp_path):
    app, conn, ident = _app(tmp_path)
    extra = DurableConnection(tmp_path / "account.db")
    settings = SettingsNode(SettingsStore(extra))
    gateway = GatewayApp(
        app._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        settings_node=settings,
        subscriptions=SubscriptionService(extra, settings=settings),
    )
    return gateway, ident, (conn, extra)


def test_the_console_routes_run_the_lifecycle(tmp_path):
    gateway, ident, conns = _gateway(tmp_path)
    token = ident.token("user-1")

    view = gateway.handle(_req("GET", "/v1/subscription", token=token))
    assert view.status == 200 and view.body["plan"] == "free"

    chosen = gateway.handle(
        _req(
            "POST",
            "/v1/subscription/choose",
            token=token,
            body={"plan": "plus", "cycle": "monthly"},
        )
    )
    assert chosen.status == 200 and chosen.body["charged"] is False

    blocked = gateway.handle(
        _req(
            "POST",
            "/v1/subscription/choose",
            token=token,
            body={"plan": "pro", "cycle": "yearly"},
        )
    )
    assert blocked.status == 409
    assert "cancel your current plan" in blocked.body["error"]["message"]

    # Settings PUT is refused with directions to the console — for every
    # caller, the assistant included (it writes through this same node).
    refused = gateway.handle(
        _req(
            "PUT",
            "/v1/settings",
            token=token,
            body={"changes": {"subscription.plan": "enterprise"}},
        )
    )
    assert refused.status == 400
    assert "account console" in refused.body["error"]["message"]

    cancelled = gateway.handle(
        _req("POST", "/v1/subscription/cancel", token=token)
    )
    assert cancelled.status == 200 and cancelled.body["cancelled"] == "plus"

    for conn in conns:
        conn.close()


# --------------------------------------------------------------------------- #
# The console page: served, wired to real routes, XSS-safe by construction.    #
# --------------------------------------------------------------------------- #
def test_the_account_console_is_served_and_honest(tmp_path):
    from pathlib import Path

    from test_gateway_asgi import _call

    from oolu.gateway.asgi import GatewayASGI

    app, conn, _ = _app(tmp_path)
    try:
        for frontend in ("host", "shell"):
            status, headers, body = _call(
                GatewayASGI(app, frontend=frontend), "GET", "/account"
            )
            assert status == 200
            assert headers["Content-Type"].startswith("text/html")
            assert b"OoLu account" in body
    finally:
        conn.close()

    html = (
        Path(__file__).resolve().parent.parent
        / "src/oolu/gateway/frontend/account.html"
    ).read_text()
    app_source = (
        Path(__file__).resolve().parent.parent / "src/oolu/gateway/app.py"
    ).read_text()
    for path in (
        "/v1/subscription",
        "/v1/subscription/choose",
        "/v1/subscription/cancel",
        "/v1/auth/login",
    ):
        assert path in html, f"console no longer calls {path}"
        assert f'"{path}"' in app_source, f"{path} is not a registered route"
    # XSS-safe by construction; the token never lands in localStorage.
    assert "innerHTML" not in html
    assert "localStorage" not in html
