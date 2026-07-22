"""The durable workflow service: restart-safe orchestration over the durable stores.

The service wraps a :class:`WorkflowOrchestrator` with durable state, a task queue,
a transactional outbox, an idempotency ledger, a hash-linked audit log, and the
domain record stores. A run-state checkpoint and the outbox message announcing it
commit together; in async mode a worker leases an ``advance`` task, drives the run
from its last checkpoint, and acknowledges it. If a worker dies, its lease expires,
the task is reclaimed, and the run is re-driven from the last checkpoint — and
because execution effects are idempotent, that re-drive cannot duplicate them.
"""

from __future__ import annotations

from collections.abc import Callable

from ..orchestrator.engine import WorkflowOrchestrator
from ..orchestrator.state import ResumeInput, RunState, TaskContract
from .artifacts import FilesystemArtifactStore
from .audit import DurableAuditLog
from .connection import DurableConnection
from .idempotency import IdempotencyLedger
from .outbox import TransactionalOutbox
from .queue import DurableTaskQueue, Task
from .records import DurableRecordStore, DurableRunStateStore

# The factory receives the service's durable audit log as the orchestrator's event
# sink, so every workflow lifecycle event lands in the tamper-evident chain.
OrchestratorFactory = Callable[[DurableAuditLog], WorkflowOrchestrator]

_ADVANCE = "workflow.advance"


class DurableWorkflowService:
    def __init__(
        self,
        conn: DurableConnection,
        orchestrator_factory: OrchestratorFactory,
        *,
        artifacts: FilesystemArtifactStore | None = None,
    ):
        self.conn = conn
        self.runs = DurableRunStateStore(conn)
        self.queue = DurableTaskQueue(conn)
        self.outbox = TransactionalOutbox(conn)
        self.idempotency = IdempotencyLedger(conn)
        self.audit = DurableAuditLog(conn)
        self.records = DurableRecordStore(conn)
        self.artifacts = artifacts
        # The orchestrator emits its lifecycle events into the durable audit log.
        self._orch = orchestrator_factory(self.audit)

    # ------------------------------------------------------------------ #
    # Synchronous driving (one call advances to the next pause/terminal). #
    # ------------------------------------------------------------------ #
    def submit(
        self, contract: TaskContract, *, max_recovery_attempts: int = 1
    ) -> RunState:
        state = self._orch.start(contract, max_recovery_attempts=max_recovery_attempts)
        self._checkpoint(state, "workflow.submitted")
        return state

    def resume(self, run_id: str, resume: ResumeInput) -> RunState:
        state = self.runs.get(run_id)
        if state is None:
            raise KeyError(f"unknown run: {run_id}")
        state = self._orch.resume(state, resume)
        self._checkpoint(state, "workflow.resumed")
        return state

    def restart(self, run_id: str) -> RunState:
        """Re-drive a FAILED run in its own thread instead of minting a
        sibling — the retry lives where the failure lives."""
        state = self.runs.get(run_id)
        if state is None:
            raise KeyError(f"unknown run: {run_id}")
        state = self._orch.restart(state)
        self._checkpoint(state, "workflow.restarted")
        return state

    def get(self, run_id: str) -> RunState | None:
        return self.runs.get(run_id)

    # ------------------------------------------------------------------ #
    # Asynchronous driving via the durable queue.                         #
    # ------------------------------------------------------------------ #
    def submit_async(
        self, contract: TaskContract, *, max_recovery_attempts: int = 1
    ) -> str:
        state = RunState(
            intent=contract.intent,
            contract=contract,
            max_recovery_attempts=max_recovery_attempts,
        )
        self.runs.save(state)
        # Idempotent enqueue keyed by the run, so a retried submission is a no-op.
        self.queue.enqueue(
            _ADVANCE,
            {"run_id": state.run_id},
            idempotency_key=f"advance:{state.run_id}",
        )
        return state.run_id

    def process_next(self, owner: str, *, lease_seconds: float = 30.0) -> Task | None:
        """Reclaim any expired work, lease one task, drive it, and acknowledge it."""
        self.queue.reclaim_expired()
        task = self.queue.lease(owner, lease_seconds=lease_seconds)
        if task is None:
            return None
        if task.kind != _ADVANCE:
            self.queue.fail(task.task_id, owner, error=f"unknown kind: {task.kind}")
            return task
        run_id = task.payload["run_id"]
        state = self.runs.get(run_id)
        if state is None:
            self.queue.fail(task.task_id, owner, error=f"missing run: {run_id}")
            return task
        state = self._orch.run(state)
        self._checkpoint(state, "workflow.advanced")
        self.queue.complete(task.task_id, owner, result={"phase": state.phase.value})
        return self.queue.get(task.task_id)

    # ------------------------------------------------------------------ #
    # History reconstruction (storage-only).                              #
    # ------------------------------------------------------------------ #
    def reconstruct_history(self, run_id: str) -> dict:
        """Rebuild the complete execution history from storage alone."""
        state = self.runs.get(run_id)
        if state is None:
            raise KeyError(f"unknown run: {run_id}")
        return {
            "run_id": run_id,
            "phase": state.phase.value,
            "routes": self.records.list_routes(run_id),
            "approvals": self.records.list_approvals(run_id),
            "incidents": self.records.list_incidents(run_id),
            "semantic_evidence": self.records.list_semantic_evidence(run_id),
            "execution_outcomes": self.records.list_execution_outcomes(run_id),
            "audit": self.audit.records(run_id=run_id),
            "audit_verified": self.audit.verify(),
        }

    # ------------------------------------------------------------------ #
    def _checkpoint(self, state: RunState, event: str) -> None:
        # The checkpoint and its announcement commit atomically.
        with self.conn.transaction() as db:
            self.runs.stage(db, state)
            self.outbox.stage(
                db,
                event,
                {"run_id": state.run_id, "phase": state.phase.value},
            )
        self._sync_records(state)

    def _sync_records(self, state: RunState) -> None:
        # Idempotent record syncing (INSERT OR IGNORE / upsert), eventually
        # consistent with the checkpoint; safe to repeat after a re-drive.
        run_id = state.run_id
        if state.route is not None:
            self.records.save_route(run_id, state.route)
        if state.grounding is not None:
            self.records.save_semantic_evidence(run_id, state.grounding)
        for approval in state.approvals:
            self.records.save_approval(run_id, approval)
        for incident in state.incidents:
            self.records.save_incident(run_id, incident)
        if state.execution is not None:
            self.records.save_execution_outcome(run_id, state.execution)
