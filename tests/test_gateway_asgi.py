from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from test_http_gateway import _AUDIENCE, _IDP, _ISSUER, _app

from oolu.gateway import GatewayASGI
from oolu.identity import Hs256Signer


def _fresh_token(subject="user-1", tenant="t1"):
    signer = Hs256Signer(secret=_IDP, issuer=_ISSUER, audience=_AUDIENCE)
    return signer.mint(subject=subject, tenant_id=tenant, now=datetime.now(UTC))


def _call(asgi, method, path, *, headers=None, body=None, query=b""):
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [
            (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
        ],
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
    body_bytes = b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )
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
        assert b"OoLu" in body
    finally:
        conn.close()


def test_desktop_shell_serves_the_built_react_app(tmp_path):
    """`oolu desktop` (what setup.bat starts) must show the product UI:
    the index references a built asset, and that exact asset serves."""
    import re

    app, conn, _ = _app(tmp_path)
    try:
        asgi = GatewayASGI(app, frontend="shell")

        status, headers, body = _call(asgi, "GET", "/")
        assert status == 200
        assert headers["Content-Type"].startswith("text/html")
        script = re.search(rb'src="(/assets/[^"]+\.js)"', body)
        assert script, "shell index does not reference a built script"

        status, headers, _ = _call(asgi, "GET", script.group(1).decode())
        assert status == 200
        assert headers["Content-Type"].startswith("text/javascript")
        assert "immutable" in headers["Cache-Control"]

        style = re.search(rb'href="(/assets/[^"]+\.css)"', body)
        assert style, "shell index does not reference a built stylesheet"
        status, headers, _ = _call(asgi, "GET", style.group(1).decode())
        assert status == 200
        assert headers["Content-Type"].startswith("text/css")
    finally:
        conn.close()


def test_paired_server_widens_the_csp_to_exactly_that_origin(tmp_path):
    """OOLU_SERVER_URL must be callable from the shell: connect-src gains
    the paired origin (and only the origin — path stripped), nothing else."""
    app, conn, _ = _app(tmp_path)
    try:
        asgi = GatewayASGI(
            app,
            frontend="shell",
            connect_src=("https://cloud.oolu.example/some/path",),
        )
        _, headers, _ = _call(asgi, "GET", "/")
        csp = headers["Content-Security-Policy"]
        assert "connect-src 'self' https://cloud.oolu.example;" in csp

        # Unpaired installs stay locked to themselves.
        plain = GatewayASGI(app, frontend="shell")
        _, headers, _ = _call(plain, "GET", "/")
        assert "connect-src 'self';" in headers["Content-Security-Policy"]
    finally:
        conn.close()


def test_shell_assets_cannot_reach_outside_the_dist(tmp_path):
    app, conn, _ = _app(tmp_path)
    try:
        asgi = GatewayASGI(app, frontend="shell")
        # A crafted traversal that resolves to real source code must not
        # serve it; a missing asset falls through to the router's 404.
        for path in (
            "/assets/../../../asgi.py",
            "/assets/../index.html",
            "/assets/nope.js",
            "/asgi.py",
        ):
            status, _, body = _call(asgi, "GET", path)
            assert status != 200, path
            assert b"GatewayASGI" not in body, path
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


# --------------------------------------------------------------------------- #
# Live WebSocket event transport (ADR-0004).                                  #
# --------------------------------------------------------------------------- #
def _ws(asgi, path, *, subprotocols=None, query=b"", incoming=None):
    """Drive a WebSocket scope. ``incoming`` is a list of post-connect client

    frames (a callable is invoked for its side effect, e.g. appending an audit
    event, and then treated as a plain client frame); the stream is terminated
    with a ``websocket.disconnect`` once they are exhausted.
    """
    scope = {
        "type": "websocket",
        "path": path,
        "subprotocols": list(subprotocols or []),
        "query_string": query if isinstance(query, bytes) else query.encode(),
    }
    frames: list = [{"type": "websocket.connect"}, *(incoming or [])]
    index = 0

    async def receive():
        nonlocal index
        if index < len(frames):
            frame = frames[index]
            index += 1
            if callable(frame):
                frame()
                return {"type": "websocket.receive", "text": "poll"}
            return frame
        return {"type": "websocket.disconnect", "code": 1000}

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    asyncio.run(asgi(scope, receive, send))
    return sent


def _submit_run(asgi, token):
    _, _, body = _call(
        asgi, "POST", "/v1/runs", headers=_auth(token), body={"intent": "auto"}
    )
    return json.loads(body)["run_id"]


def test_websocket_rejects_missing_token(tmp_path):
    app, conn, _ = _app(tmp_path)
    asgi = GatewayASGI(app)
    token = _fresh_token()
    try:
        run_id = _submit_run(asgi, token)
        sent = _ws(asgi, f"/v1/runs/{run_id}/events")
        assert sent == [{"type": "websocket.close", "code": 4401}]
    finally:
        conn.close()


def test_websocket_streams_snapshot_then_closes(tmp_path):
    app, conn, _ = _app(tmp_path)
    asgi = GatewayASGI(app)
    token = _fresh_token()
    try:
        run_id = _submit_run(asgi, token)
        sent = _ws(
            asgi,
            f"/v1/runs/{run_id}/events",
            subprotocols=["bearer", token],
        )
        assert sent[0] == {"type": "websocket.accept", "subprotocol": "bearer"}
        frames = [json.loads(m["text"]) for m in sent if m["type"] == "websocket.send"]
        assert frames, "expected the run's audit snapshot to be pushed"
        assert all({"seq", "event_type", "phase", "at"} <= f.keys() for f in frames)
        seqs = [f["seq"] for f in frames]
        assert seqs == sorted(seqs)
    finally:
        conn.close()


def test_websocket_pushes_new_events_incrementally(tmp_path):
    app, conn, _ = _app(tmp_path)
    asgi = GatewayASGI(app)
    token = _fresh_token()
    try:
        run_id = _submit_run(asgi, token)

        def emit():
            app._durable.audit.append("live.marker", {"run_id": run_id})

        sent = _ws(
            asgi,
            f"/v1/runs/{run_id}/events",
            query=f"access_token={token}",
            incoming=[emit],
        )
        assert sent[0] == {"type": "websocket.accept"}
        frames = [json.loads(m["text"]) for m in sent if m["type"] == "websocket.send"]
        assert any(f["event_type"] == "live.marker" for f in frames)
        # The marker is delivered exactly once and never before its own seq.
        markers = [f for f in frames if f["event_type"] == "live.marker"]
        assert len(markers) == 1
    finally:
        conn.close()


def test_websocket_cross_tenant_run_is_closed(tmp_path):
    app, conn, _ = _app(tmp_path)
    asgi = GatewayASGI(app)
    owner = _fresh_token(subject="user-1", tenant="t1")
    intruder = _fresh_token(subject="user-2", tenant="t2")
    try:
        run_id = _submit_run(asgi, owner)
        sent = _ws(
            asgi,
            f"/v1/runs/{run_id}/events",
            subprotocols=["bearer", intruder],
        )
        assert sent == [{"type": "websocket.close", "code": 4404}]
    finally:
        conn.close()
