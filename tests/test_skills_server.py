from __future__ import annotations

import asyncio
import json

import pytest

from workflow_gps.skills.models import (
    ActionEvent,
    ExecutionOutcome,
    ExecutionStatus,
    ReusableSkill,
    SkillParameter,
    SkillSignature,
)
from workflow_gps.skills.registry import SkillRegistry
from workflow_gps.skills.server import SkillsServer


class _Executor:
    name = "browser"

    def capabilities(self):
        return frozenset({"click", "read_rows"})

    def execute(self, action, *, idempotency_key):
        return ExecutionOutcome(
            idempotency_key=idempotency_key,
            skill_id=action.correlation_id,
            status=ExecutionStatus.SUCCEEDED,
            evidence={"op": action.operation, "params": action.parameters},
        )


def _skill(name, description, *ops, params=()):
    return ReusableSkill(
        name=name,
        description=description,
        signature=SkillSignature(application="web", adapter="browser"),
        parameters=[SkillParameter(name=p, value_type="string") for p in params],
        actions=[
            ActionEvent(correlation_id="c", adapter="browser", operation=op)
            for op in ops
        ],
    )


def _call(app, method, path, *, query=b"", body=None):
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query if isinstance(query, bytes) else query.encode(),
    }
    payload = json.dumps(body).encode() if body is not None else b""

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    raw = b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )
    return start["status"], json.loads(raw)


@pytest.fixture
def app(tmp_path):
    reg = SkillRegistry(tmp_path / "reg.db")
    reg.register(
        _skill("Paginated Table", "extract a paginated table", "read_rows"),
        semver="1.0.0",
        tags=["table", "extract"],
    )
    reg.register(
        _skill(
            "Dynamic Dropdown",
            "interact with a dynamic dropdown",
            "click",
            params=["selector"],
        ),
        semver="1.3.0",
        tags=["ui", "dropdown"],
    )
    yield SkillsServer(reg, executors={"browser": _Executor()})
    reg.close()


def test_list_skills(app):
    status, payload = _call(app, "GET", "/v1/skills")
    assert status == 200
    assert {item["name"] for item in payload["items"]} == {
        "Paginated Table",
        "Dynamic Dropdown",
    }


def test_search_skills(app):
    status, payload = _call(app, "GET", "/v1/skills", query="q=dropdown")
    assert status == 200
    assert payload["items"][0]["name"] == "Dynamic Dropdown"
    assert payload["items"][0]["score"] > 0


def test_execute_runs_the_skill(app):
    _, listing = _call(app, "GET", "/v1/skills", query="q=dropdown")
    skill_id = listing["items"][0]["skill_id"]
    status, payload = _call(
        app,
        "POST",
        "/v1/skills/execute",
        body={"skill_id": skill_id, "parameters": {"selector": "#menu"}},
    )
    assert status == 200
    assert payload["outcomes"][0]["status"] == "succeeded"
    assert payload["outcomes"][0]["evidence"]["params"] == {"selector": "#menu"}


def test_execute_unknown_skill_is_404(app):
    status, payload = _call(
        app, "POST", "/v1/skills/execute", body={"skill_id": "nope"}
    )
    assert status == 404


def test_execute_refuses_irreversible(tmp_path):
    reg = SkillRegistry(tmp_path / "r.db")
    danger = _skill("Purge", "delete everything", "delete_all")
    reg.register(danger, semver="1.0.0")
    app = SkillsServer(reg, executors={"browser": _Executor()})
    try:
        status, payload = _call(
            app, "POST", "/v1/skills/execute", body={"skill_id": danger.id}
        )
        assert status == 409
        assert payload["error"] == "irreversible_action"
    finally:
        reg.close()
