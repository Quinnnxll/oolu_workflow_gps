"""Work survives the window (V1): the deliberate worker + restart re-drive.

Exit gate (node-vitality-plan, phase V1): a chat task is ACCEPTED —
durable run row + queue task — and the turn returns; the one deliberate
worker drives it (lease/reclaim already durable), then lands the REPORT
turn in the thread that asked, with a reminder-ring ping; a resumed
decision queues its drive instead of dying with the window; a killed
process is recovered at boot (re-queue what lost its task, report what
settled unreported); and a graceful stop hands the lease back with the
attempt refunded, the staged row being the checkpoint.
"""

from __future__ import annotations

import time

from test_http_gateway import _app, _approval, _autonomous, _req

from oolu.durable import DurableConnection, DurableWorkflowService
from oolu.gateway import GatewayApp
from oolu.identity import LocalAccountService, LocalUserStore
from oolu.orchestrator import (
    PauseKind,
    Phase,
    ResumeInput,
    TaskContract,
)
from oolu.reminders import ReminderStore
from oolu.settings_node import SettingsNode, SettingsStore
from oolu.social import AssistantHistoryStore, RunReportStore


# --------------------------------------------------------------------------- #
# The service seam: async submission, honest drives, graceful drains.          #
# --------------------------------------------------------------------------- #
def _service(tmp_path, *, mode=None):
    from test_durable_runtime import (
        ScriptedActionExecutor,
        _blueprint,
        _factory,
        _param,
    )

    from oolu.skills.requirements import (
        AuthorizationGrant,
        AuthorizationMode,
        RequirementBrief,
    )

    brief = RequirementBrief(
        intent="summarize",
        parameters=[_param("format", value="md")],
        authorization=AuthorizationGrant(
            mode=mode or AuthorizationMode.FULLY_DELEGATED
        ),
    )
    factory = _factory(
        brief=brief,
        blueprint=_blueprint(
            operation="render", capability="render", reserved=False, risk="write"
        ),
        executor=ScriptedActionExecutor({"render"}),
        grounding_map={"format": "render"},
    )
    conn = DurableConnection(tmp_path / "runs.db")
    return conn, DurableWorkflowService(conn, factory)


def test_submit_async_queues_and_process_next_finishes(tmp_path):
    conn, service = _service(tmp_path)
    run_id = service.submit_async(TaskContract(intent="summarize"))
    # Accepted means durable: the run row AND its advance task stand
    # before anything drives.
    assert service.get(run_id) is not None
    assert run_id in service.queued_run_ids()
    task = service.process_next("worker-1")
    assert task is not None and task.status.value == "done"
    assert task.result == {"phase": "completed"}
    assert service.get(run_id).phase is Phase.COMPLETED
    assert service.queued_run_ids() == set()
    conn.close()


def test_a_drain_hands_the_lease_back_with_the_attempt_refunded(tmp_path):
    conn, service = _service(tmp_path)
    run_id = service.submit_async(TaskContract(intent="summarize"))
    # The worker is asked to stop after the very first step: the task
    # goes BACK to the pool (no attempt burned — a drain is not a
    # failure), and the staged row already shows the run mid-flight.
    task = service.process_next("worker-1", should_stop=lambda: True)
    assert task is not None and task.status.value == "pending"
    assert task.attempts == 0
    staged = service.get(run_id)
    assert not staged.is_terminal
    assert staged.phase is not Phase.INTAKE  # the first step happened
    # The next (unhurried) worker takes it from exactly there.
    done = service.process_next("worker-2")
    assert done is not None and done.status.value == "done"
    assert service.get(run_id).phase is Phase.COMPLETED
    conn.close()


def test_a_raised_drive_fails_the_run_in_words(tmp_path, monkeypatch):
    conn, service = _service(tmp_path)
    run_id = service.submit_async(TaskContract(intent="summarize"))

    def boom(state, *, on_step=None):
        raise RuntimeError("the sandbox burst")

    monkeypatch.setattr(service._orch, "run", boom)
    task = service.process_next("worker-1")
    # Never a silent lease-reclaim-raise loop: the run FAILS in the
    # exception's own words and the queue item is acknowledged.
    assert task is not None and task.status.value == "done"
    state = service.get(run_id)
    assert state.phase is Phase.FAILED
    assert "the sandbox burst" in state.failure_reason
    conn.close()


