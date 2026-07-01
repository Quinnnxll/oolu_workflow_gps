from __future__ import annotations

from test_http_gateway import _app, _req

from workflow_gps.billing import (
    BillingService,
    EarningsEntry,
    EarningsKind,
    EarningsLedger,
)
from workflow_gps.gateway import GatewayApp
from workflow_gps.nodeplace import NodeplaceService, RegistryStore
from workflow_gps.skills.models import ActionEvent, ReusableSkill, SkillSignature


def _build(tmp_path):
    base, conn, ident = _app(tmp_path)
    ledger = EarningsLedger(conn)
    app = GatewayApp(
        base._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        nodeplace=NodeplaceService(RegistryStore(conn)),
        billing=BillingService(ledger),
    )
    return app, conn, ident, ledger


def _skill_body(*, visibility="public"):
    skill = ReusableSkill(
        name="deploy helper",
        description="automates a deploy",
        signature=SkillSignature(application="shell", adapter="cli"),
        actions=[ActionEvent(correlation_id="c1", adapter="cli", operation="run")],
    )
    return {
        "skill": skill.model_dump(mode="json"),
        "semver": "1.0.0",
        "title": "Deploy Helper",
        "summary": "automates a deploy",
        "tags": ["deploy", "ops"],
        "visibility": visibility,
    }


def _contribute(app, ident, *, subject="noder-1", tenant="t1", visibility="public"):
    resp = app.handle(
        _req(
            "POST",
            "/v1/nodeplace",
            token=ident.token(subject, tenant),
            body=_skill_body(visibility=visibility),
        )
    )
    assert resp.status == 201
    return resp.body


def test_contribute_then_publish_then_discover(tmp_path):
    app, _, ident, _ = _build(tmp_path)
    created = _contribute(app, ident)
    assert len(created["content_hash"]) == 64

    published = app.handle(
        _req(
            "POST",
            f"/v1/listings/{created['listing_id']}/publish",
            token=ident.token("noder-1", "t1"),
        )
    )
    assert published.status == 200
    assert published.body["status"] == "active"

    found = app.handle(
        _req("GET", "/v1/listings", token=ident.token("consumer", "t2"), query={"q": "deploy"})
    )
    assert found.status == 200
    assert [item["version_id"] for item in found.body["items"]] == [created["version_id"]]


def test_draft_listing_not_discoverable(tmp_path):
    app, _, ident, _ = _build(tmp_path)
    _contribute(app, ident)
    found = app.handle(_req("GET", "/v1/listings", token=ident.token("consumer", "t2")))
    assert found.body["items"] == []


def test_list_own_nodes_is_scoped_to_the_caller(tmp_path):
    app, _, ident, _ = _build(tmp_path)
    _contribute(app, ident, subject="noder-1", tenant="t1")
    mine = app.handle(_req("GET", "/v1/nodeplace", token=ident.token("noder-1", "t1")))
    assert len(mine.body["items"]) == 1
    other = app.handle(_req("GET", "/v1/nodeplace", token=ident.token("noder-2", "t2")))
    assert other.body["items"] == []


def test_only_owner_can_revoke(tmp_path):
    app, _, ident, _ = _build(tmp_path)
    created = _contribute(app, ident)
    intruder = app.handle(
        _req(
            "POST",
            f"/v1/nodeplace/{created['node_id']}/revoke",
            token=ident.token("noder-2", "t2"),
        )
    )
    assert intruder.status == 403
    owner = app.handle(
        _req(
            "POST",
            f"/v1/nodeplace/{created['node_id']}/revoke",
            token=ident.token("noder-1", "t1"),
        )
    )
    assert owner.status == 200 and owner.body["revoked"] is True


def test_earnings_are_display_only_and_principal_scoped(tmp_path):
    app, _, ident, ledger = _build(tmp_path)
    ledger.append(
        EarningsEntry(
            noder_principal="noder-1",
            event_id="e1",
            amount_micros=294000,
            kind=EarningsKind.ACCRUAL,
        )
    )
    mine = app.handle(_req("GET", "/v1/earnings", token=ident.token("noder-1", "t1")))
    assert mine.status == 200
    assert mine.body["available_micros"] == 294000

    stranger = app.handle(_req("GET", "/v1/earnings", token=ident.token("noder-2", "t2")))
    assert stranger.body["available_micros"] == 0

    entries = app.handle(
        _req("GET", "/v1/earnings/entries", token=ident.token("noder-1", "t1"))
    )
    assert len(entries.body["items"]) == 1
    stranger_entries = app.handle(
        _req("GET", "/v1/earnings/entries", token=ident.token("noder-2", "t2"))
    )
    assert stranger_entries.body["items"] == []


def test_nodeplace_and_earnings_require_auth(tmp_path):
    app, _, _, _ = _build(tmp_path)
    assert app.handle(_req("GET", "/v1/earnings")).status == 401
    assert app.handle(_req("GET", "/v1/nodeplace")).status == 401


def test_no_payout_or_dispute_routes_exist(tmp_path):
    app, _, ident, _ = _build(tmp_path)
    for path in ("/v1/payout", "/v1/payout-accounts", "/v1/disputes"):
        resp = app.handle(_req("POST", path, token=ident.token("noder-1", "t1")))
        assert resp.status == 404
    document = app.handle(_req("GET", "/v1/openapi.json")).body
    assert not any(
        "payout" in path or "dispute" in path for path in document["paths"]
    )
