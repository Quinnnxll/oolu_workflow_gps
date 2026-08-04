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


class WorkerDrain(Exception):
    """Raised inside the drive's step hook when the worker was asked to
    stop (V1's graceful drain): the just-finished step is already staged
    — that row IS the checkpoint — and the task's lease is handed back,
    so the next worker (or the next boot) re-drives from exactly here."""


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
    #                                                                     #
    # The executing snapshot (V0): the run row is staged BEFORE the drive #
    # and refreshed after every phase step, so a concurrent status read   #
    # answers honestly WHILE the run executes — no more "no row until the #
    # first pause". Intermediate stages are runs-only (no outbox noise);  #
    # the terminal/pause checkpoint keeps its announcement, as ever.      #
    # ------------------------------------------------------------------ #
    def submit(
        self,
        contract: TaskContract,
        *,
        max_recovery_attempts: int = 1,
        on_progress: Callable[[RunState], None] | None = None,
    ) -> RunState:
        state = self._orch.prepare(
            contract, max_recovery_attempts=max_recovery_attempts
        )
        self._stage_progress(state)
        state = self._orch.run(state, on_step=self._step_hook(on_progress))
        self._checkpoint(state, "workflow.submitted")
        return state

    def resume(
        self,
        run_id: str,
        resume: ResumeInput,
        *,
        on_progress: Callable[[RunState], None] | None = None,
    ) -> RunState:
        state = self.runs.get(run_id)
        if state is None:
            raise KeyError(f"unknown run: {run_id}")
        # The decision lands durably BEFORE the drive: a poll stops
        # showing a stale "needs a decision" the moment it was made.
        state = self._orch.apply_resume(state, resume)
        self._stage_progress(state)
        state = self._orch.run(state, on_step=self._step_hook(on_progress))
        self._checkpoint(state, "workflow.resumed")
        return state

    def restart(
        self,
        run_id: str,
        *,
        on_progress: Callable[[RunState], None] | None = None,
    ) -> RunState:
        """Re-drive a FAILED run in its own thread instead of minting a
        sibling — the retry lives where the failure lives."""
        state = self.runs.get(run_id)
        if state is None:
            raise KeyError(f"unknown run: {run_id}")
        state = self._orch.restart(state, on_step=self._step_hook(on_progress))
        self._checkpoint(state, "workflow.restarted")
        return state

    def _stage_progress(self, state: RunState) -> None:
        """Stage the run row alone — the honest mid-drive snapshot."""
        with self.conn.transaction() as db:
            self.runs.stage(db, state)

    def _step_hook(
        self, on_progress: Callable[[RunState], None] | None
    ) -> Callable[[RunState], None]:
        def hook(state: RunState) -> None:
            self._stage_progress(state)
            if on_progress is not None:
                try:
                    on_progress(state)
                except Exception:  # noqa: BLE001 - a progress ear never stops the run
                    pass

        return hook

    def get(self, run_id: str) -> RunState | None:
        return self.runs.get(run_id)

    # ------------------------------------------------------------------ #
    # Asynchronous driving via the durable queue (V1: the chat lane's    #
    # path — accepted work survives the request, the window, and the     #
    # process, because the queue row and the staged run row do).         #
    # ------------------------------------------------------------------ #
    def submit_async(
        self, contract: TaskContract, *, max_recovery_attempts: int = 1
    ) -> str:
        # prepare() (not a bare RunState) so the async path emits the
        # same workflow.started event the synchronous path always has.
        state = self._orch.prepare(
            contract, max_recovery_attempts=max_recovery_attempts
        )
        self.runs.save(state)
        # Idempotent enqueue keyed by the run, so a retried submission is a no-op.
        self.queue.enqueue(
            _ADVANCE,
            {"run_id": state.run_id},
            idempotency_key=f"advance:{state.run_id}",
        )
        return state.run_id

    def resume_async(self, run_id: str, resume: ResumeInput) -> RunState:
        """Fold the human's decision in durably and QUEUE the drive.

        The decision lands before anything else (a poll stops showing
        "needs a decision" the moment it was made), and the drive itself
        belongs to the worker — a confirmed run must not die with the
        window that confirmed it. The enqueue is keyless: the
        ``advance:`` idempotency key belongs to the submission, and a
        second advance for the same run is a harmless no-op drive."""
        state = self.runs.get(run_id)
        if state is None:
            raise KeyError(f"unknown run: {run_id}")
        state = self._orch.apply_resume(state, resume)
        self._stage_progress(state)
        self.queue.enqueue(_ADVANCE, {"run_id": run_id})
        return state

    def restart_async(self, run_id: str) -> RunState:
        """Reset a FAILED run in place durably and QUEUE the fresh drive."""
        state = self.runs.get(run_id)
        if state is None:
            raise KeyError(f"unknown run: {run_id}")
        state = self._orch.apply_restart(state)
        self._stage_progress(state)
        self.queue.enqueue(_ADVANCE, {"run_id": run_id})
        return state

    def ensure_queued(self, run_id: str) -> None:
        """Boot recovery's hand: a run that is owed a drive but has no
        standing queue task gets one. Keyless — the caller has already
        checked ``queued_run_ids()``, and the submission's idempotency
        key may be spent on a task that already ran once."""
        self.queue.enqueue(_ADVANCE, {"run_id": run_id})

    def queued_run_ids(self) -> set[str]:
        """Every run with a pending or leased advance task — the work
        the queue still owes, as the boot scan reads it."""
        out: set[str] = set()
        for task in self.queue.active(_ADVANCE):
            run_id = (task.payload or {}).get("run_id")
            if run_id:
                out.add(str(run_id))
        return out

    def process_next(
        self,
        owner: str,
        *,
        lease_seconds: float = 30.0,
        on_progress: Callable[[RunState], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> Task | None:
        """Reclaim any expired work, lease one task, drive it, and acknowledge it.

        V1 hardening: the drive heartbeats its lease after every phase
        step (a live worker never loses in-flight work to the reclaim; a
        dead one loses it within one lease); ``should_stop`` drains
        gracefully between steps (the task's lease is handed back with
        the attempt refunded, the staged row is the checkpoint); and a
        drive that RAISES fails the run in the exception's own words
        instead of looping the lease-reclaim-raise cycle forever."""
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

        def hook(step_state: RunState) -> None:
            self._stage_progress(step_state)
            self.queue.heartbeat(task.task_id, owner, lease_seconds=lease_seconds)
            if on_progress is not None:
                try:
                    on_progress(step_state)
                except Exception:  # noqa: BLE001 - a progress ear never stops the run
                    pass
            if should_stop is not None and should_stop():
                raise WorkerDrain()

        try:
            state = self._orch.run(state, on_step=hook)
        except WorkerDrain:
            self.queue.release(task.task_id, owner)
            return self.queue.get(task.task_id)
        except Exception as exc:  # noqa: BLE001 - the honest verdict: a raised
            # drive becomes a FAILED run in words, never an eternal retry.
            state = self._orch.abort(state, str(exc) or exc.__class__.__name__)
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