def test_resume_async_lands_the_decision_then_the_worker_drives(tmp_path):
    from oolu.skills.requirements import AuthorizationMode

    conn, service = _service(tmp_path, mode=AuthorizationMode.GUIDED)
    run_id = service.submit_async(TaskContract(intent="summarize"))
    service.process_next("worker-1")
    paused = service.get(run_id)
    assert paused.pause is not None
    assert paused.pause.kind is PauseKind.CONFIRMATION
    # The decision is durable BEFORE any drive — and the drive is queued,
    # not run here: a confirmed run must not die with the window.
    state = service.resume_async(
        run_id, ResumeInput(kind=PauseKind.CONFIRMATION, confirmed=True)
    )
    assert state.pause is None
    assert service.get(run_id).pause is None
    assert not state.is_terminal
    assert run_id in service.queued_run_ids()
    service.process_next("worker-1")
    assert service.get(run_id).phase is Phase.COMPLETED
    conn.close()


def test_restart_async_rebirths_a_failed_run_for_the_worker(tmp_path, monkeypatch):
    conn, service = _service(tmp_path)
    run_id = service.submit_async(TaskContract(intent="summarize"))

    def boom(state, *, on_step=None):
        raise RuntimeError("first life ends")

    monkeypatch.setattr(service._orch, "run", boom)
    service.process_next("worker-1")
    assert service.get(run_id).phase is Phase.FAILED
    monkeypatch.undo()
    state = service.restart_async(run_id)
    assert state.phase is Phase.INTAKE
    assert state.user_retries == 1
    assert run_id in service.queued_run_ids()
    service.process_next("worker-1")
    assert service.get(run_id).phase is Phase.COMPLETED
    conn.close()


# --------------------------------------------------------------------------- #
# The gateway: accepted, queued, driven, REPORTED.                             #
# --------------------------------------------------------------------------- #
def _host(tmp_path, scenario=_autonomous):
    app, conn, ident = _app(tmp_path, scenario)
    users = LocalUserStore(":memory:")
    accounts = LocalAccountService(users, ident.store, ident._signer)
    accounts.create_user("alice", "alice-password-1", tenant="t1")
    gateway = GatewayApp(
        app._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        accounts=accounts,
        settings_node=SettingsNode(SettingsStore(conn)),
        assistant_history=AssistantHistoryStore(conn),
        reminders=ReminderStore(conn),
    )
    return gateway, conn, ident


_TASK_ASK = "tidy the quarterly ledger"


def _ask(gateway, ident, *, node_id=None):
    body = {"message": _TASK_ASK, "history": []}
    if node_id:
        body["node_id"] = node_id
    reply = gateway.handle(
        _req("POST", "/v1/chat", token=ident.token("alice"), body=body)
    )
    assert reply.status == 200
    return reply.body


def test_a_chat_task_is_accepted_queued_then_reported(tmp_path):
    gateway, conn, ident = _host(tmp_path)
    body = _ask(gateway, ident)
    run_id = body["run_id"]
    # The turn returned with the run ACCEPTED, not driven: the client's
    # run card takes it from the queued phase.
    assert run_id
    assert body["run"]["phase"] == "intake"
    turns = gateway._assistant_history.history(tenant="t1", principal="alice")
    assert [t["kind"] for t in turns] == ["user", "assistant", "run"]
    assert turns[2]["body"] == run_id
    binding = gateway._run_reports.get(run_id)
    assert binding is not None and binding["reported_at"] is None
    # The worker's hands: drive the queue NOW.
    assert gateway.drive_queue() >= 1
    assert gateway._durable.get(run_id).phase is Phase.COMPLETED
    # The finish REPORTED to the thread that asked — and pinged the ring.
    turns = gateway._assistant_history.history(tenant="t1", principal="alice")
    assert [t["kind"] for t in turns] == ["user", "assistant", "run", "assistant"]
    assert "finished" in turns[-1]["body"]
    assert gateway._run_reports.get(run_id)["reported_at"] is not None
    upcoming = gateway._reminders.upcoming(tenant="t1", principal="alice")
    assert any("done" in r.text for r in upcoming)
    # Driving again reports nothing twice.
    gateway.drive_queue()
    assert len(
        gateway._assistant_history.history(tenant="t1", principal="alice")
    ) == 4
    conn.close()


def test_a_node_thread_gets_its_own_report(tmp_path):
    gateway, conn, ident = _host(tmp_path)
    body = _ask(gateway, ident, node_id="n-9")
    gateway.drive_queue()
    door = gateway.handle(
        _req(
            "GET",
            "/v1/chat/history",
            token=ident.token("alice"),
            query={"agent": "node:n-9"},
        )
    )
    kinds = [t["kind"] for t in door.body["items"]]
    assert kinds == ["user", "assistant", "run", "assistant"]
    # The main thread never heard about it.
    assert gateway._assistant_history.history(tenant="t1", principal="alice") == []
    assert body["run_id"]
    conn.close()


