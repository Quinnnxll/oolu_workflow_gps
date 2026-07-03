"""Worker control-plane error hierarchy.

Lease failures are separated by cause so the control plane and workers (and the
policy tests) can assert on the precise reason a lease was refused — the basis for
the exit-gate guarantee that lost, duplicated, expired, or revoked leases cannot
execute.
"""

from __future__ import annotations


class WorkerError(RuntimeError):
    """Base class for worker control-plane failures."""


class LeaseError(WorkerError):
    """Base class for an unusable worker task lease."""


class InvalidLease(LeaseError):
    """The lease is malformed or its signature does not verify (incl. 'lost')."""


class ExpiredLease(LeaseError):
    """The lease is past its expiry."""


class AudienceMismatch(LeaseError):
    """The lease was issued for a different worker."""


class RevokedLease(LeaseError):
    """The lease was revoked (e.g. by a cancellation)."""


class ReplayedLease(LeaseError):
    """The lease was already consumed; single-use leases cannot run twice."""


class RevocationUnavailable(LeaseError):
    """The revocation authority could not be reached; the lease is refused (fail-closed)."""


class IsolationViolation(WorkerError):
    """The chosen backend does not meet the task's required isolation."""


class TaskTimeout(WorkerError):
    """The task exceeded the lease's wall-clock budget."""


class CapacityExceeded(WorkerError):
    """No healthy worker has capacity for the task right now."""


class WorkerQuarantined(WorkerError):
    """The worker is quarantined and must not receive work."""
