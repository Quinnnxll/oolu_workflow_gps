"""Self-hosting for online web users — the shell behind one shared secret.

The desktop loopback transport is deliberately unauthenticated: it binds
127.0.0.1 and serves whoever owns the machine, so `wfgps desktop` refuses
any other host. Self-hosting for browsers elsewhere needs exactly one new
property — **nobody without the access token gets anything** — and this
wrapper adds it without touching the app it guards:

- a browser signs in once at ``GET /login?token=…`` (the URL ``wfgps web``
  prints at startup) and receives an ``HttpOnly``/``SameSite=Lax`` session
  cookie; API clients may instead send ``Authorization: Bearer <token>``
  on every request;
- token comparison is constant-time, and failures get one deliberately
  information-free 401 page that only describes the login URL *shape*;
- WebSocket upgrades (the live task timeline) ride the same cookie —
  browsers attach cookies to the handshake — and close with 4401 without
  one;
- sessions live in process memory, so a restart signs everyone out: for a
  single-operator self-host that is a feature, not a bug.

Transport security stays the operator's job: serve this **behind HTTPS**
(a reverse proxy such as Caddy or nginx) — the token and cookie are bearer
secrets and travel with every request. The bundled ``Dockerfile`` and
``docker-compose.yml`` wire exactly that shape.
"""

from __future__ import annotations

import hmac
import secrets
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qs

LOGIN_PATH = "/login"
SESSION_COOKIE = "wfgps_session"
MIN_TOKEN_LENGTH = 16

_UNAUTHORIZED_HTML = """<!doctype html><meta charset="utf-8">
<title>Workflow-GPS — locked</title>
<body style="font-family: system-ui; max-width: 40rem; margin: 4rem auto">
<h1>Workflow-GPS is locked</h1>
<p>This server needs its access token. Open:</p>
<p><code>/login?token=&lt;your access token&gt;</code></p>
<p>The full sign-in URL was printed in the terminal (or container log)
when the server started.</p>
</body>"""


class TokenGuardedApp:
    """An ASGI wrapper: one shared token in, everything else 401."""

    def __init__(self, inner, token: str):
        if not token or len(token) < MIN_TOKEN_LENGTH:
            raise ValueError(
                f"the web access token must be at least {MIN_TOKEN_LENGTH} "
                "characters — short secrets are guessable"
            )
        self._inner = inner
        self._token = token
        self._sessions: set[str] = set()

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self._inner(scope, receive, send)
            return
        if scope["type"] == "websocket":
            if self._authorized(scope):
                await self._inner(scope, receive, send)
                return
            message = await receive()  # consume the opening handshake
            if message.get("type") == "websocket.connect":
                await send({"type": "websocket.close", "code": 4401})
            return
        if scope["type"] != "http":
            await self._inner(scope, receive, send)
            return

        if scope["path"] == LOGIN_PATH:
            await self._login(scope, send)
            return
        if self._authorized(scope):
            await self._inner(scope, receive, send)
            return
        await _respond(
            send,
            401,
            _UNAUTHORIZED_HTML.encode("utf-8"),
            [(b"content-type", b"text/html; charset=utf-8")],
        )

    # ------------------------------------------------------------------ #
    def _authorized(self, scope: dict) -> bool:
        headers = {
            key.decode("latin1").lower(): value.decode("latin1")
            for key, value in scope.get("headers", [])
        }
        bearer = headers.get("authorization", "")
        if bearer.lower().startswith("bearer "):
            if hmac.compare_digest(bearer[len("bearer ") :].strip(), self._token):
                return True
        cookie = SimpleCookie()
        cookie.load(headers.get("cookie", ""))
        session = cookie.get(SESSION_COOKIE)
        return session is not None and session.value in self._sessions

    async def _login(self, scope: dict, send: Any) -> None:
        query = parse_qs(scope.get("query_string", b"").decode("latin1"))
        offered = query.get("token", [""])[0]
        if not hmac.compare_digest(offered, self._token):
            await _respond(
                send,
                401,
                _UNAUTHORIZED_HTML.encode("utf-8"),
                [(b"content-type", b"text/html; charset=utf-8")],
            )
            return
        session = secrets.token_urlsafe(24)
        self._sessions.add(session)
        cookie = f"{SESSION_COOKIE}={session}; Path=/; HttpOnly; SameSite=Lax"
        await _respond(
            send,
            303,
            b"",
            [
                (b"location", b"/"),
                (b"set-cookie", cookie.encode("latin1")),
                (b"cache-control", b"no-store"),
            ],
        )


async def _respond(send, status: int, body: bytes, headers) -> None:
    await send(
        {"type": "http.response.start", "status": status, "headers": list(headers)}
    )
    await send({"type": "http.response.body", "body": body})
