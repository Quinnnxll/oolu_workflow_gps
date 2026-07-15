"""Issue 8: the work actually completes — reminders, build-first, reuse.

"Remind me" stops being a doomed workflow: a reminder is a ROW with a
clock, created deterministically and confirmed from the stored row, rung
in the conversation by the client's poll. A task whose route has no node
yet is built FIRST under standing consent — the node born WITH its
function, on the desk — instead of firing a run that must fail. Run
again reuses the node, never recreates it. And self-built code the
user's credit paid for (the LLM rebuild) persists as a real node in
Work → My nodes, not an artifact buried in one run's log.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from test_chat_assistant import _FakeModel
from test_growth_trigger import GOAL, TASK_TURN, _chat, _rig, _speak_work
from test_http_gateway import _req

from oolu.chat import ChatAssistant
from oolu.durable import DurableConnection
from oolu.reminders import ReminderStore

# Matches test_http_gateway.NOW so request-stamped clocks agree.
NOW = datetime(2026, 6, 29, 12, 0, tzinfo=UTC)


class _Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


# --------------------------------------------------------------------------- #
# The store: rows with a clock, delivered exactly once.                        #
# --------------------------------------------------------------------------- #
def test_the_store_keeps_due_and_upcoming_and_rings_once(tmp_path):
    clock = _Clock()
    conn = DurableConnection(tmp_path / "d.db")
    try:
        store = ReminderStore(conn, clock=clock)
        soon = store.add(
            tenant="t1",
            principal="u1",
            text="drink water",
            due_at=NOW + timedelta(minutes=20),
        )
        later = store.add(
            tenant="t1",
            principal="u1",
            text="stretch",
            due_at=NOW + timedelta(hours=3),
        )
        assert store.due(tenant="t1", principal="u1") == []
        assert [r.reminder_id for r in store.upcoming(tenant="t1", principal="u1")] == [
            soon.reminder_id,
            later.reminder_id,
        ]
        # The clock passes: the first ripens; another account sees nothing.
        clock.now = NOW + timedelta(minutes=30)
        assert [r.text for r in store.due(tenant="t1", principal="u1")] == [
            "drink water"
        ]
        assert store.due(tenant="t1", principal="u2") == []
        # Delivered exactly once — a second marking returns None.
        assert store.mark_delivered(
            soon.reminder_id, tenant="t1", principal="u1"
        ) is not None
        assert store.mark_delivered(
            soon.reminder_id, tenant="t1", principal="u1"
        ) is None
        assert store.due(tenant="t1", principal="u1") == []
        # Refusals, in words.
        with pytest.raises(ValueError, match="already passed"):
            store.add(
                tenant="t1", principal="u1", text="x", due_at=NOW - timedelta(1)
            )
        with pytest.raises(ValueError, match="needs words"):
            store.add(
                tenant="t1", principal="u1", text="  ",
                due_at=clock.now + timedelta(minutes=1),
            )
        with pytest.raises(ValueError, match="a year"):
            store.add(
                tenant="t1", principal="u1", text="x",
                due_at=clock.now + timedelta(days=400),
            )
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The deterministic chat path: no model, no engine, a stored row.              #
# --------------------------------------------------------------------------- #
class _MustNotSpeak:
    def reply(self, messages):  # pragma: no cover - the point is silence
        raise AssertionError("an explicit reminder must not reach the model")


class _Hands:
    """Reminder hands like the gateway's, over a real store."""

    def __init__(self, store, now):
        self.store, self.now = store, now

    def reminder_in(self, text, minutes):
        try:
            r = self.store.add(
                tenant="t1", principal="u1", text=text,
                due_at=self.now + timedelta(minutes=int(minutes)),
            )
        except ValueError as exc:
            return f"error: {exc}"
        return f"Reminder set — {r.due_at:%H:%M}: “{r.text}”."

    def reminder_at(self, text, hour, minute, ampm):
        if ampm == "pm" and hour < 12:
            hour += 12
        due = self.now.replace(hour=hour, minute=minute)
        if due <= self.now:
            due += timedelta(days=1)
        r = self.store.add(tenant="t1", principal="u1", text=text, due_at=due)
        return f"Reminder set — {r.due_at:%H:%M}: “{r.text}”."

    def reminder_list(self):
        rows = self.store.upcoming(tenant="t1", principal="u1", now=self.now)
        return "\n".join(r.text for r in rows) or "No reminders ahead."


