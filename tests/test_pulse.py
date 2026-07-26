"""P0: the pulse — schedules that fire runs, exactly once, honestly.

Exit gate: a daily schedule fires exactly once per occurrence across
restarts and concurrent stores (the durable claim decides); the fired
run is an ORDINARY run — the owner's identity, the node's own
function, the standing stores; a slept-through week yields ONE
catch-up fire with the skipped count named on the run's metadata and
the audit chain; "every day at 9 run …" creates the row and reads it
back; pausing holds the rhythm and resuming skips the backlog; a fire
that cannot submit is audited, never raised into the serving request.
"""

from __future__ import annotations

from datetime import UTC, datetime

from test_growth_trigger import GOAL, _chat, _rig
from test_http_gateway import _req
from test_node_interact import FakeAuthor

from oolu.durable import DurableConnection
from oolu.pulse import (
    PulseStore,
    Schedule,
    missed_between,
    next_occurrence,
    occurrence_at,
    spoken_schedule,
)

T0 = datetime(2026, 6, 29, 8, 0, tzinfo=UTC)  # a Monday, 08:00 UTC


def _schedule(**overrides):
    base = dict(
        schedule_id="s1",
        tenant="t1",
        principal="user-1",
        cadence="daily",
        at_minute=9 * 60,
        tz_offset_minutes=0,
        goal="g",
        floor_at=datetime(2026, 6, 1, 8, 0, tzinfo=UTC),
        created_at=datetime(2026, 6, 1, 8, 0, tzinfo=UTC),
    )
    base.update(overrides)
    return Schedule(**base)


# --------------------------------------------------------------------------- #
# The ear: spoken rhythms parse; doubt falls through.                          #
# --------------------------------------------------------------------------- #
def test_the_spoken_rhythms_parse_and_doubt_falls_through():
    assert spoken_schedule("every day at 9 run my invoice node") == {
        "cadence": "daily", "at_minute": 540, "goal": "my invoice node",
    }
    assert spoken_schedule("Every Monday at 7:30pm send the weekly report") == {
        "cadence": "weekly", "weekday": 0, "at_minute": 19 * 60 + 30,
        "goal": "send the weekly report",
    }
    assert spoken_schedule("every month on the 1st at 9:00 do the books") == {
        "cadence": "monthly", "day_of_month": 1, "at_minute": 540,
        "goal": "the books",
    }
    assert spoken_schedule(
        "every year on june 5 at 8am celebrate the launch"
    ) == {
        "cadence": "yearly", "month": 6, "day": 5, "at_minute": 480,
        "goal": "celebrate the launch",
    }
    # Anything doubtful stays conversation — never a surprise alarm.
    assert spoken_schedule("every day I feel great") is None
    assert spoken_schedule("every day at 25:00 run x") is None
    assert spoken_schedule("every day at 13pm run x") is None


# --------------------------------------------------------------------------- #
# Occurrence arithmetic: local clocks, floors, clamped months.                 #
# --------------------------------------------------------------------------- #
def test_occurrences_honor_the_floor_and_the_local_clock():
    daily = _schedule()
    assert (
        occurrence_at(daily, datetime(2026, 6, 1, 9, 5, tzinfo=UTC))
        == "2026-06-01T09:00"
    )
    # Created AFTER today's time: the first fire is tomorrow.
    late = _schedule(floor_at=datetime(2026, 6, 1, 9, 30, tzinfo=UTC))
    assert occurrence_at(late, datetime(2026, 6, 1, 10, 0, tzinfo=UTC)) is None
    assert (
        next_occurrence(late, datetime(2026, 6, 1, 10, 0, tzinfo=UTC))
        == "2026-06-02T09:00"
    )
    # The owner's clock: 02:00 UTC is 10:00 in UTC+8 — nine has passed.
    eastern = _schedule(
        tz_offset_minutes=480,
        floor_at=datetime(2026, 5, 31, 20, 0, tzinfo=UTC),
    )
    assert (
        occurrence_at(eastern, datetime(2026, 6, 1, 2, 0, tzinfo=UTC))
        == "2026-06-01T09:00"
    )
    # Weekly rides its weekday; monthly day 31 clamps to February's end.
    weekly = _schedule(
        cadence="weekly", weekday=0,
        floor_at=datetime(2026, 6, 28, 0, 0, tzinfo=UTC),
    )
    assert (
        occurrence_at(weekly, datetime(2026, 6, 29, 9, 5, tzinfo=UTC))
        == "2026-06-29T09:00"
    )
    monthly = _schedule(
        cadence="monthly", day_of_month=31,
        floor_at=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
    )
    assert (
        occurrence_at(monthly, datetime(2026, 3, 5, 12, 0, tzinfo=UTC))
        == "2026-02-28T09:00"
    )


