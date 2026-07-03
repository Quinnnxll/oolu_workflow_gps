from __future__ import annotations

from workflow_gps.skills.learner import SkillLearner, scrub_demonstration
from workflow_gps.skills.models import (
    ActionEvent,
    Demonstration,
    ExecutionOutcome,
    ExecutionStatus,
    StateSnapshot,
)
from workflow_gps.skills.registry import SkillRegistry


class _CliExecutor:
    name = "cli"

    def __init__(self, *, fail=False):
        self._fail = fail

    def capabilities(self):
        return frozenset({"run"})

    def execute(self, action, *, idempotency_key):
        status = ExecutionStatus.FAILED if self._fail else ExecutionStatus.SUCCEEDED
        return ExecutionOutcome(
            idempotency_key=idempotency_key,
            skill_id=action.correlation_id,
            status=status,
            error="sandbox failure" if self._fail else None,
        )


def _demo(intent="produce the report", argv=("cp", "in", "out.txt")):
    return Demonstration(
        intent=intent,
        actions=[
            ActionEvent(
                correlation_id="c",
                adapter="cli",
                operation="run",
                parameters={"argv": list(argv)},
            )
        ],
        before=StateSnapshot(fingerprint="f0", state={"files": {}}),
        after=StateSnapshot(
            fingerprint="f1", state={"files": {"out.txt": {"sha": "abc", "size": 3}}}
        ),
        outcome=ExecutionStatus.SUCCEEDED,
    )


def _registry(tmp_path):
    return SkillRegistry(tmp_path / "reg.db")


def test_scrub_demonstration_masks_pii_everywhere():
    demo = _demo(intent="email report to alice@corp.com", argv=("send", "bob@corp.com"))
    scrubbed = scrub_demonstration(demo)
    assert "alice@corp.com" not in scrubbed.intent
    assert "<EMAIL>" in scrubbed.intent
    assert scrubbed.actions[0].parameters["argv"] == ["send", "<EMAIL>"]


def test_learn_registers_on_verified_sandbox_success(tmp_path):
    reg = _registry(tmp_path)
    learner = SkillLearner(reg, executors={"cli": _CliExecutor()})
    try:
        learned = learner.learn(
            _demo(), name="Make Report", description="make a report"
        )
        assert learned.status == "registered"
        assert learned.verification.verified is True
        assert learned.registered.skill_id == "learned.make.report"
        assert reg.get("learned.make.report") is not None
    finally:
        reg.close()


def test_unverified_skill_is_not_registered(tmp_path):
    reg = _registry(tmp_path)
    learner = SkillLearner(reg, executors={"cli": _CliExecutor(fail=True)})
    try:
        learned = learner.learn(
            _demo(), name="Bad Skill", description="fails in sandbox"
        )
        assert learned.status == "unverified"
        assert learned.verification.verified is False
        assert reg.list() == []
    finally:
        reg.close()


def test_no_sandbox_executor_blocks_registration(tmp_path):
    reg = _registry(tmp_path)
    learner = SkillLearner(reg)
    try:
        learned = learner.learn(_demo(), name="X", description="x")
        assert learned.status == "unverified"
        assert "no sandbox executor" in learned.reason
        assert reg.list() == []
    finally:
        reg.close()


def test_verify_disabled_registers_directly(tmp_path):
    reg = _registry(tmp_path)
    learner = SkillLearner(reg)
    try:
        learned = learner.learn(_demo(), name="Trusted", description="t", verify=False)
        assert learned.status == "registered"
        assert learned.verification.verified is None
    finally:
        reg.close()


def test_compile_failure_when_no_artifacts(tmp_path):
    reg = _registry(tmp_path)
    learner = SkillLearner(reg, executors={"cli": _CliExecutor()})
    demo = _demo().model_copy(update={"after": None})
    try:
        learned = learner.learn(demo, name="X", description="x")
        assert learned.status == "compile_failed"
        assert reg.list() == []
    finally:
        reg.close()


def test_relearning_is_idempotent(tmp_path):
    reg = _registry(tmp_path)
    learner = SkillLearner(reg, executors={"cli": _CliExecutor()})
    try:
        first = learner.learn(_demo(), name="Make Report", description="make a report")
        second = learner.learn(_demo(), name="Make Report", description="make a report")
        assert first.registered.skill_id == second.registered.skill_id
        assert len(reg.list()) == 1
    finally:
        reg.close()
