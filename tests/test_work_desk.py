"""The Work environment's backend: node accounts and the operator desk.

A node's account answers WHO is responsible, under WHAT authority (1-5),
in WHICH state (live / needs verification / error), and whether it is an
audit node — one that never runs unattended. The desk composes the Work
list (accounts + cumulative earnings + platform-verified health) and each
node's execution feed, and audit-mode nodes force contract runs into the
manual-commit hold queue.
"""

from __future__ import annotations

import pytest
from test_contract_run import _assembled_contract, _build, _CliExecutor, _seed_chain
from test_http_gateway import _req

from oolu.billing import BillingService, EarningsLedger
from oolu.metering.deriver import MeteringDeriver
from oolu.nodeplace import (
    NodeAccount,
    NodeAccountStore,
    NodeStatus,
    OwnershipError,
    WorkDesk,
)


def _desk_build(tmp_path, **kwargs):
    app, conn, ident, registry, metering, attribution, audit = _build(
        tmp_path, **kwargs
    )
    billing = BillingService(EarningsLedger(conn))
    desk = WorkDesk(
        registry=registry,
        accounts=NodeAccountStore(conn),
        billing=billing,
        metering=metering,
        stats=app._market._stats if hasattr(app._market, "_stats") else None,
        attribution=attribution,
        audit=audit,
    )
    app._desk = desk
    return app, conn, ident, registry, metering, attribution, audit, desk, billing


def _run_paid_contract(app, conn, ident, registry, metering, attribution, audit):
    """The whole consumer journey once: seed, assemble, run, derive, accrue.

    Returns the two seeded version ids and the run id.
    """
    exporter, cleaner = _seed_chain(app, ident, registry)
    contract = _assembled_contract(app, ident)
    resp = app.handle(
        _req(
            "POST",
            "/v1/runs/contract",
            token=ident.token("consumer", "t2"),
            body={"contract": contract},
        )
    )
    assert resp.status == 200, resp.body
    billing = BillingService(EarningsLedger(conn))
    for event in MeteringDeriver(audit, metering, attribution).derive():
        billing.accrue(event, attribution.attributions(event.event_id))
    return exporter, cleaner, resp.body["run_id"]


# --------------------------------------------------------------------------- #
# The account store.                                                           #
# --------------------------------------------------------------------------- #
def test_account_store_defaults_and_roundtrip(tmp_path):
    from oolu.durable import DurableConnection

    conn = DurableConnection(tmp_path / "d.db")
    try:
        store = NodeAccountStore(conn)
        assert store.get("n1") is None

        account = NodeAccount(node_id="n1", responsible="alice")
        assert account.status is NodeStatus.NEEDS_VERIFICATION
        assert account.authority_level is None  # authority needs a Supernode
        assert account.audit_mode is False

        store.upsert(account)
        assert store.get("n1") == account

        updated = account.model_copy(
            update={"authority_level": 5, "audit_mode": True}
        )
        store.upsert(updated)
        assert store.get("n1") == updated
    finally:
        conn.close()


def test_authority_level_is_bounded_one_to_five():
    with pytest.raises(ValueError):
        NodeAccount(node_id="n1", responsible="alice", authority_level=6)
    with pytest.raises(ValueError):
        NodeAccount(node_id="n1", responsible="alice", authority_level=0)


# --------------------------------------------------------------------------- #
# The desk overview.                                                           #
# --------------------------------------------------------------------------- #
def test_new_node_appears_with_defaults_and_no_history(tmp_path):
    app, conn, ident, registry, *_rest, desk, _ = _desk_build(tmp_path)
    try:
        exporter, _cleaner = _seed_chain(app, ident, registry)
        entries = desk.overview(principal="noder-export", tenant="t1")
        assert len(entries) == 1
        entry = entries[0]
        assert entry.title == "raw exporter"
        assert entry.status == "needs_verification"
        assert entry.account.responsible == "noder-export"
        assert entry.earnings_micros == 0
        assert entry.health.score is None
    finally:
        conn.close()


def test_earnings_and_health_follow_the_verified_run(tmp_path):
    app, conn, ident, registry, metering, attribution, audit, desk, _ = _desk_build(
        tmp_path, executors={"cli": _CliExecutor()}
    )
    try:
        _run_paid_contract(app, conn, ident, registry, metering, attribution, audit)
        entries = desk.overview(principal="noder-export", tenant="t1")
        (entry,) = entries
        assert entry.earnings_micros > 0
        assert entry.health.verified_successes >= 1
        assert entry.health.score == 1.0
    finally:
        conn.close()


