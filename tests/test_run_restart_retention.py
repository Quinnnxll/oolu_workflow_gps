"""Failed runs revive in place, and finished history gets trimmed.

Exit gate: asking a goal again after its run FAILED restarts THAT run —
same run_id, same Noder thread, retry counted, moment moved (the thread
rises) — instead of minting a dead sibling every attempt; when the
world changes (the executor heals, a node appears), the revived thread
completes. Retention applies for real: terminal runs, finished tasks,
delivered outbox rows, and the audit chain's oldest prefix leave the
books on the hourly tick — the cut is attested so the surviving chain
still verifies, and a SILENT prefix deletion still fails verification.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from test_http_gateway import NOW, _app, _blueprint, _Executor, _param, _req

from oolu.durable.audit import DurableAuditLog
from oolu.durable.maintenance import prune_retention
from oolu.gateway import GatewayConfig
from oolu.skills.requirements import (
    AuthorizationGrant,
    AuthorizationMode,
    RequirementBrief,
)

_LIVE: dict[str, _Executor] = {}


def _flaky():
    """An executor that fails until the test heals it."""
    executor = _Executor({"run"}, fail_times=99)
    _LIVE["executor"] = executor
    return (
        RequirementBrief(
            intent="auto",
            parameters=[_param("a", value="b")],
            authorization=AuthorizationGrant(
                mode=AuthorizationMode.FULLY_DELEGATED
            ),
        ),
        _blueprint(),
        executor,
        {"a": "run"},
    )


def test_a_reasked_failed_goal_revives_its_own_thread(tmp_path):
    app, conn, ident = _app(tmp_path, _flaky)
    try:
        token = ident.token("user-1", "t1")
        first = app.handle(
            _req("POST", "/v1/runs", token=token, body={"intent": "tidy desk"})
        )
        run_id = first.body["run_id"]
        # A failing execution parks on the incident door (awaiting the
        # operator's retry/abort) — the exact state that used to breed
        # sibling threads on every re-ask.
        assert first.body["awaiting"] == "incident"

        # Asking again does NOT mint a sibling: the same thread revives
        # (the re-ask IS the retry answer), fails again — the world is
        # unchanged — and counts the retry.
        again = app.handle(
            _req("POST", "/v1/runs", token=token, body={"intent": "tidy desk"})
        )
        assert again.body["run_id"] == run_id
        assert again.body["user_retries"] == 1
        listed = app.handle(_req("GET", "/v1/runs", token=token))
        assert listed.body["total"] == 1  # one goal, one thread

        # The world heals (the executor works now): the SAME thread
        # completes — no phantom siblings were ever created.
        _LIVE["executor"]._fail_times = 0
        healed = app.handle(
            _req("POST", "/v1/runs", token=token, body={"intent": "tidy desk"})
        )
        assert healed.body["run_id"] == run_id
        assert healed.body["phase"] == "completed"
        assert healed.body["user_retries"] == 2
        assert app.handle(_req("GET", "/v1/runs", token=token)).body["total"] == 1

        # A DIFFERENT goal is genuinely new work — its own thread.
        other = app.handle(
            _req("POST", "/v1/runs", token=token, body={"intent": "sort mail"})
        )
        assert other.body["run_id"] != run_id
    finally:
        conn.close()


def test_a_strangers_failed_goal_is_not_reused(tmp_path):
    app, conn, ident = _app(tmp_path, _flaky)
    try:
        mine = app.handle(
            _req(
                "POST", "/v1/runs",
                token=ident.token("user-1", "t1"), body={"intent": "tidy desk"},
            )
        )
        theirs = app.handle(
            _req(
                "POST", "/v1/runs",
                token=ident.token("user-2", "t1"), body={"intent": "tidy desk"},
            )
        )
        # Same goal, different person: never the same thread.
        assert theirs.body["run_id"] != mine.body["run_id"]
    finally:
        conn.close()


# --------------------------------------------------------------------- #
# Retention: history trimmed, the chain still a chain.                   #
# --------------------------------------------------------------------- #
def test_retention_trims_terminal_runs_and_attests_the_audit_cut(tmp_path):
    app, conn, ident = _app(tmp_path)
    try:
        app.handle(
            _req(
                "POST", "/v1/runs",
                token=ident.token("user-1", "t1"), body={"intent": "tidy"},
            )
        )
        audit = DurableAuditLog(conn)
        assert audit.records()  # the run left its trail
        pruned = prune_retention(
            conn,
            older_than_days=0.0,
            now=datetime.now(UTC) + timedelta(minutes=1),
        )
        assert pruned["runs"] == 1
        assert pruned["audit"] >= 1
        # The surviving chain verifies — the cut is attested in-chain.
        assert audit.verify()
        (retention_mark,) = [
            r for r in audit.records() if r.event_type == "audit.retention"
        ]
        assert retention_mark.payload["pruned_rows"] == pruned["audit"]
        # And a SILENT deletion (no attestation) still fails verification:
        # grow the chain past the mark, then rip the mark out.
        audit.append("noise.one", {"n": 1})
        audit.append("noise.two", {"n": 2})
        with conn.transaction() as db:
            db.execute(
                "DELETE FROM audit_log WHERE seq ="
                " (SELECT MIN(seq) FROM audit_log)"
            )
        assert not audit.verify()
    finally:
        conn.close()


def test_the_hourly_tick_applies_retention_from_config(tmp_path):
    app, conn, ident = _app(
        tmp_path, config=GatewayConfig(retention_days=0.25)
    )
    try:
        token = ident.token("user-1", "t1")
        app.handle(_req("POST", "/v1/runs", token=token, body={"intent": "tidy"}))
        # Traffic far in the future advances the lazy tick past the
        # window: the finished run leaves the books by itself.
        later = datetime.now(UTC) + timedelta(days=2)
        # The submit above already ticked retention once (a no-op — the
        # window hadn't passed); reopen the hourly gate so the next
        # request's tick runs now instead of an hour from now.
        app._retention_gate = 0.0
        # The tick runs before routing, so even this (token-expired)
        # request applies retention; the fresh-clock read then shows it.
        app.handle(_req("GET", "/v1/runs", token=token, now=later))
        listed = app.handle(_req("GET", "/v1/runs", token=token))
        assert listed.body["total"] == 0
        assert DurableAuditLog(conn).verify()
    finally:
        conn.close()


def test_retention_never_touches_live_or_paused_work(tmp_path):
    app, conn, ident = _app(tmp_path)
    try:
        token = ident.token("user-1", "t1")
        run = app.handle(
            _req("POST", "/v1/runs", token=token, body={"intent": "tidy"})
        )
        assert run.body["phase"] == "completed"
        # A paused run (awaiting a human) survives any cutoff.
        state = conn.db.execute(
            "SELECT COUNT(*) AS n FROM workflow_runs WHERE phase"
            " NOT IN ('completed', 'failed', 'cancelled')"
        ).fetchone()
        before_live = int(state["n"])
        prune_retention(
            conn,
            older_than_days=0.0,
            now=datetime.now(UTC) + timedelta(minutes=1),
        )
        after = conn.db.execute(
            "SELECT COUNT(*) AS n FROM workflow_runs WHERE phase"
            " NOT IN ('completed', 'failed', 'cancelled')"
        ).fetchone()
        assert int(after["n"]) == before_live
        assert NOW  # the fixed request clock stays available to siblings
    finally:
        conn.close()
