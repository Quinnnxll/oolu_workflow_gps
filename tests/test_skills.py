"""Contracts for portable skill stores and the requirement compiler."""

from __future__ import annotations

import io
import json
import sys

import pytest

from workflow_gps.cli import main
from workflow_gps.skills import (
    ActionEvent,
    AuthorizationGrant,
    AuthorizationMode,
    BriefStatus,
    CliActionExecutor,
    CliExecutionPolicy,
    CliSkillRecorder,
    ConstraintSeverity,
    ConstraintSpec,
    ConstraintStatus,
    ExecutionOutcome,
    ExecutionStatus,
    InMemoryExecutionStore,
    InMemorySkillStore,
    LocalExecutionStore,
    LocalSkillStore,
    ParameterDomain,
    ParameterSource,
    RemoteMockExecutionStore,
    RemoteMockSkillStore,
    RequirementBrief,
    RequirementConstraintCompiler,
    RequirementParameter,
    ReusableSkill,
    SafeSkillRuntime,
    SkillSignature,
    StaticApprovalProvider,
    WorkspaceConstraintValidator,
    WorkspaceProbe,
)


def _skill(skill_id: str = "skill-one") -> ReusableSkill:
    return ReusableSkill(
        id=skill_id,
        name="Create a harmless marker",
        description="Contract-test skill",
        signature=SkillSignature(
            application="test-app",
            application_version="1",
            adapter="fake",
        ),
        actions=[
            ActionEvent(
                correlation_id="demo-one",
                adapter="fake",
                operation="create_marker",
                parameters={"name": "done"},
            )
        ],
        validators=[
            ConstraintSpec(
                id="marker-exists",
                description="Marker must exist",
                validator="fake.marker_exists",
            )
        ],
    )


@pytest.fixture(params=["memory", "local", "remote"])
def skill_store(request, tmp_path):
    if request.param == "memory":
        store = InMemorySkillStore()
    elif request.param == "local":
        store = LocalSkillStore(tmp_path / "skills.db")
    else:
        store = RemoteMockSkillStore()
    yield store
    store.close()


def test_skill_store_contract(skill_store):
    first = _skill()
    second = _skill("skill-two").model_copy(update={"name": "Second skill"})

    skill_store.save(first)
    skill_store.save(second)

    assert skill_store.get(first.id) == first
    assert {skill.id for skill in skill_store.list()} == {first.id, second.id}
    assert skill_store.delete(first.id) is True
    assert skill_store.get(first.id) is None
    assert skill_store.delete(first.id) is False


@pytest.fixture(params=["memory", "local", "remote"])
def execution_store(request, tmp_path):
    if request.param == "memory":
        store = InMemoryExecutionStore()
    elif request.param == "local":
        store = LocalExecutionStore(tmp_path / "outcomes.db")
    else:
        store = RemoteMockExecutionStore()
    yield store
    store.close()


def test_execution_store_contract_and_skill_scoping(execution_store):
    outcome = ExecutionOutcome(
        idempotency_key="request-one",
        skill_id="skill-one",
        status=ExecutionStatus.SUCCEEDED,
    )
    execution_store.save(outcome)

    assert execution_store.get("skill-one", "request-one") == outcome
    assert execution_store.get("skill-two", "request-one") is None


def _cup_brief(*, authorization=None, diameter=None, constraint_status=None):
    return RequirementBrief(
        intent="Design a cup",
        parameters=[
            RequirementParameter(
                name="capacity",
                description="Usable liquid capacity",
                domain=ParameterDomain(
                    value_type="number", unit="mL", minimum=100, maximum=1000
                ),
                suggested_values=[220, 350, 500],
                question="What capacity should the cup hold?",
                question_priority=100,
            ),
            RequirementParameter(
                name="inner_diameter",
                description="Inside diameter",
                domain=ParameterDomain(
                    value_type="number", unit="mm", minimum=40, maximum=120
                ),
                value=diameter,
                source=ParameterSource.USER
                if diameter is not None
                else ParameterSource.UNRESOLVED,
                suggested_values=[60],
                question="What diameter range is acceptable?",
                question_priority=50,
            ),
        ],
        constraints=[
            ConstraintSpec(
                id="positive-volume",
                description="Internal volume must be positive",
                validator="cad.positive_volume",
                severity=ConstraintSeverity.HARD,
                status=constraint_status or ConstraintStatus.UNRESOLVED,
            )
        ],
        authorization=authorization or AuthorizationGrant(),
    )