def test_activity_feed_expands_bound_runs_into_steps(tmp_path):
    app, conn, ident, registry, metering, attribution, audit, desk, _ = _desk_build(
        tmp_path, executors={"cli": _CliExecutor()}
    )
    try:
        exporter, _cleaner, run_id = _run_paid_contract(
            app, conn, ident, registry, metering, attribution, audit
        )
        node_id = registry.get_version(exporter).node_id
        feed = desk.activity(node_id, tenant="t1")
        assert [r.run_id for r in feed] == [run_id]
        assert feed[0].gross > 0
        assert any(s["event_type"] for s in feed[0].steps)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Accountability: onboarding and updates.                                      #
# --------------------------------------------------------------------------- #
def test_onboarding_claims_responsibility_and_strangers_cannot_rewrite(tmp_path):
    app, conn, ident, registry, *_rest, desk, _ = _desk_build(tmp_path)
    try:
        exporter, _ = _seed_chain(app, ident, registry)
        node_id = registry.get_version(exporter).node_id

        # No account yet: onboarding is self-service in the tenant — and
        # it offers NO choices; the standalone defaults apply.
        onboarded = desk.onboard_account(node_id, principal="steward", tenant="t1")
        assert onboarded.responsible == "steward"
        assert onboarded.audit_mode is False
        assert onboarded.authority_level is None  # standalone: no authority

        # A live account is protected: a stranger can neither update it
        # nor onboard it out from under its responsible.
        with pytest.raises(OwnershipError):
            desk.update_account(
                node_id, principal="stranger", tenant="t1", status="live"
            )
        with pytest.raises(OwnershipError):
            desk.onboard_account(node_id, principal="stranger", tenant="t1")

        # The responsible updates the MUTABLE slice: status and admin.
        updated = desk.update_account(
            node_id,
            principal="steward",
            tenant="t1",
            status="live",
            admin="ops-team",
        )
        assert updated.status is NodeStatus.LIVE
        assert updated.admin == "ops-team"
        # Everything else — audit, auto-growing, Supernode membership,
        # authority level — is fixed at creation: update_account has no
        # parameter for any of it, absent by construction.
        with pytest.raises(TypeError):
            desk.update_account(
                node_id, principal="steward", tenant="t1", authority_level=4
            )
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The /v1/work routes.                                                         #
# --------------------------------------------------------------------------- #
def test_work_routes_end_to_end(tmp_path):
    app, conn, ident, registry, *_rest = _desk_build(tmp_path)
    try:
        exporter, _ = _seed_chain(app, ident, registry)
        node_id = registry.get_version(exporter).node_id
        token = ident.token("noder-export", "t1")

        listed = app.handle(_req("GET", "/v1/work/nodes", token=token))
        assert listed.status == 200
        (item,) = listed.body["items"]
        assert item["title"] == "raw exporter"
        assert item["account"]["authority_level"] is None  # standalone

        saved = app.handle(
            _req(
                "POST",
                f"/v1/work/nodes/{node_id}/account",
                token=token,
                body={"audit_mode": True, "admin": "gov"},
            )
        )
        assert saved.status == 200
        assert saved.body["audit_mode"] is True
        assert saved.body["audit_mode"] is True

        # Authority is a fixed trait like the rest: any attempt to send it
        # after creation is refused loudly, whatever the value.
        bad = app.handle(
            _req(
                "POST",
                f"/v1/work/nodes/{node_id}/account",
                token=token,
                body={"authority_level": 9},
            )
        )
        assert bad.status == 409
        assert "fixed at creation" in bad.body["error"]["message"]

        activity = app.handle(
            _req("GET", f"/v1/work/nodes/{node_id}/activity", token=token)
        )
        assert activity.status == 200
        assert activity.body["items"] == []

        missing = app.handle(
            _req("GET", "/v1/work/nodes/nope/activity", token=token)
        )
        assert missing.status == 404
    finally:
        conn.close()


