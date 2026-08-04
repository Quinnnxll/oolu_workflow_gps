"""P2: the personal records trio — calendar, tasks, reminders.

Exit gate: the trio's deterministic functions keep their own book —
the drawer's ``records/rows.json`` stages into the sandbox, the
emitted book lands back, and the next run reads it; a form's values
ride POST /v1/runs through the B1 strict check into ``bindings.json``
verbatim (unknown keys refuse in words); the calendar's ``events_today``
stands on the port index for the trigger to consume (B4); the
reminders node's emitted ask is filed into the standing ReminderStore
AS THE OWNER by the completion hook; and a task with a date OFFERS a
reminder in the conversation — created only on a yes.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta

from test_chat_assistant import _FakeModel
from test_growth_trigger import _chat, _rig
from test_http_gateway import _req

from oolu.cache import LocalScriptCache
from oolu.nodeplace.personal_templates import STARTER_SHELF, starter_script
from oolu.reminders import ReminderStore
from oolu.runtime import NodeScriptRunner, StubBackend
from oolu.runtime.backend import make_success
from oolu.values import ValueStore

_SHIM = (
    "import json\n"
    "def emit_result(value):\n"
    "    with open('result.json', 'w') as handle:\n"
    "        json.dump(value, handle)\n"
)

SPECS = {spec.key: spec for spec in STARTER_SHELF}


def _execute(tmp_path, key, bindings=None, records=None):
    workdir = tmp_path / f"{key}-{len(list(tmp_path.iterdir()))}"
    workdir.mkdir()
    (workdir / "main.py").write_text(starter_script(SPECS[key]))
    (workdir / "_oolu_runtime.py").write_text(_SHIM)
    if bindings is not None:
        (workdir / "bindings.json").write_text(json.dumps(bindings))
    if records is not None:
        (workdir / "records.json").write_text(json.dumps(records))
    subprocess.run(
        [sys.executable, "main.py"],
        cwd=workdir,
        check=True,
        timeout=60,
        capture_output=True,
    )
    return json.loads((workdir / "result.json").read_text())


# --------------------------------------------------------------------------- #
# The functions themselves: real execution, real behavior.                     #
# --------------------------------------------------------------------------- #
def test_the_calendar_keeps_its_book_and_projects_today_and_week(tmp_path):
    added = _execute(tmp_path, "calendar", {"entry": "team lunch today"})
    (row,) = added["records"]
    assert row["event"] == "team lunch today"
    assert row["on"] == date.today().isoformat()
    assert added["events_today"] == ["team lunch today"]

    # The next run READS the book: no new entry, the day still answers.
    again = _execute(tmp_path, "calendar", {}, records=added["records"])
    assert again["records"] == added["records"]
    assert again["answer"] == "Today: team lunch today"

    # A weekday names a day within the coming week — the week view has it.
    friday = _execute(tmp_path, "calendar", {"entry": "review on friday"})
    (frow,) = friday["records"]
    assert frow["on"]  # dated, deterministically
    assert "review on friday" in friday["events_week"]


def test_tasks_add_done_and_the_dated_task_offers_a_reminder(tmp_path):
    added = _execute(
        tmp_path, "tasks", {"task": "send the quote tomorrow at 3pm"}
    )
    (row,) = added["records"]
    assert row["due"] == (date.today() + timedelta(days=1)).isoformat()
    assert added["open_tasks"] == ["send the quote tomorrow at 3pm"]
    offer = added["reminder_offer"]
    assert offer["text"] == "send the quote tomorrow at 3pm"
    assert offer["day"] == row["due"]
    assert offer["time"] == "15:00"

    finished = _execute(
        tmp_path, "tasks", {"done": "quote"}, records=added["records"]
    )
    assert finished["answer"] == "Done: send the quote tomorrow at 3pm"
    assert finished["open_tasks"] == []
    assert finished["records"][0]["done"] is True
    assert finished["reminder_offer"] == {}


def test_reminders_parse_the_day_and_clock_in_plain_words(tmp_path):
    asked = _execute(
        tmp_path, "reminders", {"reminder": "call the bank tomorrow at 9"}
    )
    reminder = asked["reminder"]
    assert reminder["text"] == "call the bank tomorrow at 9"
    assert reminder["day"] == (date.today() + timedelta(days=1)).isoformat()
    assert reminder["time"] == "09:00"

    standing = _execute(tmp_path, "reminders", {}, records=asked["records"])
    assert standing["reminder"] == {}
    assert "call the bank" in standing["answer"]


# --------------------------------------------------------------------------- #
# The circle through the gateway: book staged, landed, and consumed.           #
# --------------------------------------------------------------------------- #
def _record_rig(tmp_path, payloads):
    holder: dict = {}
    backend = StubBackend([make_success(p) for p in payloads])
    runner = NodeScriptRunner(
        backend,
        LocalScriptCache(":memory:"),
        value_resolver=lambda bindings, tenant: holder["store"].resolve_bindings(
            bindings, tenant=tenant
        ),
    )
    app, conn, ident, desk, _exec = _rig(tmp_path, script_exec=runner)
    store = ValueStore(conn)
    holder["store"] = store
    app._values = store
    app._reminders = ReminderStore(conn)
    app._seed_starter_shelf("t1", "user-1")
    return app, conn, ident, backend


CAL_ROWS = [{"event": "team lunch", "on": "2026-06-29"}]
CAL_PAYLOAD = {
    "answer": "Noted: team lunch",
    "records": CAL_ROWS,
    "events_today": ["team lunch"],
    "events_week": ["team lunch"],
}


def test_the_book_lands_in_the_drawer_and_stages_into_the_next_run(tmp_path):
    goal = SPECS["calendar"].goal
    second = {**CAL_PAYLOAD, "answer": "Today: team lunch"}
    app, conn, ident, backend = _record_rig(tmp_path, [CAL_PAYLOAD, second])
    try:
        first = app.handle(
            _req(
                "POST",
                "/v1/runs",
                token=ident.token("user-1", "t1"),
                body={
                    "intent": goal,
                    "values": {"entry": "team lunch today"},
                },
            )
        )
        assert first.status == 202, first.body
        # The form's value rode the B1 check into the sandbox verbatim.
        staged = json.loads(backend.requests[-1].files["bindings.json"])
        assert staged == {"entry": "team lunch today"}
        # The emitted book landed in the drawer.
        rows_file = None
        for node in app._nodeplace.all_nodes():
            for f in app._files.list(tenant="t1", node_id=node.node_id):
                if f.folder == "records" and f.name == "rows.json":
                    rows_file = f
        assert rows_file is not None
        assert json.loads(rows_file.content) == CAL_ROWS

        # The NEXT run stages the landed book as ./records.json.
        again = app.handle(
            _req(
                "POST",
                "/v1/runs",
                token=ident.token("user-1", "t1"),
                body={"intent": goal},
            )
        )
        assert again.status == 202, again.body
        assert json.loads(
            backend.requests[-1].files["records.json"]
        ) == CAL_ROWS

        # And the projection stands on the port index — the slot the
        # trigger node consumes (B4).
        producers = app._values.producers_of("t1", "events_today")
        assert producers, "events_today is a standing output"
    finally:
        conn.close()


def test_the_values_door_refuses_undeclared_keys_in_words(tmp_path):
    app, conn, ident, backend = _record_rig(tmp_path, [CAL_PAYLOAD])
    try:
        refused = app.handle(
            _req(
                "POST",
                "/v1/runs",
                token=ident.token("user-1", "t1"),
                body={
                    "intent": SPECS["calendar"].goal,
                    "values": {"formatting": "csv"},
                },
            )
        )
        assert refused.status == 400, refused.body
        assert "no input called" in refused.body["error"]["message"]
    finally:
        conn.close()


def test_the_emitted_reminder_is_filed_as_the_owner(tmp_path):
    soon = (date.today() + timedelta(days=30)).isoformat()
    payload = {
        "answer": "I'll nudge you.",
        "records": [{"reminder": "renew the licence", "day": soon}],
        "reminder": {
            "text": "renew the licence",
            "day": soon,
            "time": "09:30",
        },
    }
    app, conn, ident, backend = _record_rig(tmp_path, [payload])
    try:
        ran = app.handle(
            _req(
                "POST",
                "/v1/runs",
                token=ident.token("user-1", "t1"),
                body={
                    "intent": SPECS["reminders"].goal,
                    "values": {"reminder": "renew the licence"},
                },
            )
        )
        assert ran.status == 202, ran.body
        (row,) = app._reminders.upcoming(tenant="t1", principal="user-1")
        assert row.text == "renew the licence"
        assert row.due_at == datetime.fromisoformat(
            f"{soon}T09:30"
        ).replace(tzinfo=UTC)
        filed = [
            e
            for e in app._durable.audit.records()
            if e.event_type == "reminder.filed"
        ]
        assert len(filed) == 1
    finally:
        conn.close()


_RENT_DAY = (date.today() + timedelta(days=14)).isoformat()
TASK_PAYLOAD = {
    "answer": "Added: pay the rent",
    "records": [{"task": "pay the rent", "due": _RENT_DAY, "done": False}],
    "open_tasks": ["pay the rent"],
    "reminder_offer": {
        "text": "pay the rent",
        "day": _RENT_DAY,
        "time": "09:00",
    },
}


def test_a_dated_task_offers_a_reminder_created_only_on_yes(tmp_path):
    goal = SPECS["tasks"].goal
    app, conn, ident, backend = _record_rig(tmp_path, [TASK_PAYLOAD])
    try:
        model = _FakeModel(['{"say": "On it!", "task": "' + goal + '"}'])
        app._tenant_model = lambda tenant: model
        # V1: the ask only ACCEPTS — the reply is the model's ACK, the
        # run stands queued, and the reminder offer waits on the drive.
        ran = _chat(app, ident, "please " + goal)
        assert ran.status == 200, ran.body
        assert ran.body["reply"] == "On it!"
        assert ran.body["run_id"]
        assert ran.body["run"]["phase"] == "intake"
        assert app.drive_queue() >= 1
        # The finish reported to the thread that asked, offer attached.
        turns = app._assistant_history.history(
            tenant="t1", principal="user-1"
        )
        assert [t["kind"] for t in turns] == [
            "user", "assistant", "run", "assistant"
        ]
        assert "want a reminder" in turns[-1]["body"]
        assert app._growth_offers.get("t1", "user-1")[0] == "task_reminder"
        # Nothing filed yet — the offer is a question, not an act. (The
        # drive's own reminder-ring ping is bookkeeping, not the offer.)
        assert [
            r
            for r in app._reminders.upcoming(tenant="t1", principal="user-1")
            if r.text == "pay the rent"
        ] == []

        agreed = _chat(app, ident, "yes")
        assert "Reminder set" in agreed.body["reply"]
        (row,) = [
            r
            for r in app._reminders.upcoming(tenant="t1", principal="user-1")
            if r.text == "pay the rent"
        ]
        assert row.text == "pay the rent"
        assert app._growth_offers.get("t1", "user-1") is None
    finally:
        conn.close()


def test_no_leaves_the_task_without_a_reminder(tmp_path):
    goal = SPECS["tasks"].goal
    app, conn, ident, backend = _record_rig(tmp_path, [TASK_PAYLOAD])
    try:
        model = _FakeModel(['{"say": "On it!", "task": "' + goal + '"}'])
        app._tenant_model = lambda tenant: model
        _chat(app, ident, "please " + goal)
        # V1: the offer only stands once the drive lands the report —
        # answer before it and there is no question on the table.
        assert app.drive_queue() >= 1
        declined = _chat(app, ident, "no")
        assert "no reminder" in declined.body["reply"].lower()
        # No offered reminder filed — only the drive's report ping stands.
        assert [
            r
            for r in app._reminders.upcoming(tenant="t1", principal="user-1")
            if r.text == "pay the rent"
        ] == []
    finally:
        conn.close()
