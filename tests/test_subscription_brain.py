"""The hosted subscription brain: platform keys, per-tenant books, plan quotas.

``model.source = "subscription"`` stops being an honest dead end the moment
the host's operator configures platform keys. Exit gate: tenants on a paid
plan are answered through the PLATFORM's keys (Claude first, the plan's
order); every consultation lands in the tenant's durable monthly books; the
free plan is refused with the ways out named; a spent allowance refuses
with the renewal spelled out; own-api tenants are untouched; and the usage
surface shows a tenant their books and their remaining allowance.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from test_chat_model_router import FakeTransport, _anthropic_reply, _openai_reply
from test_http_gateway import _app, _req

from oolu.billing import (
    PLAN_MODEL_ALLOWANCE_USD,
    PLATFORM_TENANT,
    ModelCallMeter,
    ModelUsageStore,
    SubscriptionBrain,
)
from oolu.chat import ModelBudgetExceeded, ModelUnavailable
from oolu.durable.connection import DurableConnection
from oolu.providers.chatmodel import ChatModelRouter
from oolu.providers.keyring import ModelKeyring


def _brain(tmp_path, *, plans=None, platform_keys=("anthropic",)):
    conn = DurableConnection(tmp_path / "brain.db")
    keyring = ModelKeyring(conn, key_path=tmp_path / "machine.key")
    for provider in platform_keys:
        keyring.store(PLATFORM_TENANT, provider, f"platform-{provider}-key")
    usage = ModelUsageStore(conn)
    plans = plans if plans is not None else {"t1": "plus"}
    brain = SubscriptionBrain(
        keyring, usage, plan_for=lambda tenant: plans.get(tenant, "free")
    )
    return conn, keyring, usage, brain, plans


def _router(keyring, brain, tenant="t1", **overrides):
    transport = overrides.pop("transport", FakeTransport())
    return (
        ChatModelRouter(
            keyring,
            tenant,
            transport=transport,
            meter=ModelCallMeter(),
            subscription=brain,
            source=overrides.pop("source", lambda: "subscription"),
            **overrides,
        ),
        transport,
    )


def test_paid_plans_are_answered_through_the_platform_key(tmp_path):
    conn, keyring, usage, brain, _ = _brain(tmp_path)
    router, transport = _router(keyring, brain)
    transport.script("anthropic.com", 200, _anthropic_reply("The plan's brain."))

    assert router.reply([{"role": "user", "content": "hi"}]) == "The plan's brain."
    # The platform's key went out — the tenant never pasted one.
    auth = transport.requests[0]["headers"].get("x-api-key") or transport.requests[
        0
    ]["headers"].get("Authorization", "")
    assert "platform-anthropic-key" in str(auth)
    # And the consultation is in the tenant's durable books.
    assert usage.month_cost("t1", source="subscription") > 0
    conn.close()


def test_the_free_plan_names_its_three_doors(tmp_path):
    conn, keyring, _, brain, plans = _brain(tmp_path)
    plans["t1"] = "free"
    router, _ = _router(keyring, brain)
    with pytest.raises(ModelUnavailable, match="paid plan"):
        router.reply([{"role": "user", "content": "hi"}])
    conn.close()


def test_a_spent_allowance_refuses_and_says_when_it_renews(tmp_path):
    conn, keyring, usage, brain, _ = _brain(tmp_path)
    # Burn the whole plus allowance this month.
    usage.record(
        "t1", source="subscription", cost=PLAN_MODEL_ALLOWANCE_USD["plus"] + 0.01
    )
    router, transport = _router(keyring, brain)
    transport.script("anthropic.com", 200, _anthropic_reply("never reached"))
    with pytest.raises(ModelBudgetExceeded, match="renews"):
        router.reply([{"role": "user", "content": "hi"}])
    assert transport.requests == []  # no provider was called past the wall
    conn.close()


def test_claude_first_with_openai_as_the_fallback(tmp_path):
    conn, keyring, _, brain, _ = _brain(
        tmp_path, platform_keys=("anthropic", "openai")
    )
    router, transport = _router(keyring, brain)
    transport.script("anthropic.com", 500, {"error": "down"})
    transport.script("openai.com", 200, _openai_reply("Backup answered."))
    assert router.reply([{"role": "user", "content": "hi"}]) == "Backup answered."
    conn.close()


def test_own_api_tenants_never_touch_the_platform_key(tmp_path):
    conn, keyring, usage, brain, _ = _brain(tmp_path)
    keyring.store("t1", "openai", "tenants-own-key")
    router, transport = _router(
        keyring,
        brain,
        source=lambda: "own-api",
        preference=lambda: "openai",
    )
    transport.script("openai.com", 200, _openai_reply("My own key."))
    assert router.reply([{"role": "user", "content": "hi"}]) == "My own key."
    assert "tenants-own-key" in str(transport.requests[0]["headers"])
    # Booked under own-api: the subscription quota is untouched.
    assert usage.month_cost("t1", source="subscription") == 0
    assert usage.month_cost("t1", source="own-api") > 0
    conn.close()


def test_without_platform_keys_the_honest_message_stays(tmp_path):
    conn, keyring, _, brain, _ = _brain(tmp_path, platform_keys=())
    router, _ = _router(keyring, brain)
    with pytest.raises(ModelUnavailable, match="isn't live yet"):
        router.reply([{"role": "user", "content": "hi"}])
    conn.close()


def test_usage_books_are_monthly_and_per_tenant(tmp_path):
    clock = {"now": datetime(2026, 7, 10, tzinfo=UTC)}
    conn = DurableConnection(tmp_path / "usage.db")
    usage = ModelUsageStore(conn, clock=lambda: clock["now"])
    usage.record("t1", source="subscription", cost=1.5, prompt_tokens=100)
    usage.record("t1", source="subscription", cost=0.5, completion_tokens=50)
    usage.record("t2", source="subscription", cost=9.0)

    assert usage.month_cost("t1", source="subscription") == 2.0
    assert usage.month_cost("t2", source="subscription") == 9.0
    [row] = usage.view("t1")
    assert row["calls"] == 2
    assert row["prompt_tokens"] == 100 and row["completion_tokens"] == 50

    # A new month opens fresh books; the quota renews by itself.
    clock["now"] = datetime(2026, 8, 1, tzinfo=UTC)
    assert usage.month_cost("t1", source="subscription") == 0.0
    conn.close()


# --------------------------------------------------------------------------- #
# The gateway surfaces.                                                        #
# --------------------------------------------------------------------------- #
def _gateway(tmp_path):
    app, conn, ident = _app(tmp_path)
    keyring = ModelKeyring(conn, key_path=tmp_path / "machine.key")
    keyring.store(PLATFORM_TENANT, "anthropic", "platform-anthropic-key")
    usage = ModelUsageStore(conn)
    brain = SubscriptionBrain(keyring, usage, plan_for=lambda tenant: "plus")
    transport = FakeTransport()
    app._model_keys = keyring
    app._model_meter = ModelCallMeter()
    app._model_usage = usage
    app._subscription = brain
    app._model_transport = transport
    return app, conn, ident, usage, transport


def test_the_hosted_brain_serves_chat_without_a_pasted_key(tmp_path):
    app, conn, ident, usage, transport = _gateway(tmp_path)
    transport.script("anthropic.com", 200, _anthropic_reply("Hosted and here!"))

    router = app._tenant_model("t1")
    assert router is not None  # no tenant key, yet a brain exists
    assert router.reply([{"role": "user", "content": "hello"}]) == "Hosted and here!"
    conn.close()


def test_the_usage_surface_shows_books_and_remaining_allowance(tmp_path):
    app, conn, ident, usage, _ = _gateway(tmp_path)
    usage.record("t1", source="subscription", cost=1.25, prompt_tokens=10)

    view = app.handle(_req("GET", "/v1/usage/model", token=ident.token("u1", "t1")))
    assert view.status == 200, view.body
    [row] = view.body["items"]
    assert row["source"] == "subscription" and row["cost_usd"] == 1.25
    sub = view.body["subscription"]
    assert sub["allowance_usd"] == PLAN_MODEL_ALLOWANCE_USD["plus"]
    assert sub["remaining_usd"] == PLAN_MODEL_ALLOWANCE_USD["plus"] - 1.25
    conn.close()
