"""One ask earns its finish (V3): the budget-bounded task loop.

Exit gate (node-vitality-plan, phase V3): under the standing delegation
("build and run under my budget") a task ask keeps moving on its own —
the worker retries mechanical incidents up the engine's own ladder (two
retries, the one rebuild, the one re-earned confirmation) — and the
REPORT states what was done, what was checked, and what it cost; a
spent task budget stops the loop in words naming the spend; judgment
(blocked gates, reserved or irreversible actions, money) always pauses
for the human; and the consent surfaces as a FIRST-RUN question instead
of a buried default.
"""

from __future__ import annotations

from types import SimpleNamespace

from test_chat_tools import _FakeModel
from test_growth_trigger import GOAL, TASK_TURN, _chat, _rig
from test_node_interact import FakeAuthor
from test_verify_at_birth import GOOD, _FailingScriptHand

from oolu.author import NodeAuthorAgent
from oolu.billing.model_calls import ModelCallMeter
from oolu.chat import ChatAssistant
from oolu.gateway.app import TASK_CAP_KEY, TASK_DELEGATION_KEY
from oolu.settings_node import SettingsNode, SettingsStore, field_for


def _speak(app, turns):
    model = _FakeModel(list(turns))
    app._tenant_model = lambda tenant: model
    return model


def _settings(app, conn):
    node = SettingsNode(SettingsStore(conn))
    app._settings = node
    return node


def _charge(meter, *, cost: float = 0.01, tokens: int = 100):
    meter.record(
        "chat.turn",
        SimpleNamespace(
            model="m",
            tier="reasoning",
            prompt_tokens=tokens,
            completion_tokens=0,
            duration_s=0.1,
        ),
    )
    # The default price table charges "reasoning" per token; the exact
    # figure doesn't matter — only that the window sees real spend.
    assert meter.total_cost() > 0 or cost == 0


# --------------------------------------------------------------------------- #
# The delegation pair: declared, implying, and asked exactly once.             #
# --------------------------------------------------------------------------- #
def test_the_delegation_settings_are_declared():
    delegation = field_for(TASK_DELEGATION_KEY)
    cap = field_for(TASK_CAP_KEY)
    assert delegation is not None and delegation.group == "account"
    assert delegation.default is False
    assert cap is not None and cap.group == "budget"
    assert cap.unit == "currency" and cap.default == 0.0


def test_delegation_implies_the_autobuild_consent(tmp_path):
    app, conn, ident, desk, _script = _rig(tmp_path)
    try:
        settings = _settings(app, conn)
        assert not app._autobuild_consented("t1", "user-1")
        settings.set("t1", TASK_DELEGATION_KEY, True, "user-1")
        assert app._autobuild_consented("t1", "user-1")
        assert app._task_delegated("t1", "user-1")
    finally:
        conn.close()


def test_answered_tells_a_stored_no_from_a_defaulted_silence(tmp_path):
    app, conn, ident, desk, _script = _rig(tmp_path)
    try:
        settings = _settings(app, conn)
        session = SimpleNamespace(tenant_id="t1", principal_id="user-1")
        assert app._delegation_unanswered(session)
        settings.set("t1", TASK_DELEGATION_KEY, False, "user-1")
        # An explicit no IS an answer — the question is never re-asked.
        assert not app._delegation_unanswered(session)
    finally:
        conn.close()


def test_the_first_buildable_task_asks_the_question_once(tmp_path):
    app, conn, ident, desk, _script = _rig(tmp_path)
    try:
        _settings(app, conn)
        _speak(app, [TASK_TURN, TASK_TURN])
        first = _chat(app, ident, f"please {GOAL}")
        assert first.status == 200
        assert "under your budget" in first.body["reply"]
        assert "(yes / no" in first.body["reply"]
        # Nothing ran: the answer decides the path.
        assert first.body["run_id"] is None
        offer = app._growth_offers.pop("t1", "user-1")
        assert offer is not None and offer[0] == "delegation"
    finally:
        conn.close()


def test_yes_stores_the_delegation_and_builds_straight_through(tmp_path):
    app, conn, ident, desk, _script = _rig(tmp_path)
    try:
        settings = _settings(app, conn)
        app._contract_executors = {}
        app._node_function_author = lambda tenant: FakeAuthor(GOOD)
        _speak(app, [TASK_TURN])
        asked = _chat(app, ident, f"please {GOAL}")
        assert "under your budget" in asked.body["reply"]
        answered = _chat(app, ident, "yes")
        assert answered.status == 200, answered.body
        assert "Delegation is on" in answered.body["reply"]
        assert "Built a NEW node" in answered.body["reply"]
        assert settings.effective("t1", "user-1")[TASK_DELEGATION_KEY] is True
        # The implied consent covers later builds too.
        assert app._autobuild_consented("t1", "user-1")
    finally:
        conn.close()