class _ReminderTools(_Hands):
    """The chat-tools face over the hands (what GatewayChatTools does)."""

    def create_reminder_in(self, text, minutes):
        return self.reminder_in(text, minutes)

    def create_reminder_at(self, text, hour, minute, ampm):
        return self.reminder_at(text, hour, minute, ampm)

    def list_reminders(self):
        return self.reminder_list()


def test_remind_me_is_deterministic_and_never_a_doomed_run(tmp_path):
    conn = DurableConnection(tmp_path / "d.db")
    try:
        clock = _Clock()
        store = ReminderStore(conn, clock=clock)
        tools = _ReminderTools(store, NOW)
        assistant = ChatAssistant(model=_MustNotSpeak())
        turn = assistant.respond(
            "remind me to drink water in 20 minutes", tools=tools
        )
        # No task was handed to the engine — a reminder is a row, not a run.
        assert turn.task is None and turn.source == "tool"
        assert turn.actions == [{"tool": "create_reminder"}]
        [row] = store.upcoming(tenant="t1", principal="u1")
        assert row.text == "drink water"
        assert row.due_at == NOW + timedelta(minutes=20)
        assert "12:20" in turn.say  # confirmation from the stored row
        # The lead form and hours work too.
        assistant.respond("remind me in 2 hours to stretch", tools=tools)
        assert [r.text for r in store.upcoming(tenant="t1", principal="u1")] == [
            "drink water",
            "stretch",
        ]
        # A clock time resolves to the NEXT occurrence.
        turn = assistant.respond("remind me to call mom at 3pm", tools=tools)
        assert "15:00" in turn.say
        # The list phrase reads the store.
        listed = assistant.respond("show my reminders", tools=tools)
        assert "drink water" in listed.say and "call mom" in listed.say
    finally:
        conn.close()


def test_the_model_tool_creates_through_the_same_door(tmp_path):
    conn = DurableConnection(tmp_path / "d.db")
    try:
        store = ReminderStore(conn, clock=_Clock())
        tools = _ReminderTools(store, NOW)
        model = _FakeModel(
            [
                '{"tool": "create_reminder", "args": {"text": "water the plants", "at": "18:30"}}',
                '{"say": "Done — set for 18:30.", "task": null}',
            ]
        )
        turn = ChatAssistant(model=model).respond(
            "hey could you make sure i water the plants this evening?",
            tools=tools,
        )
        assert turn.actions == [{"tool": "create_reminder"}]
        [row] = store.upcoming(tenant="t1", principal="u1")
        assert row.text == "water the plants" and row.due_at.hour == 18
        # The stored confirmation reached the model as the tool result.
        assert "Reminder set" in model.calls[1][-1]["content"]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The routes: create, poll due, delivered exactly once, tenant-walled.         #
# --------------------------------------------------------------------------- #
def test_the_reminder_routes_end_to_end(tmp_path):
    app, conn, ident, desk, script_exec = _rig(tmp_path)
    try:
        clock = _Clock()
        app._reminders = ReminderStore(conn, clock=clock)
        app._clock = clock
        alice = ident.token("alice", "t1")
        created = app.handle(
            _req(
                "POST",
                "/v1/reminders",
                token=alice,
                body={"text": "submit the report", "in_minutes": 15},
            )
        )
        assert created.status == 201, created.body
        rid = created.body["reminder_id"]
        # Not ripe yet.
        assert app.handle(
            _req("GET", "/v1/reminders", token=alice)
        ).body == {
            "due": [],
            "upcoming": [created.body],
        }
        # Ripe: it moves to due; delivering marks it exactly once. The
        # request's own clock decides ripeness, so a later request sees it.
        from oolu.gateway.http import Request

        def _req_at(method, path, *, token, body=None):
            return Request(
                method=method,
                path=path,
                headers={"Authorization": f"Bearer {token}"},
                query={},
                body=body,
                now=NOW + timedelta(minutes=20),
            )

        due = app.handle(_req_at("GET", "/v1/reminders", token=alice)).body["due"]
        assert [d["reminder_id"] for d in due] == [rid]
        assert app.handle(
            _req_at("POST", f"/v1/reminders/{rid}/delivered", token=alice)
        ).status == 200
        assert app.handle(
            _req_at("POST", f"/v1/reminders/{rid}/delivered", token=alice)
        ).status == 404
        # Another account can't see or ring alice's reminder.
        bob = ident.token("bob", "t1")
        assert app.handle(
            _req("GET", "/v1/reminders", token=bob)
        ).body["due"] == []
        assert app.handle(
            _req("POST", f"/v1/reminders/{rid}/delivered", token=bob)
        ).status == 404
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Build-before-run: standing consent builds the node FIRST, then runs.         #
# --------------------------------------------------------------------------- #
def _consent_autobuild(app):
    class _Settings:
        def effective(self, tenant):
            return {"account.autobuild_consent": True}

        def describe(self, tenant):
            return []

    app._settings = _Settings()


