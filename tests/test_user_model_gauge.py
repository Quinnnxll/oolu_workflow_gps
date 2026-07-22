"""Per-user API draw: every account has its own gauge on a shared tenant.

Exit gate: every consultation is booked under WHO drew it (the acting
principal, or the Supernode owner a fleet interact bills) beside the
tenant line the quota reads — so the admin Finance monitor shows an
independent gauge per user even though everyone rides the same platform
API key at the backend. The per-user give-back erases exactly what one
user drew (the shared quota refills by that amount, everyone else's
gauge stands), and each user's own usage view answers with THEIR line.
"""

from __future__ import annotations

from test_http_gateway import _app, _req

from oolu.billing.model_usage import ModelUsageStore, SubscriptionBrain
from oolu.gateway import GatewayApp
from oolu.identity.models import AuthorityGrant, Role
from oolu.providers.chatmodel import ChatModelRouter
from oolu.providers.keyring import ModelKeyring


class _Call:
    """One consultation's telemetry, as the router books it."""

    cost = 0.5
    prompt_tokens = 100
    completion_tokens = 20


def _grant_finance(ident, principal, tenant="t1"):
    ident.store.add_role(
        Role(
            tenant_id=tenant,
            name="finance",
            permissions=frozenset({"finance:view", "approve:usage.giveback"}),
        )
    )
    ident.store.add_grant(
        AuthorityGrant(
            tenant_id=tenant,
            principal_id=principal,
            role_name="finance",
            granted_by="root",
        )
    )


# --------------------------------------------------------------------- #
# The books: the same call lands on the tenant line AND the user's own. #
# --------------------------------------------------------------------- #
def test_the_books_record_who_drew(tmp_path):
    from oolu.durable.connection import DurableConnection

    conn = DurableConnection(tmp_path / "u.db")
    try:
        usage = ModelUsageStore(conn)
        usage.record(
            "main", source="subscription", cost=1.0,
            prompt_tokens=100, account="alice",
        )
        usage.record(
            "main", source="subscription", cost=3.0,
            completion_tokens=50, account="bob",
        )
        usage.record("main", source="subscription", cost=0.5)  # legacy: no actor
        # The tenant line carries everything — the quota's basis.
        assert usage.month_cost("main") == 4.5
        # And each user has an INDEPENDENT gauge beside it.
        gauges = {u["account"]: u for u in usage.users("main")}
        assert gauges["alice"]["cost_usd"] == 1.0
        assert gauges["alice"]["prompt_tokens"] == 100
        assert gauges["bob"]["cost_usd"] == 3.0
        assert usage.user_all_time("main", "alice")["calls"] == 1
        # A user who never drew answers zero, not an invention.
        assert usage.user_all_time("main", "nobody")["cost_usd"] == 0.0
    finally:
        conn.close()


def test_reset_user_refills_exactly_what_one_user_drew(tmp_path):
    from oolu.durable.connection import DurableConnection

    conn = DurableConnection(tmp_path / "u.db")
    try:
        usage = ModelUsageStore(conn)
        usage.record("main", source="subscription", cost=2.0, account="alice")
        usage.record("main", source="subscription", cost=3.0, account="bob")
        forgiven = usage.reset_user("main", "alice")
        assert forgiven == 2.0
        # Alice's gauge is clean; Bob's stands; the shared quota refilled
        # by exactly what Alice drew.
        assert usage.user_all_time("main", "alice")["cost_usd"] == 0.0
        assert usage.user_all_time("main", "bob")["cost_usd"] == 3.0
        assert usage.month_cost("main") == 3.0
        # A whole-tenant reset clears the user lines with it.
        usage.reset("main")
        assert usage.users("main") == []
        assert usage.month_cost("main") == 0.0
    finally:
        conn.close()


# --------------------------------------------------------------------- #
# The router names the actor; the gateway seat helper sets it.           #
# --------------------------------------------------------------------- #
def test_the_router_books_under_the_acting_user(tmp_path):
    from oolu.durable.connection import DurableConnection

    conn = DurableConnection(tmp_path / "u.db")
    try:
        usage = ModelUsageStore(conn)
        keyring = ModelKeyring(conn, key_path=tmp_path / "machine.key")
        brain = SubscriptionBrain(keyring, usage, plan_for=lambda t: "plus")
        router = ChatModelRouter(keyring, "main", subscription=brain)
        router.act_as("alice")
        router._book_usage(_Call())
        router.act_as("bob")
        router._book_usage(_Call())
        gauges = {u["account"]: u for u in usage.users("main")}
        assert gauges["alice"]["cost_usd"] == 0.5
        assert gauges["bob"]["cost_usd"] == 0.5
        assert usage.month_cost("main") == 1.0
    finally:
        conn.close()


def test_the_seat_helper_tolerates_stub_brains():
    class Stub:  # a test double without the actor channel
        pass

    class Real:
        actor = None

        def act_as(self, principal):
            self.actor = principal

    real = Real()
    assert GatewayApp._seat_actor(real, "alice") is real
    assert real.actor == "alice"
    stub = Stub()
    assert GatewayApp._seat_actor(stub, "alice") is stub  # no crash
    assert GatewayApp._seat_actor(None, "alice") is None


# --------------------------------------------------------------------- #
# The monitor and the doors.                                            #
# --------------------------------------------------------------------- #
def _host(tmp_path):
    app, conn, ident = _app(tmp_path)
    usage = ModelUsageStore(conn)
    keyring = ModelKeyring(conn, key_path=tmp_path / "machine.key")
    brain = SubscriptionBrain(keyring, usage, plan_for=lambda t: "plus")
    gateway = GatewayApp(
        app._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        model_usage=usage,
        subscription=brain,
    )
    return gateway, conn, ident, usage


def test_finance_shows_independent_gauges_and_user_giveback(tmp_path):
    gateway, conn, ident, usage = _host(tmp_path)
    try:
        usage.record("t1", source="subscription", cost=2.0, account="alice")
        usage.record("t1", source="subscription", cost=3.0, account="bob")
        _grant_finance(ident, "admin-1")
        shown = gateway.handle(
            _req(
                "GET", "/v1/platform/finance",
                token=ident.token("admin-1", "t1"),
            )
        )
        assert shown.status == 200
        (account,) = shown.body["accounts"]
        gauges = {u["account"]: u["cost_usd"] for u in account["users"]}
        assert gauges == {"alice": 2.0, "bob": 3.0}

        # The per-user give-back: Alice refills, Bob stands.
        given = gateway.handle(
            _req(
                "POST", "/v1/platform/usage/giveback",
                token=ident.token("admin-1", "t1"),
                body={"users": [{"tenant": "t1", "account": "alice"}]},
            )
        )
        assert given.status == 200
        assert given.body["given_back_usd"] == {"t1:alice": 2.0}
        assert usage.user_all_time("t1", "bob")["cost_usd"] == 3.0
        assert usage.month_cost("t1") == 3.0
    finally:
        conn.close()


def test_my_own_gauge_answers_on_the_usage_view(tmp_path):
    gateway, conn, ident, usage = _host(tmp_path)
    try:
        usage.record("t1", source="subscription", cost=1.25, account="user-1")
        usage.record("t1", source="subscription", cost=9.0, account="other")
        mine = gateway.handle(
            _req(
                "GET", "/v1/usage/model", token=ident.token("user-1", "t1")
            )
        )
        assert mine.status == 200
        # MY line, not the tenant's collapsed total.
        assert mine.body["mine"]["cost_usd"] == 1.25
        assert mine.body["mine"]["account"] == "user-1"
    finally:
        conn.close()
