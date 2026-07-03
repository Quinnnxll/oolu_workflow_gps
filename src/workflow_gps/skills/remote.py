from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from ..worker.control_plane import ControlPlane, TaskRequest
from ..worker.leases import TrustLevel
from ..worker.worker import Worker
from .models import ActionEvent, ExecutionOutcome, ExecutionStatus


@runtime_checkable
class WorkerTransport(Protocol):
    def send(self, worker_id: str, lease_token: str, payload: dict) -> dict: ...


class InProcessWorkerTransport:
    def __init__(self, workers: dict[str, Worker], *, clock=None):
        self._workers = dict(workers)
        self._clock = clock

    def send(self, worker_id: str, lease_token: str, payload: dict) -> dict:
        worker = self._workers.get(worker_id)
        if worker is None:
            raise KeyError(f"unknown worker: {worker_id}")
        now = self._clock() if self._clock else None
        return worker.execute(lease_token, payload=payload, now=now)


class RemoteWorkerActionExecutor:
    def __init__(
        self,
        control_plane: ControlPlane,
        transport: WorkerTransport,
        *,
        tenant_id: str = "local",
        trust_level: TrustLevel = TrustLevel.UNTRUSTED_SYNTHESIZED,
        capabilities: frozenset[str] = frozenset({"run"}),
        timeout_seconds: float = 30.0,
        name: str = "worker",
        clock=None,
    ):
        self.name = name
        self._cp = control_plane
        self._transport = transport
        self._tenant_id = tenant_id
        self._trust_level = trust_level
        self._capabilities = frozenset(capabilities)
        self._timeout = timeout_seconds
        self._clock = clock
        self._lock = threading.RLock()
        self._completed: dict[str, ExecutionOutcome] = {}
        self._task_by_key: dict[str, str] = {}

    def capabilities(self) -> frozenset[str]:
        return self._capabilities

    def execute(self, action: ActionEvent, *, idempotency_key: str) -> ExecutionOutcome:
        with self._lock:
            if idempotency_key in self._completed:
                return self._completed[idempotency_key]
        if action.adapter != self.name or action.operation not in self._capabilities:
            return self._finish(
                action,
                idempotency_key,
                ExecutionStatus.BLOCKED,
                "unsupported worker action",
            )

        task_id = uuid4().hex
        payload = {
            "task_id": task_id,
            "action": action.model_dump(mode="json"),
            "idempotency_key": idempotency_key,
        }
        task = TaskRequest(
            task_id=task_id,
            tenant_id=self._tenant_id,
            capabilities=frozenset({action.operation}),
            trust_level=self._trust_level,
            payload=payload,
            timeout_seconds=self._timeout,
        )
        now = self._clock() if self._clock else None
        assignment = self._cp.dispatch(task, now=now)
        if assignment is None:
            # No healthy worker with capacity and a policy-permitted isolated backend.
            return self._finish(
                action,
                idempotency_key,
                ExecutionStatus.BLOCKED,
                "no eligible worker available",
            )
        with self._lock:
            self._task_by_key[idempotency_key] = task_id

        started = datetime.now(UTC)
        worker_id = assignment.worker_id
        try:
            result = self._transport.send(
                worker_id, assignment.lease_token, assignment.payload
            )
        except Exception as exc:  # noqa: BLE001 - a remote failure fails the run, never crashes
            self._cp.report_result(worker_id, task_id, success=False)
            return self._finish(
                action,
                idempotency_key,
                ExecutionStatus.FAILED,
                f"worker error: {exc}",
                started,
                worker_id=worker_id,
            )

        status_str = str(result.get("status", "succeeded"))
        success = status_str == "succeeded"
        self._cp.report_result(worker_id, task_id, success=success)
        evidence: dict[str, Any] = {
            "worker_id": worker_id,
            "trust_level": self._trust_level.value,
        }
        extra = result.get("evidence")
        evidence.update(extra if isinstance(extra, dict) else {"result": result})
        return self._finish(
            action,
            idempotency_key,
            ExecutionStatus.SUCCEEDED if success else ExecutionStatus.FAILED,
            result.get("error"),
            started,
            evidence=evidence,
            worker_id=worker_id,
        )

    def cancel(self, idempotency_key: str) -> None:
        with self._lock:
            task_id = self._task_by_key.get(idempotency_key)
        if task_id is not None:
            self._cp.cancel(task_id)

    def _finish(
        self,
        action: ActionEvent,
        idempotency_key: str,
        status: ExecutionStatus,
        error: str | None,
        started: datetime | None = None,
        evidence: dict[str, Any] | None = None,
        worker_id: str | None = None,
    ) -> ExecutionOutcome:
        started = started or datetime.now(UTC)
        outcome = ExecutionOutcome(
            idempotency_key=idempotency_key,
            skill_id=action.correlation_id,
            status=status,
            evidence=evidence or ({"worker_id": worker_id} if worker_id else {}),
            error=error,
            started_at=started,
            completed_at=datetime.now(UTC),
        )
        # Cache only settled success; BLOCKED/FAILED may become runnable on retry.
        if status is ExecutionStatus.SUCCEEDED:
            with self._lock:
                self._completed[idempotency_key] = outcome
        return outcome