def test_missed_between_counts_what_the_host_slept_through():
    daily = _schedule()  # floor 2026-06-01 08:00
    # Never fired: everything after the floor and before the firing
    # occurrence counts — 6/1 through 6/7.
    assert missed_between(daily, None, "2026-06-08T09:00") == 7
    # Fired 6/1 last: 6/2 through 6/7 were missed.
    assert missed_between(daily, "2026-06-01T09:00", "2026-06-08T09:00") == 6
    # Yesterday fired: nothing missed.
    assert missed_between(daily, "2026-06-07T09:00", "2026-06-08T09:00") == 0


def test_claims_elect_exactly_once_across_restarts(tmp_path):
    conn = DurableConnection(tmp_path / "pulse.db")
    try:
        store = PulseStore(conn)
        assert store.claim("s1", "2026-06-29T09:00") is True
        assert store.claim("s1", "2026-06-29T09:00") is False
        # A second process (a restart) shares the ledger, not the luck.
        reborn = PulseStore(conn)
        assert reborn.claim("s1", "2026-06-29T09:00") is False
        assert reborn.claim("s1", "2026-06-30T09:00") is True
        assert store.newest_claim("s1")["occurrence"] == "2026-06-30T09:00"
        assert (
            store.newest_claim("s1", before="2026-06-30T09:00")["occurrence"]
            == "2026-06-29T09:00"
        )
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The circle: schedules fire ordinary runs through the real doors.             #
# --------------------------------------------------------------------------- #
def _fire(app, ident, now):
    """One tick at a simulated moment — any request drives the pulse;
    the real-time minute gate is reset so simulated time can step."""
    app._pulse_gate = 0.0
    return app.handle(
        _req("GET", "/v1/pulse", token=ident.token("user-1", "t1"), now=now)
    )


def _built_with_schedule(tmp_path, *, at="09:00"):
    app, conn, ident, desk, script_exec = _rig(tmp_path)
    app._node_function_author = lambda tenant: FakeAuthor()
    built = _chat(app, ident, "build me a node that " + GOAL)
    assert "Built a NEW node" in built.body["reply"]
    created = app.handle(
        _req(
            "POST",
            "/v1/pulse",
            token=ident.token("user-1", "t1"),
            body={"cadence": "daily", "at": at, "goal": GOAL},
            now=T0,
        )
    )
    assert created.status == 201, created.body
    return app, conn, ident, script_exec, created.body["schedule_id"]


def _pulse_runs(app):
    return [
        s
        for s in app._durable.runs.list()
        if s.contract.metadata.get("pulse")
    ]


def test_a_daily_schedule_fires_exactly_once_per_day_as_the_owner(tmp_path):
    app, conn, ident, script_exec, sid = _built_with_schedule(tmp_path)
    try:
        # Before nine: nothing is due.
        _fire(app, ident, T0.replace(hour=8, minute=30))
        assert script_exec.actions == []

        # Nine oh five: the fire — an ORDINARY run, as the owner,
        # through the node's own function.
        _fire(app, ident, T0.replace(hour=9, minute=5))
        assert len(script_exec.actions) == 1
        (state,) = _pulse_runs(app)
        assert state.contract.submitted_by == "user-1"
        assert state.contract.intent == GOAL
        assert state.phase.value == "completed"
        pulse = state.contract.metadata["pulse"]
        assert pulse["schedule_id"] == sid
        assert pulse["occurrence"] == "2026-06-29T09:00"
        assert pulse["skipped"] == 0
        (fired,) = [
            e
            for e in app._durable.audit.records()
            if e.event_type == "pulse.fired"
        ]
        assert fired.payload["run_id"] == state.run_id
        assert app._pulse.newest_claim(sid)["run_id"] == state.run_id

        # The same occurrence never fires twice — the claim is held.
        _fire(app, ident, T0.replace(hour=9, minute=10))
        assert len(script_exec.actions) == 1

        # The next day, the rhythm continues.
        _fire(app, ident, datetime(2026, 6, 30, 9, 5, tzinfo=UTC))
        assert len(script_exec.actions) == 2
    finally:
        conn.close()


