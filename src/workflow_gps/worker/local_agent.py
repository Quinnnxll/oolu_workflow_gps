"""Outbound-only local agent for desktop and private-network resources.

A local agent runs on a user's machine or inside a private network where the
control plane cannot reach it. It only ever *initiates* connections to the control
plane — polling for assignments and posting results — so no inbound port is exposed.
Crucially, it holds local/desktop credentials itself and resolves them locally; the
control plane never receives them. This is what keeps the control plane free of
desktop credentials.
"""

from __future__ import annotations

from datetime import datetime

from .control_plane import ControlPlane
from .errors import WorkerError
from .worker import Worker


class LocalSecretProvider:
    """Holds local credentials. Lives only on the agent; never sent upstream."""

    def __init__(self, secrets: dict[str, str]):
        self._secrets = dict(secrets)

    def resolve(self, ref: str) -> str:
        if ref not in self._secrets:
            raise KeyError(f"unknown local credential: {ref}")
        return self._secrets[ref]


class LocalAgent:
    def __init__(
        self,
        worker: Worker,
        control_plane: ControlPlane,
        *,
        secrets: LocalSecretProvider | None = None,
    ):
        self._worker = worker
        self._control_plane = control_plane
        self._secrets = secrets or LocalSecretProvider({})

    @property
    def worker_id(self) -> str:
        return self._worker.worker_id

    def resolve_secret(self, ref: str) -> str:
        """Resolve a credential locally (the control plane is never asked)."""
        return self._secrets.resolve(ref)

    def run_once(self, *, now: datetime | None = None) -> dict | None:
        """Pull one assignment, execute it locally, and report the result."""
        assignment = self._control_plane.poll(self.worker_id)
        if assignment is None:
            return None
        payload = dict(assignment.payload)
        # Any credential the task needs is resolved here, locally — never upstream.
        if (ref := payload.get("credential_ref")) is not None:
            payload["_resolved_secret"] = self._secrets.resolve(ref)
        try:
            result = self._worker.execute(
                assignment.lease_token, payload=payload, now=now
            )
        except WorkerError as exc:
            self._control_plane.report_result(
                self.worker_id, assignment.task_id, success=False
            )
            return {"status": "failed", "error": str(exc)}
        self._control_plane.report_result(
            self.worker_id,
            assignment.task_id,
            success=result.get("status") == "succeeded",
        )
        return result
