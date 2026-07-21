"""The operator's two-sided ledger, and the give-back that refills it.

Exit gate: GET /v1/platform/finance shows, per account, the model/API
spend drawn against the platform (with the allowance standing) AND, per
noder, the node-execution revenue — permission-gated like every operator
read, 403 for everyone else. POST /v1/platform/usage/giveback is an
approved, audited platform move that erases booked spend for all or
selected accounts — the experiment-cohort refill — and names the
forgiven amounts on the audit log.
"""

from __future__ import annotations

from test_http_gateway import _app, _req

from oolu.billing import BillingService, EarningsEntry, EarningsLedger
from oolu.billing.model_usage import ModelUsageStore, SubscriptionBrain
from oolu.gateway import GatewayApp
from oolu.identity.models import AuthorityGrant, Role
from oolu.providers.keyring import ModelKeyring


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


def _host(tmp_path):
    app, conn, ident = _app(tmp_path)
    usage = ModelUsageStore(conn)
    keyring = ModelKeyring(conn, key_path=tmp_path / "machine.key")
    plans = {"t1": "plus"}  # t2 stays a free-plan (trial) experiment user
    brain = SubscriptionBrain(
        keyring, usage, plan_for=lambda tenant: plans.get(tenant, "free")
    )
    billing = BillingService(EarningsLedger(conn))
    gateway = GatewayApp(
        app._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        model_usage=usage,
        subscription=brain,
        billing=billing,
    )
    return gateway, conn, ident, usage, billing


def _seed(usage, billing):
    usage.record("t1", source="subscription", cost=1.5, prompt_tokens=100)
    usage.record("t1", source="own-api", cost=0.4)
    usage.record("t2", source="subscription", cost=6.0, completion_tokens=50)
    billing._ledger.append(
        EarningsEntry(noder_principal="noder-1", amount_micros=250_000)
    )


def test_the_monitor_is_walled_and_the_giveback_needs_approval(tmp_path):
    gw, conn, ident, usage, billing = _host(tmp_path)
    try:
        peek = gw.handle(
            _req("GET", "/v1/platform/finance", token=ident.token("user-1", "t1"))
        )
        assert peek.status == 403
        push = gw.handle(
            _req(
                "POST", "/v1/platform/usage/giveback",
                token=ident.token("user-1", "t1"), body={"all": True},
            )
        )
        assert push.status == 403
    finally:
        conn.close()


def test_the_monitor_shows_both_sides_of_the_books(tmp_path):
    gw, conn, ident, usage, billing = _host(tmp_path)
    try:
        _seed(usage, billing)
        _grant_finance(ident, "operator")
        got = gw.handle(
            _req("GET", "/v1/platform/finance", token=ident.token("operator", "t1"))
        )
        assert got.status == 200, got.body

        by_tenant = {a["tenant_id"]: a for a in got.body["accounts"]}
        assert set(by_tenant) == {"t1", "t2"}
        # The whole ledger line: every source, so the operator sees what
        # each account DRAWS — platform allowance and own key alike.
        assert by_tenant["t1"]["all_time"]["cost_usd"] == 1.9
        assert by_tenant["t1"]["all_time"]["prompt_tokens"] == 100
        # The allowance standing rides each account: t1 is a paid plan,
        # t2 a free-plan trial — the experiment cohort marker.
        assert by_tenant["t1"]["subscription"]["trial"] is False
        assert by_tenant["t2"]["subscription"]["trial"] is True
        assert by_tenant["t2"]["subscription"]["spent_usd"] == 6.0

        # The other side: node-execution revenue per noder.
        (noder,) = got.body["noders"]
        assert noder["principal"] == "noder-1"
        assert noder["pending_micros"] + noder["available_micros"] == 250_000
    finally:
        conn.close()


def test_giveback_refills_selected_accounts_and_lands_on_the_audit_log(tmp_path):
    gw, conn, ident, usage, billing = _host(tmp_path)
    try:
        _seed(usage, billing)
        _grant_finance(ident, "operator")
        gave = gw.handle(
            _req(
                "POST", "/v1/platform/usage/giveback",
                token=ident.token("operator", "t1"), body={"tenants": ["t2"]},
            )
        )
        assert gave.status == 200, gave.body
        assert gave.body["given_back_usd"] == {"t2": 6.0}
        # t2's booked subscription spend is gone; t1's books are untouched,
        # and t2's own-api rows would never be the platform's to forgive.
        assert usage.total_cost("t2", source="subscription") == 0.0
        assert usage.total_cost("t1", source="subscription") == 1.5
        assert usage.total_cost("t1", source="own-api") == 0.4
        # The forgiveness is audited by name and amount.
        entries = [
            e
            for e in gw._durable.audit.records()
            if e.event_type == "usage.giveback"
        ]
        assert entries[-1].payload["given_back_usd"] == {"t2": 6.0}
        assert entries[-1].payload["by"] == "operator"
    finally:
        conn.close()


def test_giveback_all_reaches_every_account_with_books(tmp_path):
    gw, conn, ident, usage, billing = _host(tmp_path)
    try:
        _seed(usage, billing)
        _grant_finance(ident, "operator")
        gave = gw.handle(
            _req(
                "POST", "/v1/platform/usage/giveback",
                token=ident.token("operator", "t1"), body={"all": True},
            )
        )
        assert gave.status == 200, gave.body
        assert gave.body["given_back_usd"] == {"t1": 1.5, "t2": 6.0}
        # An empty ask is refused in words, never a silent no-op.
        empty = gw.handle(
            _req(
                "POST", "/v1/platform/usage/giveback",
                token=ident.token("operator", "t1"), body={},
            )
        )
        assert empty.status == 400
    finally:
        conn.close()
