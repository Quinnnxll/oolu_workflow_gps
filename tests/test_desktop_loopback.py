from __future__ import annotations

import asyncio
import json

from workflow_gps.assembly import build_desktop_runtime
from workflow_gps.desktop.loopback import DesktopLoopbackApp
from workflow_gps.skills.models import ActionEvent, ReusableSkill, SkillSignature
from workflow_gps.skills.registry import SkillRegistry


def _call(app, method, path, *, query=b"", body=None, headers=None):
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query if isinstance(query, bytes) else query.encode(),
        "headers": [
            (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
        ],
    }
    payload = json.dumps(body).encode() if body is not None else b""

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    sent: list[dict] = []

    async def send(m):
        sent.append(m)

    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    raw = b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )
    return start["status"], json.loads(raw)


class _Model:
    def __init__(self, answer):
        self._answer = answer

    def propose(self, intent):
        return self._answer


def _runtime(tmp_path, **kw):
    return build_desktop_runtime(db_path=tmp_path / "d.db", **kw)


def test_submit_and_fetch_task(tmp_path):
    rt = _runtime(tmp_path)
    app = DesktopLoopbackApp(rt.desktop)
    try:
        status, view = _call(app, "POST", "/v1/tasks", body={"intent": "do a thing"})
        assert status == 201
        run_id = view["run_id"]
        assert view["intent"] == "do a thing"

        status, again = _call(app, "GET", f"/v1/tasks/{run_id}")
        assert status == 200 and again["run_id"] == run_id

        status, tl = _call(app, "GET", f"/v1/tasks/{run_id}/timeline")
        assert status == 200 and isinstance(tl["items"], list)
    finally:
        rt.close()


def test_clarification_answer_flow(tmp_path):
    answer = (
        '{"parameters": [{"name": "city", "value_type": "string", '
        '"required": true, "question": "Which city?"}]}'
    )
    rt = _runtime(tmp_path, intake_model=_Model(answer))
    app = DesktopLoopbackApp(rt.desktop)
    try:
        _, view = _call(app, "POST", "/v1/tasks", body={"intent": "book a flight"})
        run_id = view["run_id"]
        assert view["awaiting"] == "clarification"
        assert view["questions"][0]["parameter"] == "city"

        status, inbox = _call(app, "GET", "/v1/inbox")
        assert status == 200
        assert any(i["run_id"] == run_id for i in inbox["items"])

        status, resumed = _call(
            app,
            "POST",
            f"/v1/tasks/{run_id}/answers",
            body={"answers": {"city": "LIS"}},
        )
        assert status == 200
        assert resumed["phase"] == "failed"  # planning-only: no route configured
    finally:
        rt.close()


def test_unknown_run_is_404(tmp_path):
    rt = _runtime(tmp_path)
    app = DesktopLoopbackApp(rt.desktop)
    try:
        status, _ = _call(app, "GET", "/v1/tasks/nope")
        assert status == 404
        status, _ = _call(app, "POST", "/v1/tasks", body={})
        assert status == 400
    finally:
        rt.close()


def test_skills_library_endpoint(tmp_path):
    rt = _runtime(tmp_path)
    reg = SkillRegistry(tmp_path / "reg.db")
    reg.register(
        ReusableSkill(
            name="Dropdown",
            description="a dropdown skill",
            signature=SkillSignature(application="web", adapter="browser"),
            actions=[
                ActionEvent(correlation_id="c", adapter="browser", operation="run")
            ],
        ),
        semver="1.0.0",
        tags=["ui", "dropdown"],
    )
    app = DesktopLoopbackApp(rt.desktop, registry=reg)
    try:
        status, listing = _call(app, "GET", "/v1/skills")
        assert status == 200 and listing["items"][0]["name"] == "Dropdown"
        status, found = _call(app, "GET", "/v1/skills", query="q=dropdown")
        assert status == 200 and found["items"][0]["name"] == "Dropdown"
    finally:
        reg.close()
        rt.close()


def test_worker_health_and_offline_policy(tmp_path):
    rt = _runtime(tmp_path)
    app = DesktopLoopbackApp(rt.desktop)
    try:
        status, health = _call(app, "GET", "/v1/worker-health")
        assert status == 200 and "labels" in health
        status, policy = _call(app, "GET", "/v1/offline-policy")
        assert status == 200 and policy["network"] == "local-only"
    finally:
        rt.close()


def _call_raw(app, method, path):
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"",
        "headers": [],
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    sent: list[dict] = []

    async def send(m):
        sent.append(m)

    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    headers = {k.decode(): v.decode() for k, v in start.get("headers", [])}
    body = b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )
    return start["status"], headers, body.decode("utf-8")


def test_scaffold_ui_is_served_at_the_root(tmp_path):
    """GET / serves the self-contained shell page — and the page only
    speaks routes this same loopback actually has."""
    rt = _runtime(tmp_path)
    app = DesktopLoopbackApp(rt.desktop)
    try:
        status, headers, body = _call_raw(app, "GET", "/")
        assert status == 200
        assert headers["content-type"].startswith("text/html")
        assert "Workflow-GPS" in body
        # The scaffold stays honest to the API it fronts.
        for route in (
            "/v1/assembly/preview",
            "/v1/assembly/confirm",
            "/v1/assembly/approvals/",
            "/v1/tasks",
            "/v1/inbox",
            "/v1/worker-health",
        ):
            assert route in body, f"UI lost its wiring to {route}"

        # /index.html is the same page; the JSON API is untouched by it.
        assert _call_raw(app, "GET", "/index.html")[0] == 200
        status, _view = _call(app, "GET", "/v1/inbox")
        assert status == 200
        status, _err = _call(app, "POST", "/")  # only GET serves the page
        assert status == 404
    finally:
        rt.close()


def test_ui_script_is_valid_javascript(tmp_path):
    """The scaffold's inline script must at least parse — template edits
    that break the page fail here, not in a user's browser."""
    import re
    import shutil
    import subprocess

    import pytest

    from workflow_gps.desktop.ui import INDEX_HTML

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available to syntax-check the UI script")
    script = re.search(r"<script>\n(.*?)</script>", INDEX_HTML, re.S).group(1)
    path = tmp_path / "ui.js"
    path.write_text(script)
    check = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
    assert check.returncode == 0, check.stderr


def test_websocket_streams_timeline(tmp_path):
    rt = _runtime(tmp_path)
    app = DesktopLoopbackApp(rt.desktop)
    try:
        _, view = _call(app, "POST", "/v1/tasks", body={"intent": "stream me"})
        run_id = view["run_id"]

        scope = {"type": "websocket", "path": f"/v1/tasks/{run_id}/events"}
        frames = [{"type": "websocket.connect"}, {"type": "websocket.disconnect"}]
        idx = 0

        async def receive():
            nonlocal idx
            frame = (
                frames[idx] if idx < len(frames) else {"type": "websocket.disconnect"}
            )
            idx += 1
            return frame

        sent: list[dict] = []

        async def send(m):
            sent.append(m)

        asyncio.run(app(scope, receive, send))
        assert sent[0] == {"type": "websocket.accept"}
        events = [json.loads(m["text"]) for m in sent if m["type"] == "websocket.send"]
        assert all("label" in e for e in events)
    finally:
        rt.close()
