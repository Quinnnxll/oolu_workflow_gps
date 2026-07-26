"""P4: the automated life — the trigger node and the morning pulse.

Exit gate: the Automation Trigger node's own run parses a rhythm and
its emitted schedule is filed into the standing PulseStore by the
completion hook (the sandbox parses, the host keeps the beat); the
morning pulse seeds DISABLED with the shelf, switches on in one plain
sentence, and its fire runs the owner's Calendar and Tasks as ordinary
pulse-stamped runs — landing ONE combined message through the reminder
channel; two accounts get two different mornings from the same shelf;
and "what fired you, when" answers from the stores with run ids.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime

from test_growth_trigger import _chat, _rig
from test_http_gateway import _req
from test_records_trio import _SHIM, SPECS

from oolu.cache import LocalScriptCache
from oolu.nodeplace.personal_templates import (
    MORNING_PULSE_GOAL,
    starter_script,
)
from oolu.reminders import ReminderStore
from oolu.runtime import NodeScriptRunner, StubBackend
from oolu.runtime.backend import make_success
from oolu.values import ValueStore

T0 = datetime(2026, 6, 29, 8, 0, tzinfo=UTC)  # a Monday, 08:00 UTC


def _execute(tmp_path, bindings=None, records=None):
    workdir = tmp_path / f"trigger-{len(list(tmp_path.iterdir()))}"
    workdir.mkdir()
    (workdir / "main.py").write_text(starter_script(SPECS["trigger"]))
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
# The trigger's own function: rhythms parsed, doubt refused.                   #
# --------------------------------------------------------------------------- #
def test_the_trigger_parses_rhythms_and_refuses_doubt(tmp_path):
    daily = _execute(
        tmp_path, {"rhythm": "every day at 9, run my invoice node"}
    )
    assert daily["schedule"] == {
        "cadence": "daily",
        "at_minute": 540,
        "goal": "my invoice node",
        "words": "every day at 9, run my invoice node",
    }
    assert "Standing rhythm set" in daily["answer"]
    assert daily["fired_at"] and daily["occasion"]

    weekly = _execute(
        tmp_path, {"rhythm": "every monday at 7:30pm, send the report"}
    )
    assert weekly["schedule"]["cadence"] == "weekly"
    assert weekly["schedule"]["weekday"] == 0
    assert weekly["schedule"]["at_minute"] == 19 * 60 + 30

    garbled = _execute(tmp_path, {"rhythm": "whenever feels right"})
    assert "schedule" not in garbled
    assert "Nothing was set" in garbled["answer"]

    listing = _execute(tmp_path, {}, records=daily["records"])
    assert "every day at 9, run my invoice node" in listing["answer"]


# --------------------------------------------------------------------------- #
# The circle: the emitted schedule becomes a standing rhythm.                  #
# --------------------------------------------------------------------------- #
def _life_rig(tmp_path, payloads):
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
    return app, conn, ident, backend


TRIGGER_PAYLOAD = {
    "answer": "Standing rhythm set: every day at 9, run my invoice node.",
    "records": [{"rhythm": "every day at 9, run my invoice node"}],
    "fired_at": "2026-06-29T08:05:00",
    "occasion": "monday",
    "schedule": {
        "cadence": "daily",
        "at_minute": 540,
        "goal": "my invoice node",
        "words": "every day at 9, run my invoice node",
    },
}


def test_the_emitted_schedule_is_filed_into_the_pulse_store(tmp_path):
    app, conn, ident, backend = _life_rig(tmp_path, [TRIGGER_PAYLOAD])
    try:
        app._seed_starter_shelf("t1", "user-1")
        ran = app.handle(
            _req(
                "POST",
                "/v1/runs",
                token=ident.token("user-1", "t1"),
                body={
                    "intent": SPECS["trigger"].goal,
                    "values": {
                        "rhythm": "every day at 9, run my invoice node"
                    },
                },
                now=T0,
            )
        )
        assert ran.status == 202, ran.body
        rows = [
            s
            for s in app._pulse.list_for("t1", "user-1")
            if s.goal == "my invoice node"
        ]
        (row,) = rows
        assert row.cadence == "daily" and row.at_minute == 540
        assert row.label == "every day at 9, run my invoice node"
        created = [
            e
            for e in app._durable.audit.records()
            if e.event_type == "pulse.created"
            and e.payload.get("run_id") == ran.body["run_id"]
        ]
        assert len(created) == 1
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The morning pulse: seeded off, one sentence on, one message each.            #
# --------------------------------------------------------------------------- #
def _fire(app, ident, now):
    app._pulse_gate = 0.0
    return app.handle(
        _req("GET", "/v1/pulse", token=ident.token("user-1", "t1"), now=now)
    )


def _morning_row(app, principal):
    return next(
        s
        for s in app._pulse.list_for("t1", principal)
        if s.goal == MORNING_PULSE_GOAL
    )


def test_the_morning_pulse_seeds_disabled_and_switches_on_in_a_sentence(
    tmp_path,
):
    app, conn, ident, backend = _life_rig(tmp_path, [])
    try:
        app._seed_starter_shelf("t1", "user-1")
        row = _morning_row(app, "user-1")
        assert row.enabled is False
        assert row.cadence == "daily" and row.at_minute == 540

        switched = _chat(app, ident, "turn on the morning pulse")
        assert switched.status == 200, switched.body
        assert "morning pulse is ON" in switched.body["reply"]
        assert switched.body["source"] == "tool"
        assert _morning_row(app, "user-1").enabled is True

        off = _chat(app, ident, "turn off the morning pulse")
        assert "morning pulse is OFF" in off.body["reply"]
        assert _morning_row(app, "user-1").enabled is False
    finally:
        conn.close()


def _payloads_for(cal_answer, tasks_answer):
    return [
        {
            "answer": cal_answer,
            "records": [],
            "events_today": [],
            "events_week": [],
        },
        {
            "answer": tasks_answer,
            "records": [],
            "open_tasks": [],
            "reminder_offer": {},
        },
    ]


def test_two_accounts_get_two_different_mornings(tmp_path):
    payloads = _payloads_for(
        "Today: standup at 10", "Open: invoice Alex"
    ) + _payloads_for("Nothing on the calendar today.", "Open: water plants")
    app, conn, ident, backend = _life_rig(tmp_path, payloads)
    try:
        app._seed_starter_shelf("t1", "user-1")
        app._seed_starter_shelf("t1", "user-2")
        for principal in ("user-1", "user-2"):
            row = _morning_row(app, principal)
            app._pulse.set_enabled(
                row.schedule_id,
                tenant="t1",
                principal=principal,
                enabled=True,
                now=T0,
            )

        # The tick after nine: each account's OWN morning, one message
        # on the reminder channel (due minutes ahead on the host clock,
        # so the client's next poll delivers it).
        _fire(app, ident, T0.replace(hour=9, minute=5))
        (mine,) = app._reminders.upcoming(tenant="t1", principal="user-1")
        assert "Today: standup at 10" in mine.text
        assert "Open: invoice Alex" in mine.text
        (theirs,) = app._reminders.upcoming(tenant="t1", principal="user-2")
        assert "Nothing on the calendar today." in theirs.text
        assert "Open: water plants" in theirs.text
        assert mine.text != theirs.text

        # "What fired you, when" answers from the stores with run ids:
        # every fired run carries the schedule and occurrence…
        fired = [
            s
            for s in app._durable.runs.list()
            if s.contract.metadata.get("pulse")
        ]
        assert len(fired) == 4  # calendar + tasks, per account
        for state in fired:
            pulse = state.contract.metadata["pulse"]
            assert pulse["occurrence"] == "2026-06-29T09:00"
        # …and the claim names a fired run.
        claim = app._pulse.newest_claim(
            _morning_row(app, "user-1").schedule_id
        )
        assert claim["run_id"] in {s.run_id for s in fired}

        # The same occurrence never fires twice.
        _fire(app, ident, T0.replace(hour=9, minute=30))
        assert len(
            [
                s
                for s in app._durable.runs.list()
                if s.contract.metadata.get("pulse")
            ]
        ) == 4
    finally:
        conn.close()
