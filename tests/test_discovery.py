from __future__ import annotations

import shutil

import pytest

from workflow_gps.assembly import build_discovered_cli_executor
from workflow_gps.skills.discovery import (
    DiscoveredTool,
    ToolSpec,
    discover_tools,
    resolve_file,
)
from workflow_gps.skills.models import ActionEvent, ExecutionStatus


def test_discovers_present_tools_with_absolute_paths():
    catalog = (
        ToolSpec("sh", "shell", ("shell",)),
        ToolSpec("definitely-not-a-real-tool-xyz", "none", ("none",)),
    )
    tools = discover_tools(catalog)
    names = {t.name for t in tools}
    assert "sh" in names
    assert "definitely-not-a-real-tool-xyz" not in names
    sh = next(t for t in tools if t.name == "sh")
    assert sh.path.startswith("/") and sh.category == "shell"


def test_alias_fallback():
    real = "sh" if shutil.which("sh") else "env"
    catalog = (ToolSpec("primary-missing-xyz", "x", ("x",), aliases=(real,)),)
    (tool,) = discover_tools(catalog)
    assert tool.name == "primary-missing-xyz"
    assert tool.path.endswith(real)


def test_resolve_file_direct_and_nested(tmp_path):
    (tmp_path / "top.txt").write_text("x")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    (nested / "deep.csv").write_text("y")

    assert resolve_file("top.txt", tmp_path) == (tmp_path / "top.txt").resolve()
    assert resolve_file("deep.csv", tmp_path) == (nested / "deep.csv").resolve()
    assert resolve_file("missing.dat", tmp_path) is None


def test_discovered_cli_executor_runs_a_discovered_tool(tmp_path):
    if shutil.which("true") is None:
        pytest.skip("no `true` binary")
    executors = build_discovered_cli_executor(
        workspace=tmp_path,
        extra_allow=[shutil.which("true")],
    )
    cli = executors["cli"]
    outcome = cli.execute(
        ActionEvent(
            correlation_id="c",
            adapter="cli",
            operation="run",
            parameters={"argv": ["true"]},
        ),
        idempotency_key="k1",
    )
    assert outcome.status is ExecutionStatus.SUCCEEDED


def test_tools_endpoint_lists_discovered(tmp_path):
    import asyncio
    import json

    from workflow_gps.skills.registry import SkillRegistry
    from workflow_gps.skills.server import SkillsServer

    reg = SkillRegistry(tmp_path / "r.db")
    tools = [
        DiscoveredTool(name="jq", path="/usr/bin/jq", category="data", tags=["json"])
    ]
    server = SkillsServer(reg, tools=tools)

    scope = {"type": "http", "method": "GET", "path": "/v1/tools", "query_string": b""}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    sent: list[dict] = []

    async def send(m):
        sent.append(m)

    try:
        asyncio.run(server(scope, receive, send))
    finally:
        reg.close()
    body = b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )
    payload = json.loads(body)
    assert payload["items"][0]["name"] == "jq"
    assert payload["items"][0]["path"] == "/usr/bin/jq"
