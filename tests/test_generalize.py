"""Slot induction: repeated demonstrations -> parameterized, portable skills."""

from __future__ import annotations

import pytest

from oolu.skills import DemonstrationCompiler, bind_parameters
from oolu.skills.models import (
    ActionEvent,
    Demonstration,
    ExecutionStatus,
    SkillSignature,
    StateSnapshot,
)

SIGNATURE = SkillSignature(application="cli", adapter="cli")


def _snapshot(workspace: str, files: dict) -> StateSnapshot:
    return StateSnapshot(
        fingerprint=f"fp-{sorted(files)}",
        state={"workspace": workspace, "files": files},
    )


def _demo(
    argv: list[str],
    *,
    workspace: str = "/home/user/ws",
    produced: str = "out/report.csv",
    outcome: ExecutionStatus = ExecutionStatus.SUCCEEDED,
) -> Demonstration:
    return Demonstration(
        intent="convert a file",
        actions=[
            ActionEvent(
                correlation_id="c",
                adapter="cli",
                operation="run",
                parameters={"argv": argv, "cwd": workspace},
            )
        ],
        before=_snapshot(workspace, {}),
        after=_snapshot(workspace, {produced: {"sha256": "x", "size": 1}}),
        outcome=outcome,
    )


def test_varying_values_become_slots_and_constants_stay():
    demos = [
        _demo(["convert", "/home/user/ws/a.txt", "--fast"]),
        _demo(["convert", "/home/user/ws/b.txt", "--fast"]),
    ]
    skill = DemonstrationCompiler().compile_generalized(
        demos, name="convert", description="", signature=SIGNATURE
    )
    assert len(skill.parameters) == 1
    param = skill.parameters[0]
    assert param.value_type == "path"
    assert param.domain["role"] == "path"
    # Observed values are workspace-templated, never the user's absolute paths.
    assert param.domain["observed"] == ["{workspace}/a.txt", "{workspace}/b.txt"]
    argv = skill.actions[0].parameters["argv"]
    assert argv[0] == "convert" and argv[2] == "--fast"  # constants survive
    assert argv[1] == {"$param": param.name}
    # The generalized skill drops the exact-mode fingerprint pin.
    assert skill.preconditions == []


def test_constant_workspace_paths_are_templated_too():
    demos = [
        _demo(["convert", "/home/user/ws/in.txt"]),
        _demo(["convert", "/home/user/ws/in.txt"]),
    ]
    skill = DemonstrationCompiler().compile_generalized(
        demos, name="convert", description="", signature=SIGNATURE
    )
    assert skill.parameters == []
    assert skill.actions[0].parameters["argv"][1] == "{workspace}/in.txt"
    assert skill.actions[0].parameters["cwd"] == "{workspace}"


def test_identical_varying_values_unify_into_one_slot():
    demos = [
        _demo(["copy", "/home/user/ws/a.txt", "/home/user/ws/a.txt"]),
        _demo(["copy", "/home/user/ws/b.txt", "/home/user/ws/b.txt"]),
    ]
    skill = DemonstrationCompiler().compile_generalized(
        demos, name="copy", description="", signature=SIGNATURE
    )
    assert len(skill.parameters) == 1
    argv = skill.actions[0].parameters["argv"]
    assert argv[1] == argv[2]  # both positions bind the same parameter


def test_common_artifacts_become_a_hard_validator():
    demos = [_demo(["run"]), _demo(["run"])]
    skill = DemonstrationCompiler().compile_generalized(
        demos, name="r", description="", signature=SIGNATURE
    )
    (validator,) = skill.validators
    assert validator.validator == "workspace.expected_artifacts"
    assert validator.evidence["expected_files"] == ["out/report.csv"]


def test_bind_parameters_produces_concrete_actions():
    demos = [
        _demo(["convert", "/home/user/ws/a.txt", "--fast"]),
        _demo(["convert", "/home/user/ws/b.txt", "--fast"]),
    ]
    skill = DemonstrationCompiler().compile_generalized(
        demos, name="convert", description="", signature=SIGNATURE
    )
    param = skill.parameters[0].name
    (action,) = bind_parameters(
        skill, {param: "{workspace}/c.txt"}, workspace="/tmp/other-ws"
    )
    assert action.parameters["argv"] == ["convert", "/tmp/other-ws/c.txt", "--fast"]
    assert action.parameters["cwd"] == "/tmp/other-ws"

    with pytest.raises(ValueError, match="missing parameter"):
        bind_parameters(skill, {}, workspace="/tmp/other-ws")
    with pytest.raises(ValueError, match="workspace"):
        bind_parameters(skill, {param: "{workspace}/c.txt"})
    with pytest.raises(ValueError, match="unknown parameter"):
        bind_parameters(skill, {param: "x", "nope": 1}, workspace="/tmp/w")


def test_learner_generalize_induces_path_slots_despite_pii_scrubbing(tmp_path):
    """The scrubber masks absolute paths; templating must run first so the
    varying path survives as {workspace}/... and still becomes a slot."""
    from oolu.skills import SkillLearner, SkillRegistry

    registry = SkillRegistry(tmp_path / "registry.db")
    learner = SkillLearner(registry)  # scrub_pii=True is the default
    demos = [
        _demo(["convert", "/home/user/ws/a.txt"]),
        _demo(["convert", "/home/user/ws/b.txt"]),
    ]
    learned = learner.generalize(demos, name="convert", description="", verify=False)
    assert learned.status == "registered", learned.reason
    (param,) = learned.skill.parameters
    assert param.value_type == "path"
    assert param.domain["observed"] == ["{workspace}/a.txt", "{workspace}/b.txt"]
    registry.close()


def test_guardrails_refuse_to_guess():
    compiler = DemonstrationCompiler()
    with pytest.raises(ValueError, match="at least two"):
        compiler.compile_generalized(
            [_demo(["a"])], name="n", description="", signature=SIGNATURE
        )
    with pytest.raises(ValueError, match="skeleton"):
        different = _demo(["a"])
        different = different.model_copy(
            update={
                "actions": [
                    ActionEvent(
                        correlation_id="c",
                        adapter="cli",
                        operation="other",
                        parameters={},
                    )
                ]
            }
        )
        compiler.compile_generalized(
            [_demo(["a"]), different], name="n", description="", signature=SIGNATURE
        )
    with pytest.raises(ValueError, match="successful"):
        compiler.compile_generalized(
            [_demo(["a"]), _demo(["b"], outcome=ExecutionStatus.FAILED)],
            name="n",
            description="",
            signature=SIGNATURE,
        )