def test_a_slept_through_week_yields_one_catchup_with_skips_named(tmp_path):
    app, conn, ident, script_exec, sid = _built_with_schedule(tmp_path)
    try:
        _fire(app, ident, T0.replace(hour=9, minute=5))
        assert len(script_exec.actions) == 1

        # The host sleeps a week; waking fires ONCE, and the run
        # NAMES the six mornings it slept through.
        _fire(app, ident, datetime(2026, 7, 6, 9, 5, tzinfo=UTC))
        assert len(script_exec.actions) == 2
        newest = max(_pulse_runs(app), key=lambda s: s.created_at)
        assert newest.contract.metadata["pulse"]["occurrence"] == (
            "2026-07-06T09:00"
        )
        assert newest.contract.metadata["pulse"]["skipped"] == 6
        fired = [
            e
            for e in app._durable.audit.records()
            if e.event_type == "pulse.fired"
        ]
        assert fired[-1].payload["skipped"] == 6
    finally:
        conn.close()


def test_pausing_holds_the_rhythm_and_resuming_skips_the_backlog(tmp_path):
    app, conn, ident, script_exec, sid = _built_with_schedule(tmp_path)
    try:
        paused = app.handle(
            _req(
                "POST",
                f"/v1/pulse/{sid}",
                token=ident.token("user-1", "t1"),
                body={"enabled": False},
                now=T0,
            )
        )
        assert paused.status == 200 and paused.body["enabled"] is False
        _fire(app, ident, T0.replace(hour=9, minute=5))
        assert script_exec.actions == []

        # Resuming resets the floor: the paused stretch stays skipped,
        # and even today's earlier nine stays behind the new floor.
        later = datetime(2026, 7, 2, 10, 0, tzinfo=UTC)
        fresh = ident._signer.mint(
            subject="user-1", tenant_id="t1", now=later
        )
        resumed = app.handle(
            _req(
                "POST",
                f"/v1/pulse/{sid}",
                token=fresh,
                body={"enabled": True},
                now=later,
            )
        )
        assert resumed.status == 200 and resumed.body["enabled"] is True
        _fire(app, ident, datetime(2026, 7, 2, 10, 5, tzinfo=UTC))
        assert script_exec.actions == []
        _fire(app, ident, datetime(2026, 7, 3, 9, 5, tzinfo=UTC))
        assert len(script_exec.actions) == 1
        (state,) = _pulse_runs(app)
        assert state.contract.metadata["pulse"]["skipped"] == 0
    finally:
        conn.close()


def test_the_spoken_schedule_is_created_read_back_and_cancelled(tmp_path):
    app, conn, ident, desk, script_exec = _rig(tmp_path)
    try:
        app._node_function_author = lambda tenant: FakeAuthor()
        built = _chat(app, ident, "build me a node that " + GOAL)
        assert "Built a NEW node" in built.body["reply"]

        spoken = _chat(app, ident, "every day at 9 run " + GOAL)
        assert spoken.status == 200, spoken.body
        reply = spoken.body["reply"]
        assert "every day at 09:00" in reply
        assert GOAL in reply
        # The read-back names the node the goal resolves to.
        assert "Normalize Invoice Csv Files" in reply
        assert spoken.body["source"] == "tool"
        (row,) = app._pulse.list_for("t1", "user-1")
        assert row.goal == GOAL
        assert row.cadence == "daily" and row.at_minute == 540

        listed = _chat(app, ident, "schedules")
        assert GOAL in listed.body["reply"]
        assert row.schedule_id[:8] in listed.body["reply"]

        gone = _chat(app, ident, f"cancel schedule {row.schedule_id[:8]}")
        assert "Cancelled" in gone.body["reply"]
        assert app._pulse.list_for("t1", "user-1") == []
        cancelled = [
            e
            for e in app._durable.audit.records()
            if e.event_type == "pulse.cancelled"
        ]
        assert cancelled, "the cancellation is on the chain"
    finally:
        conn.close()


def test_a_fire_that_cannot_submit_is_audited_not_raised(tmp_path):
    app, conn, ident, desk, script_exec = _rig(tmp_path)
    try:
        created = app.handle(
            _req(
                "POST",
                "/v1/pulse",
                token=ident.token("user-1", "t1"),
                body={"cadence": "daily", "at": "09:00",
                      "goal": "polish the moon"},
                now=T0,
            )
        )
        assert created.status == 201, created.body
        response = _fire(app, ident, T0.replace(hour=9, minute=5))
        # The serving request is untouched by the refused fire.
        assert response.status == 200, response.body
        assert script_exec.actions == []
        failed = [
            e
            for e in app._durable.audit.records()
            if e.event_type == "pulse.fire_failed"
        ]
        assert failed
        assert "no capable node" in failed[-1].payload["reason"]
    finally:
        conn.close()
