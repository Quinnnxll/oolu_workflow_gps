from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from test_http_gateway import _AUDIENCE, _IDP, _ISSUER, _app

from workflow_gps.gateway import GatewayASGI
from workflow_gps.identity import Hs256Signer


def _fresh_token(subject="user-1", tenant="t1"):
    signer = Hs256Signer(secret=_IDP, issuer=_ISSUER, audience=_AUDIENCE)
    return signer.mint(subject=subject, tenant_id=tenant, now=datetime.now(UTC))


def _call(asgi, method, path, *, headers=None, body=None, query=b""):
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "query_string": query if isinstance(query, bytes) else query.encode(),
    }
    payload = json.dumps(body).encode() if body is not None else b""

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    asyncio.run(asgi(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body_bytes = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    headers_out = {k.decode(): v.decode() for k, v in start["headers"]}
    return start["status"], headers_out, body_bytes


def _auth(token):
    return {"Authorization": "Bearer " + token}


def test_serves_frontend_index(tmp_path):
    app, conn, _ = _app(tmp_path)
    try:
        status, headers, body = _call(GatewayASGI(app), "GET", "/")
        assert status == 200
        assert headers["Content-Type"].startswith("text/html")
        assert b"Workflow-GPS" in body
    finally:
        conn.close()


def test_public_openapi_through_asgi(tmp_path):
    app, conn, _ = _app(tmp_path)
    try:
        status, headers, body = _call(GatewayASGI(app), "GET", "/v1/openapi.json")
        assert status == 200
        assert headers["Content-Type"] == "application/json"
        document = json.loads(body)
        assert document["info"]["version"] == "v1"
        assert "/v1/runs" in document["paths"]
    finally:
        conn.close()


def test_unauthenticated_request_is_rejected(tmp_path):
    app, conn, _ = _app(tmp_path)
    try:
        status, _, _ = _call(GatewayASGI(app), "GET", "/v1/runs")
        assert status == 401
    finally:
        conn.close()


def test_full_run_lifecycle_through_asgi(tmp_path):
    app, conn, ident = _app(tmp_path)
    asgi = GatewayASGI(app)
    token = _fresh_token()
    try:
        status, _, body = _call(
            asgi, "POST", "/v1/runs", headers=_auth(token), body={"intent": "auto"}
        )
        assert status in (200, 202)
        run_id = json.loads(body)["run_id"]

        status, _, body = _call(asgi, "GET", "/v1/runs/" + run_id, headers=_auth(token))
        assert status == 200
        assert json.loads(body)["run_id"] == run_id

        status, headers, body = _call(
            asgi, "GET", "/v1/runs/" + run_id + "/events", headers=_auth(token)
        )
        assert status == 200
        assert headers["Content-Type"] == "text/event-stream"
        assert b"event:" in body
    finally:
        conn.close()


def test_query_string_is_forwarded(tmp_path):
    app, conn, ident = _app(tmp_path)
    asgi = GatewayASGI(app)
    token = _fresh_token()
    try:
        _call(asgi, "POST", "/v1/runs", headers=_auth(token), body={"intent": "auto"})
        status, _, body = _call(
            asgi, "GET", "/v1/runs", headers=_auth(token), query="page=1&size=1"
        )
        assert status == 200
        assert len(json.loads(body)["items"]) == 1
    finally:
        conn.close()
