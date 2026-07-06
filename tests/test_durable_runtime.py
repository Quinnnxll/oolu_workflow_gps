"""Durability tests for the restart-safe runtime (codex/durable-runtime).

The two exit-gate guarantees are exercised directly:

1. Restarting processes neither loses nor duplicates a workflow — the durable
   queue reclaims a crashed worker's lease, and the idempotency ledger keeps the
   re-driven effect exactly-once.
2. Approval, incident, and audit records reconstruct the complete execution
   history from storage alone, with a verifiable (tamper-evident) audit chain.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from oolu.durable import (
    DurableAuditLog,
    DurableConnection,
    DurableTaskQueue,
    DurableWorkflowService,
    FilesystemArtifactStore,
    IdempotencyLedger,
    TaskStatus,
    TransactionalOutbox,
    backup,
    delete_workflow,
    prune_retention,
    restore,
)
from oolu.orchestrator import (
    ActionExecutorRouteRunner,
    Blueprint,
    BoundedRetryRecovery,
    CapabilityGrounder,
    CollectingFeedbackSink,
    LeastCostRouteOptimizer,
    PauseKind,
    Phase,
    ReservedAction,
    ResumeInput,
    RiskBasedHumanControl,
    StaticIntaker,
    StatusOutcomeMonitor,
    TaskContract,
    WorkflowOrchestrator,
)
from oolu.skills.models import (
    ActionEvent,
    ApprovalRecord,
    ExecutionOutcome,
    ExecutionStatus,
)
from oolu.skills.requirements import (
    AuthorizationGrant,
    AuthorizationMode,
    ParameterDomain,
    ParameterSource,
    RequirementBrief,
    RequirementParameter,
)

# A far-future base time so a task enqueued at the real "now" is always already
# available; the relative offsets below still drive lease-expiry behaviour.
T0 = datetime(2099, 1, 1, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Shared builders.                                                            #
# --------------------------------------------------------------------------- #
class ScriptedActionExecutor:
    name = "test"

    def __init__(self, capabilities: set[str], *, fail_times: int = 0):
        self._caps = frozenset(capabilities)
        self._fail_times = fail_times
        self.calls = 0

    def capabilities(self) -> frozenset[str]:
        return self._caps

    def execute(self, action: ActionEvent, *, idempotency_key: str) -> ExecutionOutcome:
        self.calls += 1
        status = (
            ExecutionStatus.FAILED
            if self.calls <= self._fail_times
            else ExecutionStatus.SUCCEEDED
        )
        return ExecutionOutcome(
            idempotency_key=idempotency_key,
            skill_id=action.correlation_id,
            status=status,
            error="transient" if status is ExecutionStatus.FAILED else None,
        )

    def cancel(self, idempotency_key: str) -> None:
        return None


def _param(name: str, *, value=None) -> RequirementParameter:
    if value is None:
        return RequirementParameter(
            name=name,
            description=name,
            domain=ParameterDomain(value_type="str"),
            required=True,
        )
    return RequirementParameter(
        name=name,
        description=name,
        domain=ParameterDomain(value_type="str"),
        required=True,
        value=value,
        source=ParameterSource.USER,
    )


def _blueprint(*, operation, capability, reserved, risk) -> Blueprint:
    action = ActionEvent(correlation_id="c1", adapter="test", operation=operation)
    return Blueprint(
        name=f"{operation}-route",
        actions=[
            ReservedAction(
                action=action,
                required_capabilities=frozenset({capability}),
                reserved=reserved,
                risk=risk,
            )
        ],
        estimated_cost=1.0,
    )


def _factory(*, brief, blueprint, executor, grounding_map, dual_risks=frozenset()):
    def build(events):
        return WorkflowOrchestrator(
            intaker=StaticIntaker(brief),
            grounder=CapabilityGrounder(grounding_map),
            optimizer=LeastCostRouteOptimizer([blueprint]),
            human_control=RiskBasedHumanControl(dual_approval_risks=dual_risks),
            executor=ActionExecutorRouteRunner({"test": executor}),
            monitor=StatusOutcomeMonitor(),
            recovery=BoundedRetryRecovery(),
            feedback=CollectingFeedbackSink(),
            events=events,
        )

    return build


# --------------------------------------------------------------------------- #
# Durable task queue.                                                         #
# --------------------------------------------------------------------------- #
def test_queue_enqueue_lease_complete(tmp_path):
    conn = DurableConnection(tmp_path / "d.db")
    queue = DurableTaskQueue(conn)
    task = queue.enqueue("work", {"x": 1})
    leased = queue.lease("worker-a", now=T0)
    assert leased is not None and leased.task_id == task.task_id
    assert leased.status is TaskStatus.LEASED and leased.attempts == 1
    # No other task is available.
    assert queue.lease("worker-b", now=T0) is None
    assert queue.complete(leased.task_id, "worker-a", result={"ok": True})
    assert queue.get(task.task_id).status is TaskStatus.DONE
    conn.close()


def test_queue_enqueue_is_idempotent(tmp_path):
    conn = DurableConnection(tmp_path / "d.db")
    queue = DurableTaskQueue(conn)
    a = queue.enqueue("work", {"x": 1}, idempotency_key="k1")
    b = queue.enqueue("work", {"x": 2}, idempotency_key="k1")
    assert a.task_id == b.task_id  # second enqueue did not create a duplicate
    conn.close()


def test_queue_retry_then_dead(tmp_path):
    conn = DurableConnection(tmp_path / "d.db")
    queue = DurableTaskQueue(conn)
    queue.enqueue("work", {}, max_attempts=2)
    first = queue.lease("w", now=T0)
    requeued = queue.fail(first.task_id, "w", error="boom", now=T0)
    assert requeued.status is TaskStatus.PENDING
    second = queue.lease("w", now=T0)
    dead = queue.fail(second.task_id, "w", error="boom again", now=T0)
    assert dead.status is TaskStatus.DEAD  # exhausted max_attempts
    conn.close()


def test_queue_heartbeat_prevents_reclaim(tmp_path):
    conn = DurableConnection(tmp_path / "d.db")
    queue = DurableTaskQueue(conn)
    task = queue.enqueue("work", {})
    queue.lease("w", lease_seconds=30, now=T0)
    # Heartbeat extends the lease beyond the reclaim check.
    assert queue.heartbeat(
        task.task_id, "w", lease_seconds=30, now=T0 + timedelta(seconds=20)
    )
    assert queue.reclaim_expired(now=T0 + timedelta(seconds=40)) == []
    assert queue.get(task.task_id).status is TaskStatus.LEASED
    conn.close()


def test_queue_cancel(tmp_path):
    conn = DurableConnection(tmp_path / "d.db")
    queue = DurableTaskQueue(conn)
    task = queue.enqueue("work", {})
    assert queue.cancel(task.task_id)
    assert queue.get(task.task_id).status is TaskStatus.CANCELLED
    assert queue.lease("w", now=T0) is None
    conn.close()


# --------------------------------------------------------------------------- #
# Exit gate #1: a crashed worker's task is neither lost nor duplicated.        #
# --------------------------------------------------------------------------- #
def test_crashed_worker_task_is_reclaimed_and_run_exactly_once(tmp_path):
    db = tmp_path / "d.db"
    effect_calls = {"n": 0}

    def effect():
        effect_calls["n"] += 1
        return {"done": True}

    # Worker A leases the task, performs the effect once, then "crashes" before
    # acknowledging (the process dies — we close the connection).
    conn = DurableConnection(db)
    queue = DurableTaskQueue(conn)
    ledger = IdempotencyLedger(conn)
    task = queue.enqueue("charge", {"run": "r1"})
    leased = queue.lease("worker-a", lease_seconds=30, now=T0)
    assert leased is not None
    ledger.run("effect:r1", effect)
    assert effect_calls["n"] == 1
    conn.close()  # crash

    # A new process reopens the same durable database, reclaims the expired lease,
    # and re-drives the task. The effect must NOT run a second time.
    conn2 = DurableConnection(db)
    queue2 = DurableTaskQueue(conn2)
    ledger2 = IdempotencyLedger(conn2)
    reclaimed = queue2.reclaim_expired(now=T0 + timedelta(seconds=60))
    assert task.task_id in reclaimed  # not lost
    released = queue2.lease("worker-b", now=T0 + timedelta(seconds=60))
    assert released is not None and released.task_id == task.task_id
    ledger2.run("effect:r1", effect)
    assert effect_calls["n"] == 1  # not duplicated
    assert queue2.complete(released.task_id, "worker-b")
    assert queue2.get(task.task_id).status is TaskStatus.DONE
    conn2.close()


def test_idempotency_ledger_runs_once(tmp_path):
    conn = DurableConnection(tmp_path / "d.db")
    ledger = IdempotencyLedger(conn)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        return {"v": calls["n"]}

    first = ledger.run("k", fn)
    second = ledger.run("k", fn)
    assert first == second == {"v": 1}
    assert calls["n"] == 1
    assert ledger.seen("k")
    conn.close()


# --------------------------------------------------------------------------- #
# Transactional outbox.                                                       #
# --------------------------------------------------------------------------- #
def test_outbox_stages_with_state_and_relays_once(tmp_path):
    db = tmp_path / "d.db"
    conn = DurableConnection(db)
    outbox = TransactionalOutbox(conn)
    # A state change and its event commit together.
    with conn.transaction() as handle:
        handle.execute(
            "INSERT INTO accounts (account_id, payload_json, created_at, updated_at)"
            " VALUES ('a1', '{}', '2026', '2026')"
        )
        outbox.stage(handle, "account.created", {"account_id": "a1"})
    conn.close()

    # Survives reopen: the message is still pending until relayed.
    conn2 = DurableConnection(db)
    outbox2 = TransactionalOutbox(conn2)
    assert len(outbox2.pending()) == 1
    delivered: list = []
    assert outbox2.relay(delivered.append) == 1
    assert delivered[0].topic == "account.created"
    # A second relay delivers nothing (already sent).
    assert outbox2.relay(delivered.append) == 0
    conn2.close()


def test_outbox_failed_delivery_stays_pending(tmp_path):
    conn = DurableConnection(tmp_path / "d.db")
    outbox = TransactionalOutbox(conn)
    outbox.publish("topic", {"k": "v"})

    def failing(_message):
        raise RuntimeError("sink down")

    assert outbox.relay(failing) == 0
    assert len(outbox.pending()) == 1  # retried later
    conn.close()


# --------------------------------------------------------------------------- #
# Hash-linked audit log.                                                      #
# --------------------------------------------------------------------------- #
def test_audit_chain_verifies_and_detects_tampering(tmp_path):
    db = tmp_path / "d.db"
    conn = DurableConnection(db)
    audit = DurableAuditLog(conn)
    audit.append("a", {"run_id": "r1", "n": 1})
    audit.append("b", {"run_id": "r1", "n": 2})
    audit.append("c", {"run_id": "r2", "n": 3})
    assert audit.verify()
    assert [r.event_type for r in audit.records(run_id="r1")] == ["a", "b"]

    # Tamper with a stored payload; the chain must no longer verify.
    with conn.transaction() as handle:
        handle.execute(
            "UPDATE audit_log SET payload_json = ? WHERE event_type = 'b'",
            ('{"run_id": "r1", "n": 999}',),
        )
    assert not audit.verify()
    conn.close()


# --------------------------------------------------------------------------- #
# Content-addressed object storage.                                          #
# --------------------------------------------------------------------------- #
def test_artifact_store_is_content_addressed_and_dedupes(tmp_path):
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    ref1 = store.put("evidence-1", b"hello world", media_type="text/plain")
    ref2 = store.put("evidence-2", b"hello world", media_type="text/plain")
    assert ref1 == ref2  # identical bytes deduplicate
    assert store.get(ref1) == b"hello world"
    assert store.exists(ref1)
    assert store.delete(ref1)
    assert not store.exists(ref1)


# --------------------------------------------------------------------------- #
# Exit gate: service survives restart mid-pause and resumes to completion.     #
# --------------------------------------------------------------------------- #
def test_service_run_survives_restart_and_resumes(tmp_path):
    db = tmp_path / "d.db"
    brief = RequirementBrief(
        intent="apply change",
        parameters=[_param("target", value="prod")],
        authorization=AuthorizationGrant(mode=AuthorizationMode.GUIDED),
    )
    factory = _factory(
        brief=brief,
        blueprint=_blueprint(
            operation="write", capability="write", reserved=False, risk="write"
        ),
        executor=ScriptedActionExecutor({"write"}),
        grounding_map={"target": "write"},
    )

    conn = DurableConnection(db)
    service = DurableWorkflowService(conn, factory)
    state = service.submit(TaskContract(intent="apply change"))
    run_id = state.run_id
    assert state.pause.kind is PauseKind.CONFIRMATION
    conn.close()  # restart

    conn2 = DurableConnection(db)
    service2 = DurableWorkflowService(conn2, factory)
    loaded = service2.get(run_id)
    assert loaded is not None and loaded.pause.kind is PauseKind.CONFIRMATION
    done = service2.resume(
        run_id, ResumeInput(kind=PauseKind.CONFIRMATION, confirmed=True)
    )
    assert done.phase is Phase.COMPLETED
    conn2.close()


def test_service_async_worker_drives_run(tmp_path):
    db = tmp_path / "d.db"
    brief = RequirementBrief(
        intent="summarize",
        parameters=[_param("format", value="md")],
        authorization=AuthorizationGrant(mode=AuthorizationMode.FULLY_DELEGATED),
    )
    factory = _factory(
        brief=brief,
        blueprint=_blueprint(
            operation="render", capability="render", reserved=False, risk="read"
        ),
        executor=ScriptedActionExecutor({"render"}),
        grounding_map={"format": "render"},
    )
    conn = DurableConnection(db)
    service = DurableWorkflowService(conn, factory)
    run_id = service.submit_async(TaskContract(intent="summarize"))
    task = service.process_next("worker-a")
    assert task is not None and task.status is TaskStatus.DONE
    assert service.get(run_id).phase is Phase.COMPLETED
    conn.close()


# --------------------------------------------------------------------------- #
# Exit gate #2: approval + incident + audit reconstruct the full history.      #
# --------------------------------------------------------------------------- #
def test_history_reconstructs_from_storage_after_restart(tmp_path):
    db = tmp_path / "d.db"
    brief = RequirementBrief(
        intent="delete dataset",
        parameters=[_param("dataset", value="logs")],
        authorization=AuthorizationGrant(mode=AuthorizationMode.FULLY_DELEGATED),
    )
    # Reserved + always-failing so the run needs approval and then escalates.
    factory = _factory(
        brief=brief,
        blueprint=_blueprint(
            operation="delete", capability="delete", reserved=True, risk="irreversible"
        ),
        executor=ScriptedActionExecutor({"delete"}, fail_times=99),
        grounding_map={"dataset": "delete"},
    )
    conn = DurableConnection(db)
    service = DurableWorkflowService(conn, factory)
    state = service.submit(
        TaskContract(intent="delete dataset"), max_recovery_attempts=1
    )
    run_id = state.run_id
    assert state.pause.kind is PauseKind.APPROVAL

    state = service.resume(
        run_id,
        ResumeInput(
            kind=PauseKind.APPROVAL,
            approvals=[
                ApprovalRecord(principal="alice", policy="delete", decision="approved")
            ],
        ),
    )
    assert state.pause.kind is PauseKind.INCIDENT
    state = service.resume(
        run_id, ResumeInput(kind=PauseKind.INCIDENT, incident_decision="abort")
    )
    assert state.phase is Phase.FAILED
    conn.close()

    # Reconstruct the entire history from storage in a fresh process.
    conn2 = DurableConnection(db)
    service2 = DurableWorkflowService(conn2, factory)
    history = service2.reconstruct_history(run_id)
    assert history["phase"] == "failed"
    assert len(history["approvals"]) == 1
    assert history["approvals"][0].principal == "alice"
    assert len(history["incidents"]) == 1
    assert history["incidents"][0].resolution == "aborted"
    assert len(history["routes"]) == 1
    assert len(history["semantic_evidence"]) == 1
    assert history["audit_verified"] is True
    event_types = {record.event_type for record in history["audit"]}
    assert "workflow.started" in event_types
    assert "workflow.incident" in event_types
    conn2.close()


# --------------------------------------------------------------------------- #
# Backup / restore / retention / deletion.                                   #
# --------------------------------------------------------------------------- #
def test_backup_and_restore_roundtrip(tmp_path):
    src = DurableConnection(tmp_path / "live.db")
    DurableTaskQueue(src).enqueue("work", {"x": 1})
    backup_path = backup(src, tmp_path / "backup.db")
    src.close()

    restored = restore(backup_path)
    queue = DurableTaskQueue(restored)
    leased = queue.lease("w", now=T0)
    assert leased is not None and leased.payload == {"x": 1}
    restored.close()


def test_retention_prunes_old_terminal_records(tmp_path):
    conn = DurableConnection(tmp_path / "d.db")
    queue = DurableTaskQueue(conn)
    task = queue.enqueue("work", {})
    leased = queue.lease("w", now=T0)
    queue.complete(leased.task_id, "w")
    # The completed task is old relative to "now"; retention removes it.
    removed = prune_retention(
        conn, older_than_days=1, now=datetime.now(UTC) + timedelta(days=2)
    )
    assert removed["tasks"] == 1
    assert queue.get(task.task_id) is None
    conn.close()


def test_delete_workflow_keeps_audit_chain_verifiable(tmp_path):
    db = tmp_path / "d.db"
    conn = DurableConnection(db)
    audit = DurableAuditLog(conn)
    audit.append("workflow.started", {"run_id": "r1"})
    audit.append("workflow.started", {"run_id": "r2"})

    with conn.transaction() as handle:
        handle.execute(
            "INSERT INTO approvals (approval_id, run_id, principal, policy, decision,"
            " payload_json, created_at) VALUES ('ap1', 'r1', 'p', 'pol', 'approved', '{}', '2026')"
        )
    counts = delete_workflow(conn, "r1")
    assert counts["approvals"] == 1
    # The mutable record is gone, but the tamper-evident audit chain still verifies.
    assert audit.verify()
    assert {r.run_id for r in audit.records()} == {"r1", "r2"}
    conn.close()
