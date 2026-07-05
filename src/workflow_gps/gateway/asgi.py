from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from .app import GatewayApp
from .errors import GatewayError
from .http import Request, Response

_FRONTEND_INDEX = Path(__file__).parent / "frontend" / "index.html"

# Live event stream path (ADR-0004). The WebSocket transport binds to the same
# route the SSE snapshot serves over HTTP, so a client upgrades in place.
_EVENTS_PATH = re.compile(r"^/v1/runs/(?P<run_id>[^/]+)/events$")

_FRONTEND_HEADERS = {
    "Content-Type": "text/html; charset=utf-8",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'"
    ),
}


def _load_index() -> bytes:
    return _FRONTEND_INDEX.read_bytes()


def _extract_token(scope: dict) -> tuple[str | None, str | None]:
    """Pull a bearer token from a WebSocket handshake.

    Returns ``(token, accepted_subprotocol)``. The ``bearer, <token>``
    subprotocol is echoed back on accept (as required by the WS spec); the
    ``access_token`` query parameter carries no subprotocol.
    """
    subprotocols = scope.get("subprotocols") or []
    if len(subprotocols) == 2 and subprotocols[0] == "bearer":
        return subprotocols[1], "bearer"
    query = parse_qs(scope.get("query_string", b"").decode("latin1"))
    token = query.get("access_token", [None])[0]
    return token, None


def _serialize(response: Response) -> tuple[bytes, str]:
    body = response.body
    if body is None:
        return b"", response.content_type
    if isinstance(body, (bytes, bytearray)):
        return bytes(body), response.content_type
    if isinstance(body, str):
        return body.encode("utf-8"), response.content_type
    return json.dumps(body).encode("utf-8"), "application/json"


class GatewayASGI:
    def __init__(
        self,
        app: GatewayApp,
        *,
        serve_frontend: bool = True,
        poll_interval: float = 0.5,
    ) -> None:
        self._app = app
        self._serve_frontend = serve_frontend
        self._index = _load_index() if serve_frontend else b""
        # How long the live stream waits for a client frame before polling the
        # durable audit log for new events. A production push seam would replace
        # this poll with a durable subscription; the loop shape stays the same.
        self._poll_interval = poll_interval

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope["type"] == "websocket":
            await self._websocket(scope, receive, send)
            return
        if scope["type"] != "http":
            raise RuntimeError(f"unsupported scope type: {scope['type']}")

        method = scope["method"]
        path = scope["path"]
        if self._serve_frontend and method == "GET" and path in ("/", "/index.html"):
            await self._respond(send, 200, _FRONTEND_HEADERS, self._index)
            return

        request = await self._build_request(scope, method, path, receive)
        response = self._app.handle(request)
        payload, content_type = _serialize(response)
        headers = dict(response.headers)
        headers["Content-Type"] = content_type
        await self._respond(send, response.status, headers, payload)

    # ------------------------------------------------------------------ #
    # Live WebSocket transport (ADR-0004).                                #
    # ------------------------------------------------------------------ #
    async def _websocket(self, scope: dict, receive: Any, send: Any) -> None:
        # Consume the opening handshake before deciding accept vs. close.
        connect = await receive()
        if connect.get("type") != "websocket.connect":
            return

        match = _EVENTS_PATH.match(scope["path"])
        if match is None:
            await send({"type": "websocket.close", "code": 4404})
            return

        # Browsers cannot set an Authorization header on a WebSocket, so the
        # bearer token arrives either as the ``bearer, <token>`` subprotocol or
        # as an ``access_token`` query parameter. The token is still validated,
        # never trusted as text.
        token, subprotocol = _extract_token(scope)
        try:
            state = self._app.authorize_stream(token, match["run_id"])
        except GatewayError as exc:
            await send({"type": "websocket.close", "code": 4000 + exc.status})
            return

        accept: dict[str, Any] = {"type": "websocket.accept"}
        if subprotocol is not None:
            accept["subprotocol"] = subprotocol
        await send(accept)
        await self._pump_events(state.run_id, receive, send)

    async def _pump_events(self, run_id: str, receive: Any, send: Any) -> None:
        """Push new event frames until the client disconnects.

        Poll-then-wait: on connect the client receives the full snapshot, then
        every client frame or idle ``poll_interval`` triggers a re-poll that
        pushes only frames past the last delivered ``seq`` (the audit log is
        append-only, so the cursor never rewinds).
        """
        after = 0
        while True:
            for frame in self._app.run_event_frames(run_id, after_seq=after):
                await send({"type": "websocket.send", "text": json.dumps(frame)})
                after = frame["seq"]
            try:
                message = await asyncio.wait_for(receive(), timeout=self._poll_interval)
            except asyncio.TimeoutError:
                continue
            if message.get("type") == "websocket.disconnect":
                return

    async def _build_request(
        self, scope: dict, method: str, path: str, receive: Any
    ) -> Request:
        headers = {
            k.decode("latin1").lower(): v.decode("latin1")
            for k, v in scope.get("headers", [])
        }
        query = {
            key: values[0]
            for key, values in parse_qs(
                scope.get("query_string", b"").decode("latin1")
            ).items()
        }
        raw = await self._read_body(receive)
        body = None
        if raw:
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                body = parsed
        return Request(
            method=method, path=path, headers=headers, query=query, body=body
        )

    @staticmethod
    async def _read_body(receive: Any) -> bytes:
        chunks: list[bytes] = []
        more = True
        while more:
            message = await receive()
            chunks.append(message.get("body", b"") or b"")
            more = message.get("more_body", False)
        return b"".join(chunks)

    @staticmethod
    async def _respond(
        send: Any, status: int, headers: dict[str, str], body: bytes
    ) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (key.encode("latin1"), value.encode("latin1"))
                    for key, value in headers.items()
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _lifespan(receive: Any, send: Any) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return


_SIGNPOST = b"""Workflow-GPS: this is a signpost, not the gateway.

`uvicorn workflow_gps.gateway.asgi:app` cannot start a usable gateway:
GatewayASGI is a class that must be wired with a configured GatewayApp
(identity validator, resolver, stores) before it can serve anything.

To use Workflow-GPS locally:
  non-developers : run setup.bat (Windows) or ./setup.sh (macOS/Linux)
                   from the repository folder
  developers     : wfgps desktop --open    (the local shell)
                   wfgps serve             (the skills API)
                   wfgps doctor            (check your installation)

To embed the multi-tenant gateway, construct GatewayApp yourself and wrap
it: GatewayASGI(app). See README.md.
"""


async def app(scope: dict, receive: Any, send: Any) -> None:
    """The classic dead end (`uvicorn ...asgi:app`), turned into directions.

    Instead of uvicorn's "Attribute 'app' not found", every request gets a
    503 explaining what this module is and how to actually start the
    product. Deliberately NOT a working gateway: it has no identity or
    stores, and pretending otherwise would be worse than failing.
    """
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    if scope["type"] != "http":
        return
    await send(
        {
            "type": "http.response.start",
            "status": 503,
            "headers": [(b"content-type", b"text/plain; charset=utf-8")],
        }
    )
    await send({"type": "http.response.body", "body": _SIGNPOST})