def test_the_confirmation_door_queues_the_drive(tmp_path):
    gateway, conn, ident = _host(tmp_path, scenario=_approval)
    alice = ident.token("alice")
    submitted = gateway.handle(
        _req("POST", "/v1/runs", token=alice, body={"intent": "approve-me"})
    )
    run_id = submitted.body["run_id"]
    assert submitted.body["awaiting"] == "confirmation"
    confirmed = gateway.handle(
        _req(
            "POST",
            f"/v1/runs/{run_id}/confirmation",
            token=alice,
            body={"approved": True},
        )
    )
    # The decision landed durably; the DRIVE belongs to the worker.
    assert confirmed.status == 200
    assert confirmed.body["awaiting"] is None
    assert run_id in gateway._durable.queued_run_ids()
    gateway.drive_queue()
    shown = gateway.handle(_req("GET", f"/v1/runs/{run_id}", token=alice))
    # The reserved action now waits on its approver — the next gate,
    # reached without any window holding the wheel.
    assert shown.body["awaiting"] == "approval"
    conn.close()


# --------------------------------------------------------------------------- #
# Restart re-drive: a killed process still owes its work.                      #
# --------------------------------------------------------------------------- #
def _reborn(gateway, ident, conn):
    users = LocalUserStore(":memory:")
    accounts = LocalAccountService(users, ident.store, ident._signer)
    return GatewayApp(
        gateway._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        accounts=accounts,
        settings_node=gateway._settings,
        assistant_history=gateway._assistant_history,
        reminders=gateway._reminders,
    )


def test_boot_reports_what_settled_unreported(tmp_path):
    gateway, conn, ident = _host(tmp_path)
    _ask(gateway, ident)
    # The drive happened (another worker, another life) but the report
    # never landed — the crash window between complete and report.
    task = gateway._durable.process_next("elsewhere")
    assert task is not None and task.status.value == "done"
    assert [
        t["kind"]
        for t in gateway._assistant_history.history(tenant="t1", principal="alice")
    ] == ["user", "assistant", "run"]
    reborn = _reborn(gateway, ident, conn)
    turns = reborn._assistant_history.history(tenant="t1", principal="alice")
    assert [t["kind"] for t in turns] == ["user", "assistant", "run", "assistant"]
    assert "finished" in turns[-1]["body"]
    conn.close()


def test_boot_requeues_a_run_that_lost_its_task(tmp_path):
    gateway, conn, ident = _host(tmp_path)
    body = _ask(gateway, ident)
    run_id = body["run_id"]
    # The crash ate the queue row (submit's two transactions tore).
    with gateway._durable.conn.transaction() as db:
        db.execute("DELETE FROM tasks")
    assert gateway._durable.queued_run_ids() == set()
    reborn = _reborn(gateway, ident, conn)
    # Boot put the drive back on the queue; the worker finishes and
    # reports as if nothing had died.
    assert run_id in reborn._durable.queued_run_ids()
    reborn.drive_queue()
    assert reborn._durable.get(run_id).phase is Phase.COMPLETED
    turns = reborn._assistant_history.history(tenant="t1", principal="alice")
    assert turns[-1]["kind"] == "assistant" and "finished" in turns[-1]["body"]
    conn.close()


def test_a_lost_run_row_is_reported_as_lost_not_owed_forever(tmp_path):
    gateway, conn, ident = _host(tmp_path)
    reports = RunReportStore(gateway._durable.conn)
    reports.file(
        run_id="gone-1",
        tenant="t1",
        principal="alice",
        agent="oolu",
        intent="vanish into the restart",
    )
    reborn = _reborn(gateway, ident, conn)
    turns = reborn._assistant_history.history(tenant="t1", principal="alice")
    assert len(turns) == 1 and "lost in a restart" in turns[0]["body"]
    assert reports.get("gone-1")["reported_at"] is not None
    conn.close()


# --------------------------------------------------------------------------- #
# The worker thread: the one deliberate daemon.                                #
# --------------------------------------------------------------------------- #
def test_the_worker_thread_drives_and_reports_by_itself(tmp_path):
    gateway, conn, ident = _host(tmp_path)
    gateway.start_worker(poll_seconds=0.02)
    try:
        _ask(gateway, ident)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            turns = gateway._assistant_history.history(
                tenant="t1", principal="alice"
            )
            if turns and turns[-1]["kind"] == "assistant" and len(turns) == 4:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("the worker never landed the report")
        assert "finished" in turns[-1]["body"]
    finally:
        gateway.stop_worker()
    conn.close()