def test_standing_consent_builds_the_node_before_the_run(tmp_path):
    app, conn, ident, desk, script_exec = _rig(tmp_path)
    try:
        _consent_autobuild(app)
        _speak_work(app, [TASK_TURN, TASK_TURN])
        resp = _chat(app, ident, f"please {GOAL}")
        assert resp.status == 200, resp.body
        # The node was built FIRST — function written, on the desk — and
        # the run routed through it instead of failing for want of one.
        entries = desk.overview(principal="user-1", tenant="t1")
        assert len(entries) == 1
        assert resp.body["run_id"], "the task ran through the fresh node"
        assert "node" in resp.body["reply"].lower()
        assert script_exec.actions, "the node's own function executed"
        # Run again: the SAME node answers — nothing new is minted.
        again = _chat(app, ident, f"please {GOAL}")
        assert again.status == 200 and again.body["run_id"]
        assert len(desk.overview(principal="user-1", tenant="t1")) == 1
    finally:
        conn.close()


def test_without_consent_the_offer_still_asks_first(tmp_path):
    app, conn, ident, desk, script_exec = _rig(tmp_path)
    try:
        _speak_work(app, [TASK_TURN])
        resp = _chat(app, ident, f"please {GOAL}")
        assert resp.status == 200
        # No silent build: the reply asks, and the desk stays empty.
        assert desk.overview(principal="user-1", tenant="t1") == []
        assert "yes" in resp.body["reply"].lower()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The rebuild's self-built code persists as a REAL node on the desk.           #
# --------------------------------------------------------------------------- #
def test_a_completed_rebuild_lands_in_my_nodes(tmp_path):
    from oolu.orchestrator.state import (
        Blueprint,
        ReservedAction,
        RoutePlan,
    )
    from oolu.skills.models import ActionEvent

    app, conn, ident, desk, script_exec = _rig(tmp_path)
    try:
        _speak_work(app, [])
        # A completed run whose route the model rebuilt: minimal state.
        from oolu.orchestrator.state import TaskContract

        state = app._durable.submit(
            TaskContract(
                intent="autonomous window",
                submitted_by="user-1",
                metadata={"tenant_id": "t1"},
            )
        )
        script = (
            "from _oolu_runtime import emit_result\nemit_result('done')\n"
        )
        state.route = RoutePlan(
            chosen=Blueprint(
                name="rebuilt",
                actions=[
                    ReservedAction(
                        action=ActionEvent(
                            correlation_id="c1",
                            adapter="script",
                            operation="run",
                            parameters={"goal": "tidy the ledger", "script": script},
                        ),
                        required_capabilities=frozenset({"run"}),
                        reserved=False,
                        risk="write",
                    )
                ],
                estimated_cost=0.0,
                origin="llm_rebuild",
            ),
            alternatives=[],
            total_cost=0.0,
        )
        state.contract = state.contract.model_copy(
            update={"intent": "tidy the ledger"}
        )
        from oolu.orchestrator.state import Phase

        state.phase = Phase.COMPLETED
        app._persist_rebuilt_route(state)
        [entry] = desk.overview(principal="user-1", tenant="t1")
        assert entry.account.responsible == "user-1"
        # The persisted node carries the rebuilt SCRIPT — a real function,
        # and the next run of this goal routes straight through it.
        session = app._session_for(ident.token("user-1", "t1"), NOW)
        function = app._resolve_node_function(session, "tidy the ledger")
        assert function is not None and "emit_result" in function["script"]
        # Idempotent: persisting again mints nothing.
        app._persist_rebuilt_route(state)
        assert len(desk.overview(principal="user-1", tenant="t1")) == 1
    finally:
        conn.close()
