from __future__ import annotations

from typing import Any

from ..runtime.backend import (
    BackendError,
    ExecutionBackend,
    ExecutionRequest,
    ResourceLimits,
)
from .errors import WorkerError


class BackendWorkerExecutor:
    """A ``WorkerExecutor`` that runs a task's script through a runtime
    ``ExecutionBackend`` (Docker in production) — the real, sandboxed executor the
    worker owns. ``backend_kind`` is what the isolation policy checks, so it must
    be the policy name (``docker``), not the backend's telemetry name.
    """

    def __init__(self, backend: ExecutionBackend, *, backend_kind: str = "docker"):
        self._backend = backend
        self.backend_kind = backend_kind

    def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        spec = (
            payload.get("action", {}).get("parameters", {})
            if "action" in payload
            else payload
        )
        script = spec.get("script")
        if not isinstance(script, str) or not script.strip():
            return {"status": "failed", "error": "no script to run"}

        limits = spec.get("limits")
        request = ExecutionRequest(
            script=script,
            dependencies=list(spec.get("dependencies", [])),
            pinned_index_url=spec.get("pinned_index_url"),
            limits=ResourceLimits(**limits)
            if isinstance(limits, dict)
            else ResourceLimits(),
            session_id=str(payload.get("task_id", "worker")),
            env=dict(spec.get("env", {})),
        )
        try:
            result = self._backend.run(request)
        except BackendError as exc:
            # Isolation-machinery failure is not a script failure — surface it.
            raise WorkerError(f"isolation backend failed: {exc}") from exc

        error = None
        if not result.succeeded:
            error = (
                result.error.message
                if result.error
                else (result.stderr[:500] or f"exit {result.exit_code}")
            )
        return {
            "status": "succeeded" if result.succeeded else "failed",
            "evidence": {
                "phase": result.phase.value,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "duration_s": result.duration_s,
                "contract_ok": result.contract_ok,
                "payload": result.contract_payload,
            },
            "error": error,
        }

    def health_check(self) -> bool:
        return self._backend.health_check()

    def close(self) -> None:
        self._backend.close()
