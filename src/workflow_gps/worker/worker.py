"""The worker: the only component that executes task payloads.

A worker verifies a lease, enforces the isolation policy against its own backend,
runs the task under the lease's wall-clock budget, and reports the result. It holds
the execution backend; the control plane does not. An untrusted task can therefore
only run here, and only on a backend the policy permits.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from .errors import InvalidLease, TaskTimeout, WorkerError
from .leases import LeaseVerifier
from .policy import IsolationPolicy


@runtime_checkable
class WorkerExecutor(Protocol):
    """Runs a task payload on a specific backend. The worker owns one of these."""

    backend_kind: str

    def run(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class Worker:
    def __init__(
        self,
        worker_id: str,
        verifier: LeaseVerifier,
        executor: WorkerExecutor,
        *,
        isolation: IsolationPolicy | None = None,
    ):
        self.worker_id = worker_id
        self._verifier = verifier
        self._executor = executor
        self._isolation = isolation or IsolationPolicy()

    def execute(
        self,
        lease_token: str,
        *,
        payload: dict[str, Any],
        now: datetime | None = None,
    ) -> dict[str, Any]:
        # 1. The lease must verify (signature, audience, expiry, revocation, replay).
        lease = self._verifier.verify(lease_token, now=now)
        # 2. The lease must authorize *this* payload's task.
        if payload.get("task_id") not in (None, lease.task_id):
            raise InvalidLease("lease task_id does not match the payload")
        # 3. The chosen backend must meet the task's required isolation.
        self._isolation.enforce(lease.trust_level, self._executor.backend_kind)
        # 4. Run under the lease's wall-clock budget.
        return self._run_with_timeout(payload, lease.timeout_seconds)

    def _run_with_timeout(
        self, payload: dict[str, Any], timeout_seconds: float
    ) -> dict[str, Any]:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self._executor.run, payload)
            try:
                return future.result(timeout=timeout_seconds)
            except FuturesTimeout as exc:
                raise TaskTimeout(f"task exceeded {timeout_seconds}s budget") from exc


class StubWorkerExecutor:
    """A deterministic executor for tests and offline use.

    ``sleep`` lets a test exercise the timeout path; ``fail`` raises; otherwise it
    returns ``result`` (or an echo). A real executor wraps a runtime
    ``ExecutionBackend`` (subprocess/docker) and sets ``backend_kind`` accordingly.
    """

    def __init__(
        self,
        backend_kind: str = "docker",
        *,
        result: dict[str, Any] | None = None,
        sleep: float = 0.0,
        fail: bool = False,
    ):
        self.backend_kind = backend_kind
        self._result = result
        self._sleep = sleep
        self._fail = fail

    def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._sleep:
            import time

            time.sleep(self._sleep)
        if self._fail:
            raise WorkerError("task failed")
        return self._result or {"status": "succeeded", "echo": payload}
