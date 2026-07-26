"""M0 at the doors: /v1/commerce/* — intents, the inbox, digest-bound approvals.

The gateway surface of the commercial spine: authenticated, tenant-scoped,
idempotent, and without an execution door — the OpenAPI document says so and
the router proves it. Everything financial about these routes is a typed
verdict; no handler behind /v1/commerce can move money.
"""

from __future__ import annotations

from datetime import timedelta

from test_http_gateway import NOW, _app, _req

M = 1_000_000


def _grant_delegation(app, token):
    return app.handle(
        _req(
            "POST",
            "/v1/commerce/delegations",
            token=token,
            body={
                "agent_id": "oolu",
                "allowed_actions": ["purchase"],
                "maximum_single_micros": 5_000 * M,
                "daily_limit_micros": 5_000 * M,
                "monthly_limit_micros": 50_000 * M,
                "valid_from": (NOW - timedelta(days=1)).isoformat(),
                "expires_at": (NOW + timedelta(days=30)).isoformat(),
            },
        )
    )


def _offer_body(**overrides):
    offer = dict(
        offer_id="of-1",
        seller_id="acme",
        offer_version=1,
        item_id="steel-bottle",
        quantity=1,
        subtotal_micros=100 * M,
        currency="USD",
        tax_estimate_micros=8 * M,
        fulfillment_terms="ships in 2 days",
        refund_terms="30-day returns",
    )
    offer.update(overrides)
    return offer


def _intent_body(key="k1", **offer_overrides):
    return {
        "offer": _offer_body(**offer_overrides),
        "idempotency_key": key,
        "category": "household",
        "delivery_destination": "home",
        "risk_facts": {
            "first_time_counterparty": False,
            "new_delivery_destination": False,
            "price_benchmark_micros": 108 * M,
            "seller_identity_verified": True,
        },
    }


def test_commerce_requires_authentication(tmp_path):
    app, conn, _ = _app(tmp_path)
    assert app.handle(_req("GET", "/v1/commerce/intents")).status == 401
    assert app.handle(_req("POST", "/v1/commerce/intents")).status == 401
    conn.close()


def test_intent_creation_is_idempotent_and_verdicted(tmp_path):
    app, conn, ident = _app(tmp_path)
    token = ident.token("user-1")
    assert _grant_delegation(app, token).status == 201
    first = app.handle(
        _req("POST", "/v1/commerce/intents", token=token, body=_intent_body())
    )
    assert first.status == 201
    assert first.body["state"] == "approval_pending"
    assert first.body["verdict"]["decision"] == "require_approval"
    assert first.body["verdict"]["reasons"]
    assert first.body["intent_digest"]
    duplicate = app.handle(
        _req("POST", "/v1/commerce/intents", token=token, body=_intent_body())
    )
    assert duplicate.status == 200  # the same key returns the existing intent
    assert (
        duplicate.body["intent"]["intent_id"] == first.body["intent"]["intent_id"]
    )
    conn.close()


def test_the_inbox_lists_exact_terms_and_approval_flips_state(tmp_path):
    app, conn, ident = _app(tmp_path)
    token = ident.token("user-1")
    _grant_delegation(app, token)
    created = app.handle(
        _req("POST", "/v1/commerce/intents", token=token, body=_intent_body())
    )
    intent_id = created.body["intent"]["intent_id"]
    inbox = app.handle(_req("GET", "/v1/commerce/approvals", token=token))
    assert inbox.status == 200
    assert len(inbox.body["items"]) == 1
    summary = inbox.body["items"][0]
    assert summary["total"] == "108.00 USD"
    assert summary["intent_digest"] == created.body["intent_digest"]
    decided = app.handle(
        _req(
            "POST",
            f"/v1/commerce/intents/{intent_id}/approval",
            token=token,
            body={"decision": "approve"},
        )
    )
    assert decided.status == 200
    assert decided.body["state"] == "approved"
    assert decided.body["approval"]["intent_digest"] == created.body["intent_digest"]
    # Decided intents leave the inbox.
    assert app.handle(
        _req("GET", "/v1/commerce/approvals", token=token)
    ).body["items"] == []
    conn.close()


