"""The JobDesk's production dispatcher: jobs ride signed, single-use leases.

The M3 closer for the worker seam. A typed execution job becomes a
capability-scoped task on the worker control plane: only a worker that
REGISTERED the target node's capability can be assigned it, the assignment
carries an HMAC-signed, expiring, audience-bound lease (the standing
`oolu.worker` discipline — a lost, replayed, expired, or revoked lease
cannot execute), and the lease token itself never touches the audit chain,
because a credential is not evidence.

A dispatch with no capable, healthy worker fails LOUDLY — the desk marks
the job failed and the caller hears it; a job that silently went nowhere
would be a fulfillment promise nobody is keeping.
"""

from __future__ import annotations

from ..worker.control_plane import ControlPlane, TaskRequest
from ..worker.leases import TrustLevel
from .errors import WrongState
from .jobs import ExecutionJob


def node_capability(node_id: str) -> str:
    """The capability a worker must hold to execute this node's jobs."""
    return f"execute:{node_id}"


class WorkerLeaseDispatcher:
    def __init__(
        self,
        control_plane: ControlPlane,
        *,
        audit,
        timeout_seconds: float = 3600.0,
    ) -> None:
        self._control = control_plane
        self._audit = audit
        self._timeout = timeout_seconds

    def __call__(self, job: ExecutionJob) -> None:
        request = TaskRequest(
            task_id=f"job:{job.job_id}",
            run_id=job.order_id,
            tenant_id=job.tenant_id,
            capabilities=frozenset({node_capability(job.node_id)}),
            # A certified execution node runs its OWN adapter — a trusted
            # skill, not synthesized code; the isolation policy still
            # decides the backend it may use.
            trust_level=TrustLevel.TRUSTED_LOCAL_SKILL,
            payload={
                "kind": "market.execution_job",
                "job_id": job.job_id,
                "order_id": job.order_id,
                "node_id": job.node_id,
                "parameters": dict(job.parameters),
                "price_micros": job.price_micros,
            },
            timeout_seconds=self._timeout,
        )
        assignment = self._control.dispatch(request)
        if assignment is None:
            raise WrongState(
                f"no healthy worker holds '{node_capability(job.node_id)}'; "
                "the job cannot be dispatched"
            )
        self._audit.append(
            "market.job.leased",
            {
                "tenant": job.tenant_id,
                "job_id": job.job_id,
                "order_id": job.order_id,
                "worker_id": assignment.worker_id,
                "task_id": assignment.task_id,
                # The lease token is a credential, not evidence: only the
                # fact of the lease rides the chain.
            },
        )
