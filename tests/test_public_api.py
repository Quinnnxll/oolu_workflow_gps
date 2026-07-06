"""The public execution API: keys, scopes, and run webhooks.

Preparation for other systems calling task execution: machine keys whose
secrets exist only at mint time, a scope wall that leaves the human
surfaces absent by construction, and signed terminal-event webhooks staged
durably from the audit log.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from test_http_gateway import _app, _req

from oolu.durable import DurableConnection
from oolu.gateway import GatewayApp, WebhookVerifier
from oolu.gateway.notify import (
    RunEventNotifier,
    WebhookEndpointStore,
)
from oolu.identity import Hs256Signer
from oolu.identity.apikeys import ApiKeyError, ApiKeyService, scope_allows

_IDP = "idp-secret"
_ISSUER = "https://idp"
_AUDIENCE = "oolu"


def _token(subject="user-1", tenant="t1"):
    signer = Hs256Signer(secret=_IDP, issuer=_ISSUER, audience=_AUDIENCE)
    return signer.mint(subject=subject, tenant_id=tenant, now=datetime.now(UTC))


def _api_app(tmp_path):
    base, conn, ident = _app(tmp_path)
    endpoints = WebhookEndpointStore(conn)
    app = GatewayApp(
        base._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        api_keys=ApiKeyService(conn),
        webhook_endpoints=endpoints,
    )
    notifier = RunEventNotifier(
        audit=base._durable.audit,
        durable=base._durable,
        endpoints=endpoints,
        conn=conn,
    )
    return app, conn, notifier


class _FakeTransport:
    def __init__(self, status=200):
        self.status = status
        self.posts: list[tuple[str, dict, dict]] = []

    def post(self, url, payload, headers):
        self.posts.append((url, payload, headers))
        return self.status


# --------------------------------------------------------------------------- #
# The key service.                                                             #
# --------------------------------------------------------------------------- #
def test_key_lifecycle_and_hash_only_storage(tmp_path):
    conn = DurableConnection(tmp_path / "d.db")
    try:
        service = ApiKeyService(conn)
        record, secret = service.issue(
            tenant="t1", principal="alice", name="ci-bot"
        )
        assert secret.startswith("oolu_sk_")
        # The database never contains the secret, only its hash.
        with conn.lock:
            rows = conn.db.execute("SELECT * FROM api_keys").fetchall()
        assert all(secret not in str(dict(r)) for r in rows)

        authenticated = service.authenticate(secret)
        assert authenticated is not None
        assert authenticated.key_id == record.key_id
        assert authenticated.last_used_at is not None

        assert service.revoke(record.key_id, tenant="t1") is True
        assert service.authenticate(secret) is None  # revoked = no key
        assert service.revoke(record.key_id, tenant="t1") is False
    finally:
        conn.close()


def test_unknown_scopes_are_refused_at_mint(tmp_path):
    conn = DurableConnection(tmp_path / "d.db")
    try:
        with pytest.raises(ApiKeyError, match="unknown scopes"):
            ApiKeyService(conn).issue(
                tenant="t1", principal="a", name="x", scopes=["settings:write"]
            )
    finally:
        conn.close()


def test_scope_wall_is_absent_by_construction():
    every_scope = frozenset({"runs:submit", "runs:read", "market:read"})
    for path in (
        "/v1/settings",
        "/v1/files",
        "/v1/payment-methods",
        "/v1/chat",
        "/v1/api-keys",
        "/v1/webhook-endpoints",
        "/v1/work/nodes",
        "/v1/nodeplace",
        "/v1/auth/login",
    ):
        assert scope_allows(every_scope, "GET", path) is False
        assert scope_allows(every_scope, "POST", path) is False


# --------------------------------------------------------------------------- #
# Keys through the gateway.                                                    #
# --------------------------------------------------------------------------- #
def test_a_key_executes_tasks_and_nothing_else(tmp_path):
    app, conn, _ = _api_app(tmp_path)
    try:
        minted = app.handle(
            _req(
                "POST",
                "/v1/api-keys",
                token=_token(),
                body={"name": "acme", "scopes": ["runs:submit", "runs:read"]},
            )
        )
        assert minted.status == 201
        secret = minted.body["secret"]

        # Execute a task with the key.
        submitted = app.handle(
            _req("POST", "/v1/runs", token=secret, body={"intent": "do the thing"})
        )
        assert submitted.status == 202
        run_id = submitted.body["run_id"]
        status = app.handle(_req("GET", f"/v1/runs/{run_id}", token=secret))
        assert status.status == 200

        # The scope wall: human surfaces answer 403, whatever the key holds.
        for method, path in (
            ("GET", "/v1/settings"),
            ("GET", "/v1/files"),
            ("POST", "/v1/chat"),
            ("GET", "/v1/payment-methods"),
            ("GET", "/v1/api-keys"),
        ):
            refused = app.handle(_req(method, path, token=secret, body={}))
            assert refused.status == 403, (method, path, refused.status)

        # Keys cannot manage keys — even the management routes themselves.
        self_mint = app.handle(
            _req("POST", "/v1/api-keys", token=secret, body={"name": "sneaky"})
        )
        assert self_mint.status == 403
    finally:
        conn.close()


def test_read_only_key_cannot_submit(tmp_path):
    app, conn, _ = _api_app(tmp_path)
    try:
        minted = app.handle(
            _req(
                "POST",
                "/v1/api-keys",
                token=_token(),
                body={"name": "reader", "scopes": ["runs:read"]},
            )
        )
        secret = minted.body["secret"]
        refused = app.handle(
            _req("POST", "/v1/runs", token=secret, body={"intent": "nope"})
        )
        assert refused.status == 403
        assert app.handle(_req("GET", "/v1/runs", token=secret)).status == 200
    finally:
        conn.close()


def test_revoked_key_is_a_401_and_listing_never_leaks_secrets(tmp_path):
    app, conn, _ = _api_app(tmp_path)
    try:
        minted = app.handle(
            _req("POST", "/v1/api-keys", token=_token(), body={"name": "old"})
        )
        secret, key_id = minted.body["secret"], minted.body["key_id"]

        listed = app.handle(_req("GET", "/v1/api-keys", token=_token()))
        assert "secret" not in listed.body["items"][0]

        revoked = app.handle(
            _req("DELETE", f"/v1/api-keys/{key_id}", token=_token())
        )
        assert revoked.status == 200
        dead = app.handle(_req("GET", "/v1/runs", token=secret))
        assert dead.status == 401
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Run webhooks: derive from the audit log, deliver signed.                     #
# --------------------------------------------------------------------------- #
def test_terminal_events_reach_registered_endpoints_signed(tmp_path):
    app, conn, notifier = _api_app(tmp_path)
    try:
        registered = app.handle(
            _req(
                "POST",
                "/v1/webhook-endpoints",
                token=_token(),
                body={"url": "https://acme.example/hooks"},
            )
        )
        assert registered.status == 201
        secret = registered.body["secret"]

        # A run reaches its terminal event through the normal pipeline.
        submitted = app.handle(
            _req("POST", "/v1/runs", token=_token(), body={"intent": "job"})
        )
        run_id = submitted.body["run_id"]

        transport = _FakeTransport()
        result = notifier.pump(transport)
        assert result["staged"] >= 1
        assert result["delivered"] >= 1

        url, payload, headers = transport.posts[0]
        assert url == "https://acme.example/hooks"
        assert payload["run_id"] == run_id
        assert payload["type"] in {"workflow.completed", "workflow.failed"}
        # The signature verifies with the endpoint's own secret.
        WebhookVerifier(secret).verify(payload, headers)

        # The cursor holds: pumping again stages nothing new.
        again = notifier.pump(transport)
        assert again["staged"] == 0
    finally:
        conn.close()


def test_failed_deliveries_retry_then_stop(tmp_path):
    app, conn, notifier = _api_app(tmp_path)
    try:
        app.handle(
            _req(
                "POST",
                "/v1/webhook-endpoints",
                token=_token(),
                body={"url": "https://down.example/hooks"},
            )
        )
        app.handle(_req("POST", "/v1/runs", token=_token(), body={"intent": "job"}))

        dead = _FakeTransport(status=500)
        notifier.pump(dead)
        for _ in range(10):
            notifier.deliver(dead)
        # Bounded attempts: the delivery failed closed, not forever-pending.
        assert notifier.pending() == []
        from oolu.gateway.notify import MAX_ATTEMPTS

        assert len(dead.posts) == MAX_ATTEMPTS
    finally:
        conn.close()


def test_endpoints_are_tenant_scoped(tmp_path):
    app, conn, notifier = _api_app(tmp_path)
    try:
        app.handle(
            _req(
                "POST",
                "/v1/webhook-endpoints",
                token=_token(subject="user-9", tenant="t2"),
                body={"url": "https://other-tenant.example/hooks"},
            )
        )
        # t1's run must not notify t2's endpoint.
        app.handle(_req("POST", "/v1/runs", token=_token(), body={"intent": "job"}))
        transport = _FakeTransport()
        result = notifier.pump(transport)
        assert result["staged"] == 0
        assert transport.posts == []
    finally:
        conn.close()