def test_strong_verdicts_refuse_normal_session_approval(tmp_path):
    app, conn, ident = _app(tmp_path)
    token = ident.token("user-1")
    _grant_delegation(app, token)
    created = app.handle(
        _req(
            "POST",
            "/v1/commerce/intents",
            token=token,
            body=_intent_body(key="big", subtotal_micros=1_500 * M),
        )
    )
    assert created.body["verdict"]["decision"] == "require_strong_approval"
    refused = app.handle(
        _req(
            "POST",
            f"/v1/commerce/intents/{created.body['intent']['intent_id']}/approval",
            token=token,
            body={"decision": "approve"},
        )
    )
    assert refused.status == 403
    assert refused.body["error"]["code"] == "step_up_required"
    # Rejecting the same intent needs no step-up.
    rejected = app.handle(
        _req(
            "POST",
            f"/v1/commerce/intents/{created.body['intent']['intent_id']}/approval",
            token=token,
            body={"decision": "reject"},
        )
    )
    assert rejected.status == 200
    assert rejected.body["state"] == "rejected"
    conn.close()


def test_revoking_a_delegation_blocks_open_intents(tmp_path):
    app, conn, ident = _app(tmp_path)
    token = ident.token("user-1")
    granted = _grant_delegation(app, token)
    delegation_id = granted.body["delegation_id"]
    created = app.handle(
        _req("POST", "/v1/commerce/intents", token=token, body=_intent_body())
    )
    intent_id = created.body["intent"]["intent_id"]
    revoked = app.handle(
        _req("DELETE", f"/v1/commerce/delegations/{delegation_id}", token=token)
    )
    assert revoked.status == 200
    assert intent_id in revoked.body["blocked_intents"]
    blocked = app.handle(
        _req("GET", f"/v1/commerce/intents/{intent_id}", token=token)
    )
    assert blocked.body["state"] == "blocked"
    conn.close()


def test_commerce_is_tenant_scoped(tmp_path):
    app, conn, ident = _app(tmp_path)
    token = ident.token("user-1")
    _grant_delegation(app, token)
    created = app.handle(
        _req("POST", "/v1/commerce/intents", token=token, body=_intent_body())
    )
    intent_id = created.body["intent"]["intent_id"]
    other = ident.token("user-2", tenant="t2")
    assert (
        app.handle(
            _req("GET", f"/v1/commerce/intents/{intent_id}", token=other)
        ).status
        == 404
    )
    assert (
        app.handle(_req("GET", "/v1/commerce/intents", token=other)).body["items"]
        == []
    )
    conn.close()


def test_policy_roundtrip_and_no_execution_door(tmp_path):
    app, conn, ident = _app(tmp_path)
    token = ident.token("user-1")
    default = app.handle(_req("GET", "/v1/commerce/policy", token=token))
    assert default.status == 200
    assert default.body["policy_version"] == "purchase-v1"
    tightened = dict(default.body)
    tightened["auto_purchase_limit_micros"] = 10 * M
    put = app.handle(
        _req("PUT", "/v1/commerce/policy", token=token, body=tightened)
    )
    assert put.status == 200
    read_back = app.handle(_req("GET", "/v1/commerce/policy", token=token))
    assert read_back.body["auto_purchase_limit_micros"] == 10 * M
    # M0's boundary, pinned at the router: no execution door exists.
    openapi = app.handle(_req("GET", "/v1/openapi.json"))
    commerce_paths = [
        p for p in openapi.body["paths"] if p.startswith("/v1/commerce")
    ]
    assert commerce_paths
    assert not any("execute" in p or "order" in p for p in commerce_paths)
    executed = app.handle(
        _req("POST", "/v1/commerce/intents/any/execute", token=token)
    )
    assert executed.status == 404
    conn.close()