def test_no_is_recorded_and_never_asked_again(tmp_path):
    app, conn, ident, desk, _script = _rig(tmp_path)
    try:
        settings = _settings(app, conn)
        _speak(app, [TASK_TURN, TASK_TURN])
        _chat(app, ident, f"please {GOAL}")
        refused = _chat(app, ident, "no")
        assert "ask each time" in refused.body["reply"]
        assert (
            settings.effective("t1", "user-1")[TASK_DELEGATION_KEY] is False
        )
        # The next task ask goes the classic way — no second question.
        again = _chat(app, ident, f"please {GOAL}")
        assert "under your budget" not in again.body["reply"]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The loop: mechanical retries up the ladder, judgment for the human.          #
# --------------------------------------------------------------------------- #
def _standing_node(app, conn, ident, *, delegated: bool):
    """A published node whose runs FAIL — the loop's test bench."""
    settings = _settings(app, conn)
    if delegated:
        settings.set("t1", TASK_DELEGATION_KEY, True, "user-1")
    app._contract_executors = {}  # static walls only: the build publishes
    app._node_function_author = lambda tenant: FakeAuthor(GOOD)
    built = _chat(app, ident, f"build me a node that {GOAL}")
    assert "Built a NEW node" in built.body["reply"], built.body
    return settings


def test_the_worker_retries_a_mechanical_incident_up_the_ladder(tmp_path):
    app, conn, ident, desk, _script = _rig(
        tmp_path, script_exec=_FailingScriptHand()
    )
    try:
        _standing_node(app, conn, ident, delegated=True)
        _speak(app, [TASK_TURN])
        asked = _chat(app, ident, f"please {GOAL}")
        run_id = asked.body["run_id"]
        assert run_id
        app.drive_queue()
        state = app._durable.get(run_id)
        # The loop pressed Retry twice on the standing delegation, the
        # engine's rebuild had its (refused — no rebuilder here) shot,
        # and only THEN did the human hear about it.
        assert state.user_retries == 2
        retries = [
            r
            for r in app._durable.audit.records()
            if r.event_type == "run.auto_retry"
            and r.payload.get("run_id") == run_id
        ]
        assert len(retries) == 2
        turns = app._assistant_history.history(tenant="t1", principal="user-1")
        report = turns[-1]["body"]
        assert "didn't make it" in report
        assert "retried 2 times on your standing delegation" in report
    finally:
        conn.close()


def test_without_the_delegation_the_first_incident_reports(tmp_path):
    app, conn, ident, desk, _script = _rig(
        tmp_path, script_exec=_FailingScriptHand()
    )
    try:
        settings = _standing_node(app, conn, ident, delegated=False)
        settings.set("t1", "account.autobuild_consent", True, "user-1")
        _speak(app, [TASK_TURN])
        asked = _chat(app, ident, f"please {GOAL}")
        run_id = asked.body["run_id"]
        app.drive_queue()
        state = app._durable.get(run_id)
        assert state.user_retries == 0
        assert [
            r
            for r in app._durable.audit.records()
            if r.event_type == "run.auto_retry"
        ] == []
        turns = app._assistant_history.history(tenant="t1", principal="user-1")
        assert "didn't make it" in turns[-1]["body"]
    finally:
        conn.close()


def test_a_spent_task_budget_stops_the_loop_naming_the_spend(tmp_path):
    app, conn, ident, desk, _script = _rig(
        tmp_path, script_exec=_FailingScriptHand()
    )
    try:
        settings = _standing_node(app, conn, ident, delegated=True)
        settings.set("t1", TASK_CAP_KEY, 0.000001)
        meter = ModelCallMeter()
        app._model_meter = meter
        _speak(app, [TASK_TURN])
        asked = _chat(app, ident, f"please {GOAL}")
        run_id = asked.body["run_id"]
        _charge(meter)  # the ask's window now exceeds the tiny cap
        app.drive_queue()
        state = app._durable.get(run_id)
        assert state.user_retries == 0  # no retry past the budget
        turns = app._assistant_history.history(tenant="t1", principal="user-1")
        report = turns[-1]["body"]
        assert "reached your task budget" in report
        assert "$" in report
    finally:
        conn.close()


