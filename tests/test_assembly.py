from __future__ import annotations

import shutil

import pytest

from oolu.assembly import build_cli_executor, build_host_runtime
from oolu.orchestrator.state import Blueprint, ReservedAction
from oolu.skills.models import (
    ActionEvent,
    ExecutionOutcome,
    ExecutionStatus,
    ReusableSkill,
    SkillSignature,
)


class _Model:
    def __init__(self, answer):
        self._answer = answer

    def propose(self, intent: str) -> str:
        return self._answer


class _Executor:
    name = "local"

    def capabilities(self):
        return frozenset({"echo"})

    def execute(self, action, *, idempotency_key):
        return ExecutionOutcome(
            idempotency_key=idempotency_key,
            skill_id=action.correlation_id,
            status=ExecutionStatus.SUCCEEDED,
        )


def _echo_blueprint():
    action = ActionEvent(correlation_id="c1", adapter="local", operation="echo")
    return Blueprint(
        name="echo-route",
        actions=[ReservedAction(action=action, required_capabilities=frozenset())],
        estimated_cost=1.0,
    )


def test_build_planning_context_wires_registry_and_tools(tmp_path):
    from oolu.assembly import build_planning_context
    from oolu.skills.discovery import DiscoveredTool
    from oolu.skills.registry import SkillRegistry

    assert build_planning_context() is None

    reg = SkillRegistry(tmp_path / "reg.db")
    reg.register(
        ReusableSkill(
            name="Extract Table",
            description="extract a table",
            signature=SkillSignature(application="web", adapter="browser"),
            actions=[
                ActionEvent(correlation_id="c", adapter="browser", operation="run")
            ],
        ),
        semver="1.0.0",
        tags=["table", "extract"],
    )
    tools = [
        DiscoveredTool(name="jq", path="/usr/bin/jq", category="data", tags=["json"])
    ]
    try:
        provider = build_planning_context(registry=reg, tools=tools)
        assert provider is not None
        text = provider("extract a table and filter json")
        assert "Extract Table" in text
        assert "jq" in text
    finally:
        reg.close()


def _host(tmp_path, **kwargs):
    """The unified runtime, signed in — every port of the old desktop
    runtime tests now drives the same gateway surface the shell uses."""
    runtime = build_host_runtime(
        data_dir=tmp_path / "host",
        secret="a-thirty-two-character-plus-signing-secret",
        **kwargs,
    )
    runtime.accounts.bootstrap(tenant="main", username="admin", password="first-pass")
    token = runtime.accounts.login("admin", "first-pass").token
    return runtime, token


def _call(runtime, token, method, path, body=None):
    from oolu.gateway.http import Request

    headers = {"Authorization": f"Bearer {token}"}
    return runtime.gateway.handle(
        Request(method=method, path=path, headers=headers, query={}, body=body)
    )


def _submit(runtime, token, intent):
    submitted = _call(runtime, token, "POST", "/v1/runs", {"intent": intent})
    assert submitted.status in (200, 201, 202), submitted.body
    run_id = submitted.body["run_id"]
    return _call(runtime, token, "GET", f"/v1/runs/{run_id}").body


def test_planning_only_runtime_fails_with_no_route(tmp_path):
    runtime, token = _host(tmp_path)
    try:
        run = _submit(runtime, token, "do something")
        assert run["phase"] == "failed"
        assert "no executable route" in (run["failure_reason"] or "")
    finally:
        runtime.close()


def test_model_intake_drives_clarification_through_the_runtime(tmp_path):
    answer = (
        '{"parameters": [{"name": "city", "value_type": "string", '
        '"required": true, "question": "Which city?"}]}'
    )
    runtime, token = _host(tmp_path, intake_model=_Model(answer))
    try:
        run = _submit(runtime, token, "book a flight")
        assert run["awaiting"] == "clarification"
        questions = _call(
            runtime, token, "GET", f"/v1/runs/{run['run_id']}/questions"
        ).body
        assert [q["parameter"] for q in questions["questions"]] == ["city"]

        resumed = _call(
            runtime,
            token,
            "POST",
            f"/v1/runs/{run['run_id']}/answers",
            {"answers": {"city": "Lisbon"}},
        ).body
        assert resumed["phase"] == "failed"
        assert "no executable route" in (resumed["failure_reason"] or "")
    finally:
        runtime.close()


def test_full_path_runs_end_to_end_with_injected_planner(tmp_path):
    runtime, token = _host(
        tmp_path, blueprints=[_echo_blueprint()], executors={"local": _Executor()}
    )
    try:
        run = _submit(runtime, token, "echo please")
        assert run["phase"] == "completed"
        assert run["result"] == {
            "status": "succeeded",
            "attempts": 1,
            "route": "echo-route",
            "actions": 1,
        }
    finally:
        runtime.close()


def _cli_run_skill(argv):
    return ReusableSkill(
        name="cli-task",
        description="run a command",
        signature=SkillSignature(application="cli", adapter="cli"),
        actions=[
            ActionEvent(
                correlation_id="c1",
                adapter="cli",
                operation="run",
                parameters={"argv": argv},
            )
        ],
    )


def test_registry_and_cli_executor_run_a_real_command(tmp_path):
    if shutil.which("true") is None:
        pytest.skip("no `true` binary")
    runtime, token = _host(
        tmp_path,
        skills=[_cli_run_skill(["true"])],
        executors=build_cli_executor(workspace=tmp_path, allowed_executables=["true"]),
    )
    try:
        run = _submit(runtime, token, "run the thing")
        assert run["awaiting"] == "confirmation"

        done = _call(
            runtime,
            token,
            "POST",
            f"/v1/runs/{run['run_id']}/confirmation",
            {"approved": True},
        ).body
        assert done["phase"] == "completed"
        assert done["result"]["status"] == "succeeded"
        assert done["result"]["route"] == "cli-task"
    finally:
        runtime.close()


def test_runs_survive_reopening_the_same_data_directory(tmp_path):
    runtime, token = _host(tmp_path)
    run_id = _submit(runtime, token, "persist me")["run_id"]
    runtime.close()

    # Same directory, same secret: the run and the sign-in both survive.
    reopened = build_host_runtime(
        data_dir=tmp_path / "host",
        secret="a-thirty-two-character-plus-signing-secret",
    )
    try:
        token = reopened.accounts.login("admin", "first-pass").token
        run = _call(reopened, token, "GET", f"/v1/runs/{run_id}").body
        assert run["run_id"] == run_id
    finally:
        reopened.close()
