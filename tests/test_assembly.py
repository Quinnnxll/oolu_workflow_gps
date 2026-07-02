from __future__ import annotations

from workflow_gps.assembly import build_desktop_runtime
from workflow_gps.orchestrator.state import Blueprint, ReservedAction
from workflow_gps.skills.models import (
    ActionEvent,
    ExecutionOutcome,
    ExecutionStatus,
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


def test_planning_only_runtime_fails_with_no_route(tmp_path):
    with build_desktop_runtime(db_path=tmp_path / "d.db") as rt:
        view = rt.desktop.submit_task("do something")
        assert view.phase == "failed"
        assert "no executable route" in (view.failure_reason or "")


def test_model_intake_drives_clarification_through_the_runtime(tmp_path):
    answer = (
        '{"parameters": [{"name": "city", "value_type": "string", '
        '"required": true, "question": "Which city?"}]}'
    )
    with build_desktop_runtime(
        db_path=tmp_path / "d.db", intake_model=_Model(answer)
    ) as rt:
        view = rt.desktop.submit_task("book a flight")
        assert view.awaiting == "clarification"
        assert [q.parameter for q in view.questions] == ["city"]

        resumed = rt.desktop.answer_questions(view.run_id, {"city": "Lisbon"})
        assert resumed.phase == "failed"
        assert "no executable route" in (resumed.failure_reason or "")


def test_full_path_runs_end_to_end_with_injected_planner(tmp_path):
    with build_desktop_runtime(
        db_path=tmp_path / "d.db",
        blueprints=[_echo_blueprint()],
        executors={"local": _Executor()},
    ) as rt:
        view = rt.desktop.submit_task("echo please")
        assert view.phase == "completed"
        assert view.result == {
            "status": "succeeded",
            "attempts": 1,
            "route": "echo-route",
            "actions": 1,
        }


def test_runs_survive_reopening_the_same_db(tmp_path):
    db = tmp_path / "d.db"
    rt = build_desktop_runtime(db_path=db)
    run_id = rt.desktop.submit_task("persist me").run_id
    rt.close()

    reopened = build_desktop_runtime(db_path=db)
    try:
        assert reopened.desktop.task(run_id).run_id == run_id
    finally:
        reopened.close()