def test_work_routes_require_auth_and_a_desk(tmp_path):
    from test_http_gateway import _app

    app, conn, _ = _app(tmp_path)  # no desk configured
    try:
        assert app.handle(_req("GET", "/v1/work/nodes")).status == 401
    finally:
        conn.close()

    app2, conn2, ident, *_ = _build(tmp_path / "b")
    try:
        resp = app2.handle(
            _req("GET", "/v1/work/nodes", token=ident.token("u", "t1"))
        )
        assert resp.status == 404  # desk not enabled on this surface
    finally:
        conn2.close()


# --------------------------------------------------------------------------- #
# Data consent: a node can forbid feeding auto-development.                     #
# --------------------------------------------------------------------------- #
def test_autodev_data_flag_defaults_on_and_roundtrips(tmp_path):
    app, conn, ident, registry, *_rest, desk, _ = _desk_build(tmp_path)
    try:
        exporter, _ = _seed_chain(app, ident, registry)
        node_id = registry.get_version(exporter).node_id
        token = ident.token("noder-export", "t1")

        listed = app.handle(_req("GET", "/v1/work/nodes", token=token))
        assert listed.body["items"][0]["account"]["allow_autodev_data"] is True

        saved = app.handle(
            _req(
                "POST",
                f"/v1/work/nodes/{node_id}/account",
                token=token,
                body={"allow_autodev_data": False},
            )
        )
        assert saved.status == 200
        assert saved.body["allow_autodev_data"] is False
    finally:
        conn.close()


def test_no_autodev_node_leaves_nothing_in_the_trace_corpus(tmp_path):
    """The run executes and settles normally, but the protected node's steps
    never reach the learning store — the enterprise/government guarantee."""
    from oolu.knowledge import TraceStore
    from oolu.knowledge.traces import route_node_key

    traces = TraceStore(tmp_path / "traces.db")
    app, conn, ident, registry, metering, attribution, audit, desk, _ = _desk_build(
        tmp_path, executors={"cli": _CliExecutor()}, trace_store=traces
    )
    try:
        exporter, cleaner = _seed_chain(app, ident, registry)
        exporter_node = registry.get_version(exporter).node_id
        desk.create_account(
            exporter_node,
            principal="noder-export",
            tenant="t1",
            allow_autodev_data=False,
        )
        contract = _assembled_contract(app, ident)

        resp = app.handle(
            _req(
                "POST",
                "/v1/runs/contract",
                token=ident.token("consumer", "t2"),
                body={"contract": contract},
            )
        )
        assert resp.status == 200, resp.body
        assert resp.body["status"] == "succeeded"  # execution is unaffected
        assert resp.body["market"]["gross"] > 0  # settlement is unaffected

        # The cleaner (consenting) was observed; the exporter left nothing.
        cleaner_seen = traces.posterior(route_node_key("invoice cleaner"), "t2")
        exporter_seen = traces.posterior(route_node_key("raw exporter"), "t2")
        assert cleaner_seen.observations > 0
        assert exporter_seen.observations == 0
    finally:
        traces.close()
        conn.close()


# --------------------------------------------------------------------------- #
# Audit nodes: every request commits manually.                                 #
# --------------------------------------------------------------------------- #
def test_audit_node_holds_the_contract_for_manual_commit(tmp_path):
    app, conn, ident, registry, metering, attribution, audit, desk, _ = _desk_build(
        tmp_path, executors={"cli": _CliExecutor()}
    )
    try:
        exporter, _cleaner = _seed_chain(app, ident, registry)
        node_id = registry.get_version(exporter).node_id
        desk.create_account(
            node_id, principal="noder-export", tenant="t1", audit_mode=True
        )
        contract = _assembled_contract(app, ident)

        resp = app.handle(
            _req(
                "POST",
                "/v1/runs/contract",
                token=ident.token("consumer", "t2"),
                body={"contract": contract},
            )
        )
        assert resp.status == 202, resp.body
        assert resp.body["status"] == "awaiting_approval"
        assert f"audit-node:{node_id}" in resp.body["reserved"]

        holds = app.handle(
            _req(
                "GET",
                "/v1/runs/contract/holds",
                token=ident.token("consumer", "t2"),
            )
        )
        assert holds.status == 200
        assert any(
            f"audit-node:{node_id}" in h["reserved"] for h in holds.body["items"]
        )
    finally:
        conn.close()