def test_compiler_never_binds_suggested_cup_diameter():
    result = RequirementConstraintCompiler().compile(_cup_brief())

    assert result.status is BriefStatus.CLARIFYING
    assert result.unresolved_parameters == ["capacity", "inner_diameter"]
    assert result.questions[0].parameter == "capacity"
    assert result.questions[1].suggested_values == [60]
    assert result.can_generate_candidates is False


def test_explicit_delegation_allows_solver_but_not_production():
    authorization = AuthorizationGrant(
        mode=AuthorizationMode.AUTONOMOUS_WITHIN_BOUNDS,
        allow_all_unspecified=True,
        objectives=["minimize material"],
        reproducibility_seed=7,
    )
    result = RequirementConstraintCompiler().compile(
        _cup_brief(authorization=authorization)
    )

    assert result.status is BriefStatus.READY_FOR_SOLVER
    assert set(result.delegated_parameters) == {"capacity", "inner_diameter"}
    assert result.can_generate_candidates is True
    assert result.can_produce is False


def test_compiler_requires_hard_constraints_to_pass_before_production():
    brief = _cup_brief(
        diameter=75,
        constraint_status=ConstraintStatus.SATISFIED,
    )
    brief = brief.model_copy(
        update={
            "parameters": [
                brief.parameters[0].model_copy(
                    update={"value": 350, "source": ParameterSource.USER}
                ),
                brief.parameters[1],
            ]
        }
    )

    result = RequirementConstraintCompiler().compile(brief)

    assert result.status is BriefStatus.READY_FOR_PRODUCTION
    assert result.can_produce is True


def test_violated_hard_constraint_blocks_even_delegated_design():
    result = RequirementConstraintCompiler().compile(
        _cup_brief(
            authorization=AuthorizationGrant(mode=AuthorizationMode.FULLY_DELEGATED),
            constraint_status=ConstraintStatus.VIOLATED,
        )
    )
    assert result.status is BriefStatus.BLOCKED
    assert result.violated_hard_constraints == ["positive-volume"]


def test_skill_cli_list_inspect_and_safe_replay(tmp_path):
    db = tmp_path / "skills.db"
    store = LocalSkillStore(db)
    store.save(_skill())
    store.close()

    listing = io.StringIO()
    assert main(["skill-list", "--skill-db", str(db), "--json"], out=listing) == 0
    assert json.loads(listing.getvalue())[0]["id"] == "skill-one"

    inspected = io.StringIO()
    assert (
        main(["skill-inspect", "skill-one", "--skill-db", str(db)], out=inspected) == 0
    )
    assert json.loads(inspected.getvalue())["name"] == "Create a harmless marker"

    assert (
        main(["skill-replay", "skill-one", "--skill-db", str(db)], out=io.StringIO())
        == 2
    )
    replayed = io.StringIO()
    assert (
        main(
            ["skill-replay", "skill-one", "--skill-db", str(db), "--dry-run"],
            out=replayed,
        )
        == 0
    )
    plan = json.loads(replayed.getvalue())
    assert plan["mode"] == "dry-run" and plan["note"] == "No executor was invoked."


_TRANSFORM_CODE = (
    "from pathlib import Path; "
    "Path('output.txt').write_text(Path('input.txt').read_text().upper())"
)


def _cli_policy(workspace):
    return CliExecutionPolicy.create(
        workspace=workspace,
        allowed_executables=[sys.executable],
        timeout_s=10,
    )


def test_record_delete_and_replay_exact_cli_skill(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "input.txt").write_text("hello", encoding="utf-8")
    policy = _cli_policy(workspace)
    skill, demonstration = CliSkillRecorder(policy, WorkspaceProbe(workspace)).record(
        [sys.executable, "-c", _TRANSFORM_CODE],
        name="Uppercase text",
        description="Create uppercase output",
        approved_write=True,
    )
    assert demonstration.outcome.value == "succeeded"
    assert (workspace / "output.txt").read_text(encoding="utf-8") == "HELLO"
    (workspace / "output.txt").unlink()

    denied = SafeSkillRuntime(
        executors={"cli": CliActionExecutor(policy)},
        validators={"workspace": WorkspaceConstraintValidator()},
        probe=WorkspaceProbe(workspace),
        approval=StaticApprovalProvider(False),
    ).run(skill, idempotency_key="denied")
    assert denied.status.value == "blocked"
    assert not (workspace / "output.txt").exists()

    runtime = SafeSkillRuntime(
        executors={"cli": CliActionExecutor(policy)},
        validators={"workspace": WorkspaceConstraintValidator()},
        probe=WorkspaceProbe(workspace),
        approval=StaticApprovalProvider(True),
    )
    outcome = runtime.run(skill, idempotency_key="approved")
    assert outcome.status.value == "succeeded"
    assert (workspace / "output.txt").read_text(encoding="utf-8") == "HELLO"

    (workspace / "output.txt").write_text("tampered", encoding="utf-8")
    assert runtime.run(skill, idempotency_key="approved") == outcome