def test_judgment_shapes_never_auto_retry():
    from oolu.gateway import GatewayApp
    from oolu.orchestrator import (
        Blueprint,
        MonitorReport,
        Phase,
        ReservedAction,
        RoutePlan,
        RunState,
        TaskContract,
    )
    from oolu.skills.models import ActionEvent, ExecutionStatus

    def state_with(*, reserved=False, risk="write", params=None, blocked=False):
        action = ActionEvent(
            correlation_id="c1",
            adapter="test",
            operation="run",
            parameters=dict(params or {}),
        )
        route = RoutePlan(
            chosen=Blueprint(
                name="r",
                actions=[
                    ReservedAction(
                        action=action, reserved=reserved, risk=risk
                    )
                ],
                estimated_cost=0.0,
            ),
            alternatives=[],
            total_cost=0.0,
        )
        state = RunState(intent="x", contract=TaskContract(intent="x"))
        state.route = route
        if blocked:
            state.monitoring = MonitorReport(
                healthy=False, status=ExecutionStatus.BLOCKED, signals={}
            )
        return state

    mechanical = GatewayApp._incident_is_mechanical
    assert mechanical(state_with())
    assert not mechanical(state_with(reserved=True))
    assert not mechanical(state_with(risk="irreversible"))
    assert not mechanical(
        state_with(params={"merchant": "m", "amount_micros": 100})
    )
    assert not mechanical(state_with(blocked=True))
    assert state_with().phase is Phase.INTAKE  # the fabrications are real


# --------------------------------------------------------------------------- #
# The report: reviewed, costed, honest.                                        #
# --------------------------------------------------------------------------- #
def test_the_report_carries_the_asks_cost(tmp_path):
    app, conn, ident, desk, _script = _rig(tmp_path)
    try:
        meter = ModelCallMeter()
        app._model_meter = meter
        _charge(meter, tokens=250)
        note = app._ask_cost_note({"charges_before": 0})
        assert "≈250 tokens" in note
        assert "$" in note
        # A mark AFTER the charge sees nothing — the window is the ask's.
        assert app._ask_cost_note({"charges_before": 1}) == ""
    finally:
        conn.close()


def test_the_review_checks_the_promised_artifacts(tmp_path):
    from oolu.orchestrator import Phase, RunState, TaskContract
    from oolu.orchestrator.state import ExecutionRecord
    from oolu.skills.models import (
        ExecutionOutcome,
        ExecutionStatus,
    )

    app, conn, ident, desk, _script = _rig(tmp_path)
    try:
        from datetime import UTC, datetime

        from oolu.durable.files import UserFile
        from oolu.reminders import ReminderStore

        app._reminders = ReminderStore(conn)

        now = datetime(2026, 8, 5, 9, 0, tzinfo=UTC)
        # The run PROMISED two files; only one landed in the drawer.
        app._files.save(
            UserFile(
                tenant_id="t1",
                owner="user-1",
                node_id="n-1",
                name="report.csv",
                folder="records",
                media_type="text/csv",
                content="a,b\n",
            )
        )
        state = RunState(
            intent=GOAL,
            contract=TaskContract(
                intent=GOAL,
                submitted_by="user-1",
                metadata={
                    "tenant_id": "t1",
                    "node_function": {"node_id": "n-1", "script": "x"},
                },
            ),
        )
        state.phase = Phase.COMPLETED
        state.execution = ExecutionRecord(
            idempotency_key="k",
            status=ExecutionStatus.SUCCEEDED,
            attempt=1,
            action_outcomes=[
                ExecutionOutcome(
                    idempotency_key="k",
                    skill_id="s",
                    status=ExecutionStatus.SUCCEEDED,
                    evidence={
                        "result": {
                            "result": "done",
                            "files": {
                                "report.csv": "a,b\n",
                                "missing.csv": "c\n",
                            },
                            "reminder": {"text": "x", "day": "2026-08-06"},
                        }
                    },
                    started_at=now,
                    completed_at=now,
                )
            ],
        )
        note = app._review_note(state)
        assert "report.csv" in note and "in the node's drawer" in note
        assert "MISSING" in note and "missing.csv" in note
        # The promised reminder was never filed — said, not assumed.
        assert "the reminder was not filed" in note
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The scaled floors.                                                           #
# --------------------------------------------------------------------------- #
def test_the_author_stops_at_a_spent_budget_in_words():
    agent = NodeAuthorAgent(object(), budget_left=lambda: 0.0)
    authored = agent.author("do the thing")
    assert authored.script is None
    assert "task budget" in authored.refusal
    assert authored.consultations == 0


def test_tool_rounds_scale_past_the_floor_when_the_caller_says_so(tmp_path):
    from test_chat_tools import _tools

    tools, _, conn = _tools(tmp_path)
    try:
        model = _FakeModel(['{"tool": "list_files", "args": {}}'] * 10)
        turn = ChatAssistant(model=model).respond(
            "loop forever", tools=tools, tool_rounds=6
        )
        assert "tangled" in turn.say
        assert len(model.calls) == 6
        # The floor still binds a caller asking for LESS.
        model = _FakeModel(['{"tool": "list_files", "args": {}}'] * 10)
        turn = ChatAssistant(model=model).respond(
            "loop forever", tools=tools, tool_rounds=2
        )
        assert len(model.calls) == 4
    finally:
        conn.close()
