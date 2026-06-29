"""The control plane: planning and dispatch, separated from privileged execution.

The control plane assigns tasks to workers and issues signed leases, but it holds
**no execution backend and no desktop/provider credentials** — it cannot run code.
Workers (and outbound-only local agents) hold those. Dispatch respects worker
health, capacity, and capabilities; cancellation revokes the lease so the target
worker's verification fails; repeated failures quarantine a worker.

Workers pull their assignments (``poll``), which models an outbound-only agent that
opens connections to the control plane and never accepts inbound ones.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .leases import LeaseSigner, TrustLevel, WorkerTaskLease
from .ledger import LeaseLedger
from .policy import IsolationPolicy


class WorkerKind(str, Enum):
    CLOUD = "cloud"
    LOCAL_AGENT = "local_agent"


class WorkerInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    worker_id: str
    kind: WorkerKind = WorkerKind.CLOUD
    capabilities: frozenset[str] = Field(default_factory=frozenset)
    backend_kind: str = "docker"
    max_concurrency: int = 1


class TaskRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_id: str = Field(default_factory=lambda: uuid4().hex)
    run_id: str | None = None
    tenant_id: str
    capabilities: frozenset[str] = Field(default_factory=frozenset)
    trust_level: TrustLevel
    payload: dict = Field(default_factory=dict)
    timeout_seconds: float = 30.0


class Assignment(BaseModel):
    model_config = ConfigDict(frozen=True)

    worker_id: str
    task_id: str
    lease_token: str
    payload: dict


class ControlPlane:
    def __init__(
        self,
        signer: LeaseSigner,
        *,
        ledger: LeaseLedger,
        isolation: IsolationPolicy | None = None,
        default_lease_ttl: float = 300.0,
        failure_quarantine_threshold: int = 3,
    ):
        self._signer = signer
        self._ledger = ledger
        self._isolation = isolation or IsolationPolicy()
        self._ttl = default_lease_ttl
        self._quarantine_threshold = failure_quarantine_threshold

        self._workers: dict[str, WorkerInfo] = {}
        self._healthy: dict[str, bool] = {}
        self._load: dict[str, int] = defaultdict(int)
        self._failures: dict[str, int] = defaultdict(int)
        self._quarantined: set[str] = set()
        self._pending: dict[str, list[Assignment]] = defaultdict(list)
        self._lease_to_worker: dict[str, str] = {}
        self._task_to_lease: dict[str, str] = {}

    # NOTE: deliberately no `execute`, no backend, no credential store.

    # --- worker registry / health --------------------------------------- #
    def register_worker(self, info: WorkerInfo) -> None:
        self._workers[info.worker_id] = info
        self._healthy[info.worker_id] = True

    def heartbeat(self, worker_id: str, *, healthy: bool = True) -> None:
        self._healthy[worker_id] = healthy

    def quarantine(self, worker_id: str) -> None:
        self._quarantined.add(worker_id)

    def is_quarantined(self, worker_id: str) -> bool:
        return worker_id in self._quarantined

    def available_capacity(self, worker_id: str) -> int:
        info = self._workers.get(worker_id)
        if info is None:
            return 0
        return max(0, info.max_concurrency - self._load[worker_id])

    # --- dispatch -------------------------------------------------------- #
    def dispatch(
        self, task: TaskRequest, *, now: datetime | None = None
    ) -> Assignment | None:
        """Assign a task to a healthy worker with capacity; return the lease, or None."""
        moment = now or datetime.now(UTC)
        worker = self._select_worker(task)
        if worker is None:
            return None

        backend_required = self._isolation.required_backend(task.trust_level)
        lease = WorkerTaskLease(
            task_id=task.task_id,
            run_id=task.run_id,
            tenant_id=task.tenant_id,
            audience=worker.worker_id,
            capabilities=task.capabilities,
            trust_level=task.trust_level,
            backend_required=backend_required,
            timeout_seconds=task.timeout_seconds,
            issued_at=moment,
            expires_at=moment + timedelta(seconds=self._ttl),
        )
        token = self._signer.sign(lease)
        assignment = Assignment(
            worker_id=worker.worker_id,
            task_id=task.task_id,
            lease_token=token,
            payload=task.payload,
        )
        self._pending[worker.worker_id].append(assignment)
        self._load[worker.worker_id] += 1
        self._lease_to_worker[lease.lease_id] = worker.worker_id
        self._task_to_lease[task.task_id] = lease.lease_id
        return assignment

    def poll(self, worker_id: str) -> Assignment | None:
        """A worker pulls its next assignment (outbound-only)."""
        queue = self._pending.get(worker_id) or []
        if not queue:
            return None
        return queue.pop(0)

    def report_result(self, worker_id: str, task_id: str, *, success: bool) -> None:
        self._load[worker_id] = max(0, self._load[worker_id] - 1)
        if success:
            self._failures[worker_id] = 0
            return
        self._failures[worker_id] += 1
        if self._failures[worker_id] >= self._quarantine_threshold:
            self.quarantine(worker_id)

    def cancel(self, task_id: str) -> bool:
        """Cancel a dispatched task by revoking its lease (it can no longer execute)."""
        lease_id = self._task_to_lease.get(task_id)
        if lease_id is None:
            return False
        self._ledger.revoke(lease_id)
        return True

    # --------------------------------------------------------------------- #
    def _select_worker(self, task: TaskRequest) -> WorkerInfo | None:
        candidates = [
            info
            for worker_id, info in self._workers.items()
            if self._healthy.get(worker_id, False)
            and worker_id not in self._quarantined
            and task.capabilities <= info.capabilities
            and self.available_capacity(worker_id) > 0
            and info.backend_kind in self._isolation.allowed_backends(task.trust_level)
        ]
        if not candidates:
            return None
        # Prefer the least-loaded eligible worker.
        return min(candidates, key=lambda info: self._load[info.worker_id])