def test_replay_blocks_when_demonstrated_input_changes(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    input_path = workspace / "input.txt"
    input_path.write_text("hello", encoding="utf-8")
    policy = _cli_policy(workspace)
    skill, _ = CliSkillRecorder(policy, WorkspaceProbe(workspace)).record(
        [sys.executable, "-c", _TRANSFORM_CODE],
        name="Uppercase text",
        description="Create uppercase output",
        approved_write=True,
    )
    (workspace / "output.txt").unlink()
    input_path.write_text("different", encoding="utf-8")

    outcome = SafeSkillRuntime(
        executors={"cli": CliActionExecutor(policy)},
        validators={"workspace": WorkspaceConstraintValidator()},
        probe=WorkspaceProbe(workspace),
        approval=StaticApprovalProvider(True),
    ).run(skill, idempotency_key="changed-input")

    assert outcome.status.value == "blocked"
    assert "precondition" in outcome.error
    assert not (workspace / "output.txt").exists()


def test_runtime_blocks_unresolved_requirements_before_action(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "input.txt").write_text("hello", encoding="utf-8")
    policy = _cli_policy(workspace)
    skill, _ = CliSkillRecorder(policy, WorkspaceProbe(workspace)).record(
        [sys.executable, "-c", _TRANSFORM_CODE],
        name="Uppercase text",
        description="Create uppercase output",
        approved_write=True,
    )
    (workspace / "output.txt").unlink()

    outcome = SafeSkillRuntime(
        executors={"cli": CliActionExecutor(policy)},
        validators={"workspace": WorkspaceConstraintValidator()},
        probe=WorkspaceProbe(workspace),
        approval=StaticApprovalProvider(True),
    ).run(skill, idempotency_key="requirements", requirements=_cup_brief())

    assert outcome.status.value == "blocked"
    assert outcome.evidence["requirements"]["status"] == "clarifying"
    assert not (workspace / "output.txt").exists()


def test_cli_policy_rejects_unlisted_executable_and_workspace_escape(tmp_path):
    policy = CliExecutionPolicy.create(
        workspace=tmp_path,
        allowed_executables=["definitely-not-python"],
    )
    with pytest.raises(ValueError, match="allow-listed"):
        policy.validate_argv([sys.executable, "-c", "print('no')"])

    policy = _cli_policy(tmp_path)
    with pytest.raises(ValueError, match="escapes workspace"):
        policy.validate_argv([sys.executable, "../outside.py"])


def test_functional_skill_record_and_run_cli(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "input.txt").write_text("hello", encoding="utf-8")
    db = tmp_path / "skills.db"
    recorded = io.StringIO()
    record_args = [
        "skill-record",
        "--name",
        "Uppercase text",
        "--workspace",
        str(workspace),
        "--allow-executable",
        sys.executable,
        "--skill-db",
        str(db),
        "--approve-write",
        "--",
        sys.executable,
        "-c",
        _TRANSFORM_CODE,
    ]
    assert main(record_args, out=recorded) == 0
    skill_id = json.loads(recorded.getvalue())["skill_id"]
    (workspace / "output.txt").unlink()

    replayed = io.StringIO()
    run_args = [
        "skill-run",
        skill_id,
        "--workspace",
        str(workspace),
        "--allow-executable",
        sys.executable,
        "--skill-db",
        str(db),
        "--approve-write",
        "--idempotency-key",
        "functional-test",
    ]
    assert main(run_args, out=replayed) == 0
    assert json.loads(replayed.getvalue())["status"] == "succeeded"
    assert (workspace / "output.txt").read_text(encoding="utf-8") == "HELLO"

    (workspace / "output.txt").write_text("tampered", encoding="utf-8")
    repeated = io.StringIO()
    assert main(run_args, out=repeated) == 0
    assert json.loads(repeated.getvalue())["idempotency_key"] == "functional-test"
    assert (workspace / "output.txt").read_text(encoding="utf-8") == "tampered"
