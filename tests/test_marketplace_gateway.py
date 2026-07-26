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


def _approved_intent_id(app, token):
    created = app.handle(
        _req("POST", "/v1/commerce/intents", token=token, body=_intent_body())
    )
    intent_id = created.body["intent"]["intent_id"]
    app.handle(
        _req(
            "POST",
            f"/v1/commerce/intents/{intent_id}/approval",
            token=token,
            body={"decision": "approve"},
        )
    )
    return intent_id


def test_orders_flow_from_intent_to_completed_with_ledger(tmp_path):
    app, conn, ident = _app(tmp_path)
    token = ident.token("user-1")
    _grant_delegation(app, token)
    intent_id = _approved_intent_id(app, token)
    placed = app.handle(
        _req(
            "POST", "/v1/commerce/orders", token=token, body={"intent_id": intent_id}
        )
    )
    assert placed.status == 201
    assert placed.body["state"] == "confirmed"
    order_id = placed.body["order"]["order_id"]
    duplicate = app.handle(
        _req(
            "POST", "/v1/commerce/orders", token=token, body={"intent_id": intent_id}
        )
    )
    assert duplicate.status == 200
    assert duplicate.body["order"]["order_id"] == order_id

    shipped = app.handle(
        _req(
            "POST",
            f"/v1/commerce/orders/{order_id}/ship",
            token=token,
            body={"tracking": "TRK1"},
        )
    )
    assert shipped.status == 200
    # Delivery WITHOUT evidence: the escrow exception — nothing captures,
    # and acceptance refuses until evidence lands.
    bare = app.handle(
        _req("POST", f"/v1/commerce/orders/{order_id}/deliver", token=token)
    )
    assert bare.status == 200
    refused = app.handle(
        _req("POST", f"/v1/commerce/orders/{order_id}/accept", token=token)
    )
    assert refused.status == 409
    assert "evidence" in refused.body["error"]["message"]
    evidenced = app.handle(
        _req(
            "POST",
            f"/v1/commerce/orders/{order_id}/evidence",
            token=token,
            body={"evidence": "signed-photo"},
        )
    )
    assert evidenced.status == 200
    accepted = app.handle(
        _req("POST", f"/v1/commerce/orders/{order_id}/accept", token=token)
    )
    assert accepted.status == 200
    assert accepted.body["state"] == "completed"

    book = app.handle(
        _req("GET", f"/v1/commerce/orders/{order_id}/ledger", token=token)
    )
    assert book.status == 200
    # Escrow discipline on the book: capture holds, release settles.
    assert [txn["kind"] for txn in book.body["items"]] == ["capture", "release"]
    assert sum(
        entry["amount_micros"]
        for txn in book.body["items"]
        for entry in txn["entries"]
    ) == 0
    invoice = app.handle(
        _req("GET", f"/v1/commerce/orders/{order_id}/invoice", token=token)
    )
    assert invoice.status == 200
    assert invoice.body["number"].startswith("INV-")

    refunded = app.handle(
        _req(
            "POST",
            f"/v1/commerce/orders/{order_id}/refund",
            token=token,
            body={"reason": "damaged"},
        )
    )
    assert refunded.status == 200
    assert refunded.body["state"] == "refunded"
    book = app.handle(
        _req("GET", f"/v1/commerce/orders/{order_id}/ledger", token=token)
    )
    # Both settlement transactions reversed; the book nets to zero.
    assert [txn["kind"] for txn in book.body["items"]] == [
        "capture",
        "release",
        "refund",
        "refund",
    ]

    # A stranger from another tenant sees no order and no book.
    other = ident.token("user-2", tenant="t2")
    assert (
        app.handle(
            _req("GET", f"/v1/commerce/orders/{order_id}", token=other)
        ).status
        == 404
    )
    conn.close()


