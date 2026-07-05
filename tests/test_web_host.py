"""The self-host runner for online web users: one token in, 401 otherwise.

`wfgps desktop` stays loopback-only because the loopback transport has no
auth. `wfgps web` is the same shell wrapped in ``TokenGuardedApp`` — the
single new property that makes a network bind defensible: nobody without
the access token gets anything. Browsers sign in once (`/login?token=…` →
HttpOnly session cookie), API clients send a bearer header, WebSockets
ride the cookie, and everything else is a deliberately information-free
401. The bundled Dockerfile/compose ship exactly this shape.
"""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest
from test_gateway_market import _build

from workflow_gps import cli
from workflow_gps.desktop import DesktopService
from workflow_gps.desktop.loopback import DesktopLoopbackApp
from workflow_gps.desktop.web import SESSION_COOKIE, TokenGuardedApp

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "a-perfectly-long-shared-secret"


def _http(app, method, path, *, query="", headers=None):
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query.encode(),
        "headers": [
            (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
        ],
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    sent = []

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    start = sent[0]
    body = b"".join(m.get("body", b"") for m in sent[1:])
    return start["status"], dict(start.get("headers", [])), body


async def _inner_ok(scope, receive, send):
    if scope["type"] == "websocket":
        await receive()
        await send({"type": "websocket.accept"})
        return
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"inner ok"})


def _login_cookie(app):
    status, headers, _ = _http(app, "GET", "/login", query=f"token={TOKEN}")
    assert status == 303
    cookie = headers[b"set-cookie"].decode()
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie
    return cookie.split(";")[0]  # "wfgps_session=<id>"


# --------------------------------------------------------------------------- #
# The guard.                                                                   #
# --------------------------------------------------------------------------- #
def test_short_tokens_are_refused_outright():
    with pytest.raises(ValueError):
        TokenGuardedApp(_inner_ok, "hunter2")


def test_no_credentials_means_an_information_free_401():
    app = TokenGuardedApp(_inner_ok, TOKEN)
    for path in ("/", "/v1/tasks", "/v1/earnings"):
        status, _headers, body = _http(app, "GET", path)
        assert status == 401
        assert b"inner ok" not in body  # the shell was never reached
        assert TOKEN.encode() not in body  # and the page leaks nothing


def test_login_mints_a_cookie_that_opens_the_shell():
    app = TokenGuardedApp(_inner_ok, TOKEN)
    wrong, _h, _b = _http(app, "GET", "/login", query="token=wrong-but-long-enough")
    assert wrong == 401

    cookie = _login_cookie(app)
    status, _headers, body = _http(app, "GET", "/", headers={"Cookie": cookie})
    assert (status, body) == (200, b"inner ok")

    forged = f"{SESSION_COOKIE}=forged-session-id"
    assert _http(app, "GET", "/", headers={"Cookie": forged})[0] == 401


def test_bearer_token_serves_api_clients_directly():
    app = TokenGuardedApp(_inner_ok, TOKEN)
    ok, _h, body = _http(
        app, "GET", "/v1/tasks", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert (ok, body) == (200, b"inner ok")
    bad, _h, _b = _http(
        app, "GET", "/v1/tasks", headers={"Authorization": "Bearer not-the-token!!"}
    )
    assert bad == 401


def test_websockets_ride_the_cookie_and_close_4401_without_it():
    app = TokenGuardedApp(_inner_ok, TOKEN)

    def upgrade(headers):
        scope = {
            "type": "websocket",
            "path": "/v1/tasks/x/events",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        }
        sent = []

        async def receive():
            return {"type": "websocket.connect"}

        async def send(message):
            sent.append(message)

        asyncio.run(app(scope, receive, send))
        return sent

    assert upgrade({}) == [{"type": "websocket.close", "code": 4401}]
    cookie = _login_cookie(app)
    assert upgrade({"Cookie": cookie}) == [{"type": "websocket.accept"}]


def test_the_real_shell_serves_behind_the_guard(tmp_path):
    gateway, conn, *_rest = _build(tmp_path)
    app = TokenGuardedApp(DesktopLoopbackApp(DesktopService(gateway._durable)), TOKEN)
    assert _http(app, "GET", "/")[0] == 401  # locked by default

    cookie = _login_cookie(app)
    status, _headers, body = _http(app, "GET", "/", headers={"Cookie": cookie})
    assert status == 200
    assert b"Workflow-GPS" in body  # the actual front-end, not a stub

    status, _headers, body = _http(app, "GET", "/v1/inbox", headers={"Cookie": cookie})
    assert status == 200 and json.loads(body) == {"items": []}
    conn.close()


# --------------------------------------------------------------------------- #
# The `wfgps web` command.                                                     #
# --------------------------------------------------------------------------- #
def _run_web(monkeypatch, tmp_path, argv=(), env_token=TOKEN):
    import uvicorn

    served = {}

    def fake_run(app, **kwargs):
        served["app"] = app
        served.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    if env_token is None:
        monkeypatch.delenv("WFGPS_WEB_TOKEN", raising=False)
    else:
        monkeypatch.setenv("WFGPS_WEB_TOKEN", env_token)
    out = io.StringIO()
    code = cli.main(["web", "--db", str(tmp_path / "web.db"), *argv], out=out)
    return code, out.getvalue(), served


def test_wfgps_web_serves_the_guarded_shell(monkeypatch, tmp_path):
    code, banner, served = _run_web(monkeypatch, tmp_path)
    assert code == 0
    assert isinstance(served["app"], TokenGuardedApp)
    assert (served["host"], served["port"]) == ("0.0.0.0", 8765)
    assert f"/login?token={TOKEN}" in banner
    assert "HTTPS" in banner  # the transport warning is not optional


def test_wfgps_web_generates_and_prints_a_token_when_unset(monkeypatch, tmp_path):
    code, banner, _served = _run_web(monkeypatch, tmp_path, env_token=None)
    assert code == 0
    assert "/login?token=" in banner
    assert "one-time token" in banner and "WFGPS_WEB_TOKEN" in banner


def test_wfgps_web_refuses_a_guessable_token(monkeypatch, tmp_path, capsys):
    code, _banner, served = _run_web(monkeypatch, tmp_path, env_token="short")
    assert code == 2 and "app" not in served
    assert "at least 16 characters" in capsys.readouterr().err


def test_desktop_still_refuses_non_loopback_hosts(capsys):
    code = cli.main(["desktop", "--host", "0.0.0.0"])
    assert code == 2
    assert "loopback" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# The container shape.                                                         #
# --------------------------------------------------------------------------- #
def test_dockerfile_runs_the_web_command_on_a_volume():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert '"wfgps", "web"' in dockerfile
    assert "EXPOSE 8765" in dockerfile
    assert '".[serve]"' in dockerfile
    assert "/data" in dockerfile  # all state on one backupable volume


def test_compose_requires_a_token_and_persists_data():
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "WFGPS_WEB_TOKEN" in compose and ":?" in compose  # refuse to guess
    assert "wfgps-data:/data" in compose
    assert "8765:8765" in compose


def test_readme_documents_self_hosting():
    readme = (ROOT / "README.md").read_text()
    assert "wfgps web" in readme
    assert "docker compose up" in readme
    assert "Bearer" in readme
