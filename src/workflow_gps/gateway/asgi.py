from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from .app import GatewayApp
from .http import Request, Response

_FRONTEND_INDEX = Path(__file__).parent / "frontend" / "index.html"

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
    def __init__(self, app: GatewayApp, *, serve_frontend: bool = True) -> None:
        self._app = app
        self._serve_frontend = serve_frontend
        self._index = _load_index() if serve_frontend else b""

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
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

    async def _build_request(
        self, scope: dict, method: str, path: str, receive: Any
    ) -> Request:
        headers = {k.decode("latin1").lower(): v.decode("latin1") for k, v in scope.get("headers", [])}
        query = {
            key: values[0]
            for key, values in parse_qs(scope.get("query_string", b"").decode("latin1")).items()
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
        return Request(method=method, path=path, headers=headers, query=query, body=body)

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
    async def _respond(send: Any, status: int, headers: dict[str, str], body: bytes) -> None:
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