def test_seller_kyc_from_application_to_published_listing(tmp_path):
    """The full seller path: a personal mailbox is refused outright, a
    company application queues, only approve authority decides, and a
    verified seller's listing reaches the public catalog."""
    app, conn, ident = _app(tmp_path)
    seller = ident.token("user-1")
    refused = app.handle(
        _req(
            "POST",
            "/v1/commerce/seller/kyc",
            token=seller,
            body={"legal_name": "Acme GmbH", "company_email": "a@gmail.com"},
        )
    )
    assert refused.status == 400  # a personal mailbox cannot anchor an entity

    applied = app.handle(
        _req(
            "POST",
            "/v1/commerce/seller/kyc",
            token=seller,
            body={"legal_name": "Acme GmbH", "company_email": "kyc@acme.example"},
        )
    )
    assert applied.status == 201
    assert applied.body["status"] == "pending_review"

    draft = app.handle(
        _req(
            "POST",
            "/v1/commerce/listings",
            token=seller,
            body={
                "title": "Steel bottle",
                "unit_price_micros": 100 * M,
                "quantity_available": 3,
            },
        )
    )
    listing_id = draft.body["listing_id"]
    # Pending is not verified: publication still refuses.
    assert (
        app.handle(
            _req(
                "POST", f"/v1/commerce/listings/{listing_id}/publish", token=seller
            )
        ).status
        == 403
    )
    # The applicant cannot decide their own application — approve
    # authority is a stored grant, not a wish.
    decide_body = {"principal": "user-1", "approved": True}
    assert (
        app.handle(
            _req(
                "POST",
                "/v1/commerce/seller/kyc/decide",
                token=seller,
                body=decide_body,
            )
        ).status
        == 403
    )
    decided = app.handle(
        _req(
            "POST",
            "/v1/commerce/seller/kyc/decide",
            token=ident.token("approver-1"),
            body=decide_body,
        )
    )
    assert decided.status == 200
    assert decided.body["status"] == "verified"

    published = app.handle(
        _req("POST", f"/v1/commerce/listings/{listing_id}/publish", token=seller)
    )
    assert published.status == 200
    catalog = app.handle(_req("GET", "/v1/commerce/catalog", token=seller))
    assert [item["listing_id"] for item in catalog.body["items"]] == [listing_id]
    # A verified seller cannot re-apply over a verified record.
    assert (
        app.handle(
            _req(
                "POST",
                "/v1/commerce/seller/kyc",
                token=seller,
                body={
                    "legal_name": "Acme GmbH",
                    "company_email": "kyc@acme.example",
                },
            )
        ).status
        == 400
    )
    conn.close()


def test_listings_publish_is_gated_on_seller_verification(tmp_path):
    app, conn, ident = _app(tmp_path)
    token = ident.token("user-1")
    draft = app.handle(
        _req(
            "POST",
            "/v1/commerce/listings",
            token=token,
            body={
                "title": "Steel bottle",
                "unit_price_micros": 100 * M,
                "quantity_available": 3,
            },
        )
    )
    assert draft.status == 201
    listing_id = draft.body["listing_id"]
    # No KYC service is wired in this rig: publication refuses, it never
    # trusts — the MVP boundary at the door.
    refused = app.handle(
        _req(
            "POST", f"/v1/commerce/listings/{listing_id}/publish", token=token
        )
    )
    assert refused.status == 403
    assert refused.body["error"]["code"] == "seller_unverified"
    assert (
        app.handle(_req("GET", "/v1/commerce/catalog", token=token)).body["items"]
        == []
    )
    conn.close()


def test_evidence_content_lands_content_addressed(tmp_path):
    """Delivery evidence supplied as CONTENT is preserved in the object
    store and the ref that rides the order (and the audit chain) is its
    sha256 — tamper-evident, not just a claim. A host without evidence
    storage refuses content and says what to pass instead."""

    from oolu.durable.artifacts import FilesystemArtifactStore
    from oolu.gateway import GatewayApp

    bare, conn, ident = _app(tmp_path)
    token = ident.token("user-1")
    _grant_delegation(bare, token)
    intent_id = _approved_intent_id(bare, token)
    placed = bare.handle(
        _req(
            "POST", "/v1/commerce/orders", token=token, body={"intent_id": intent_id}
        )
    )
    order_id = placed.body["order"]["order_id"]
    bare.handle(
        _req("POST", f"/v1/commerce/orders/{order_id}/ship", token=token)
    )
    # No evidence store on this host: content is refused with directions.
    refused = bare.handle(
        _req(
            "POST",
            f"/v1/commerce/orders/{order_id}/deliver",
            token=token,
            body={"evidence_content": "photo bytes"},
        )
    )
    assert refused.status == 400
    assert "storage" in refused.body["error"]["message"]

    # The same doors on a host WITH storage: content lands addressed.
    store = FilesystemArtifactStore(tmp_path / "evidence")
    stocked = GatewayApp(
        bare._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        commerce_evidence=store,
    )
    delivered = stocked.handle(
        _req(
            "POST",
            f"/v1/commerce/orders/{order_id}/deliver",
            token=token,
            body={"evidence_content": "photo bytes"},
        )
    )
    assert delivered.status == 200
    ref = delivered.body["order"]["delivery_evidence"]
    assert ref.startswith("sha256:")
    assert store.get(ref) == b"photo bytes"
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
    # The standing boundary, pinned at the router: intents never execute
    # directly — money enters only through the order machine, and there is
    # no raw execute door to bypass it.
    openapi = app.handle(_req("GET", "/v1/openapi.json"))
    commerce_paths = [
        p for p in openapi.body["paths"] if p.startswith("/v1/commerce")
    ]
    assert commerce_paths
    assert not any("execute" in p for p in commerce_paths)
    executed = app.handle(
        _req("POST", "/v1/commerce/intents/any/execute", token=token)
    )
    assert executed.status == 404
    conn.close()
