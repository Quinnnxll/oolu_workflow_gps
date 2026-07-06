from __future__ import annotations

from datetime import UTC, datetime, timedelta

from oolu.skills.learner import SkillLearner
from oolu.skills.models import (
    ActionEvent,
    ExecutionOutcome,
    ExecutionStatus,
    StateSnapshot,
)
from oolu.skills.recorder import (
    DemonstrationRecorder,
    DurableAuditLogSource,
    InMemoryLogSource,
    LogEntry,
    Recording,
    RecordingMetrics,
    select_best,
)
from oolu.skills.registry import SkillRegistry

_T0 = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)


class _Observer:
    def __init__(self, actions):
        self._actions = tuple(actions)

    def observe(self):
        return self._actions


class _StepClock:
    def __init__(self, start=_T0, step=1.0):
        self._t = start
        self._step = timedelta(seconds=step)

    def __call__(self):
        now = self._t
        self._t = self._t + self._step
        return now


class _StatefulProbe:
    """Returns an empty workspace first, then one with a produced artifact."""

    def __init__(self):
        self._calls = 0

    def capture(self):
        self._calls += 1
        if self._calls == 1:
            return StateSnapshot(fingerprint="f0", state={"files": {}})
        return StateSnapshot(
            fingerprint="f1", state={"files": {"out.txt": {"sha": "a", "size": 1}}}
        )


def _action(op, at):
    return ActionEvent(correlation_id="c", adapter="cli", operation=op, observed_at=at)


def test_correlates_gui_actions_with_backend_logs():
    actions = [_action("run", _T0 + timedelta(seconds=0.5))]
    logs = InMemoryLogSource(
        [
            LogEntry(
                at=_T0 + timedelta(seconds=0.6),
                source="audit",
                event_type="workflow.executed",
            ),
            LogEntry(at=_T0 + timedelta(seconds=2), source="audit", event_type="late"),
        ]
    )
    recorder = DemonstrationRecorder(
        _Observer(actions), log_source=logs, clock=_StepClock(step=1.0)
    )
    recorder.start()  # t=T0
    recording = recorder.stop(intent="do the thing")  # t=T0+1s

    assert isinstance(recording, Recording)
    assert recording.metrics.action_count == 1
    # Only the in-window log (t+0.6s) is captured; the t+2s one is outside.
    assert recording.metrics.backend_event_count == 1
    assert recording.metrics.succeeded is True
    assert recording.metrics.duration_s == 1.0
    kinds = [item["kind"] for item in recording.timeline]
    assert kinds == ["action", "log"]
    assert recording.demonstration.evidence["metrics"]["succeeded"] is True


def test_backend_error_makes_recording_unreliable():
    logs = InMemoryLogSource(
        [
            LogEntry(
                at=_T0 + timedelta(seconds=0.5),
                source="audit",
                event_type="workflow.incident",
            )
        ]
    )
    recorder = DemonstrationRecorder(
        _Observer([_action("run", _T0)]), log_source=logs, clock=_StepClock()
    )
    recorder.start()
    recording = recorder.stop(intent="risky op")
    assert recording.metrics.error_count == 1
    assert recording.metrics.succeeded is False
    assert recording.demonstration.outcome is ExecutionStatus.FAILED


def test_durable_audit_log_source_windows_by_time():
    class _Rec:
        def __init__(self, at, event_type, run_id, seq):
            self.at, self.event_type, self.run_id, self.seq = (
                at,
                event_type,
                run_id,
                seq,
            )

    class _Audit:
        def records(self):
            return [
                _Rec(_T0 - timedelta(seconds=1), "before", "r", 1),
                _Rec(_T0 + timedelta(seconds=1), "workflow.executed", "r", 2),
                _Rec(_T0 + timedelta(seconds=100), "after", "r", 3),
            ]

    source = DurableAuditLogSource(_Audit())
    entries = source.entries(since=_T0, until=_T0 + timedelta(seconds=10))
    assert [e.event_type for e in entries] == ["workflow.executed"]
    assert entries[0].detail == {"seq": 2}


def test_select_best_picks_fastest_reliable():
    def _rec(*, succeeded, errors, duration, actions):
        return Recording(
            demonstration=_min_demo(),
            metrics=RecordingMetrics(
                duration_s=duration,
                action_count=actions,
                backend_event_count=1,
                error_count=errors,
                succeeded=succeeded,
            ),
        )

    slow = _rec(succeeded=True, errors=0, duration=9.0, actions=5)
    fast = _rec(succeeded=True, errors=0, duration=3.0, actions=4)
    broken = _rec(succeeded=False, errors=2, duration=1.0, actions=1)
    assert select_best([slow, fast, broken]) is fast
    assert select_best([broken]) is None


def test_recording_feeds_the_learner(tmp_path):
    actions = [_action("run", _T0 + timedelta(seconds=0.2))]
    logs = InMemoryLogSource(
        [
            LogEntry(
                at=_T0 + timedelta(seconds=0.3),
                source="audit",
                event_type="workflow.executed",
            )
        ]
    )
    recorder = DemonstrationRecorder(
        _Observer(actions),
        log_source=logs,
        probe=_StatefulProbe(),
        clock=_StepClock(),
    )
    recorder.start()
    recording = recorder.stop(intent="make the artifact")

    reg = SkillRegistry(tmp_path / "reg.db")

    class _Exec:
        name = "cli"

        def capabilities(self):
            return frozenset({"run"})

        def execute(self, action, *, idempotency_key):
            return ExecutionOutcome(
                idempotency_key=idempotency_key,
                skill_id=action.correlation_id,
                status=ExecutionStatus.SUCCEEDED,
            )

    try:
        learned = SkillLearner(reg, executors={"cli": _Exec()}).learn(
            recording.demonstration,
            name="Recorded Task",
            description="from a recording",
        )
        assert learned.status == "registered"
    finally:
        reg.close()


def _min_demo():
    from oolu.skills.models import Demonstration

    return Demonstration(intent="x", actions=[], outcome=ExecutionStatus.SUCCEEDED)
