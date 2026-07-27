"""Single-use execution leases — the arming key, never a stop command.

A lease is the short-lived, nonce-bound authorization to *start* one
approved job, once. The semantics the plan fixes and this module
enforces:

* single use: a consumed lease can never arm again, even inside TTL;
* expiry prevents a new start — it does not and cannot force a stop;
  the validated local controller owns safe completion, hold, or stop;
* anti-replay: consumption is recorded by lease id and nonce, and a
  replay is refused with a named reason, not a silent no-op;
* digest binding: a lease is issued against one aggregate job digest
  and refuses any other.

Time is injected (``now`` parameters, seconds) so lease behavior is
deterministic under test and never depends on a wall clock the edge
does not trust.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict


class LeaseRefusal(str, Enum):
    """Named reasons a lease cannot arm a job."""

    UNKNOWN_LEASE = "unknown_lease"
    EXPIRED = "expired"
    ALREADY_CONSUMED = "already_consumed"
    JOB_DIGEST_MISMATCH = "job_digest_mismatch"
    NONCE_MISMATCH = "nonce_mismatch"
    NOT_YET_VALID = "not_yet_valid"


class ExecutionLease(BaseModel):
    model_config = ConfigDict(frozen=True)

    lease_id: str
    job_digest: str
    nonce: str
    issued_at: int
    ttl_seconds: int

    def expires_at(self) -> int:
        return self.issued_at + self.ttl_seconds


class LeaseRegistry:
    """The authority that issues and consumes leases, with replay memory.

    In R0 this is in-process state; the R4 lease service persists the
    same records durably. Consumption memory is append-only: revoking a
    device or losing connectivity never forgets that a lease was spent.
    """

    def __init__(self) -> None:
        self._leases: dict[str, ExecutionLease] = {}
        self._consumed: set[str] = set()
        self._counter = 0

    def issue(
        self, *, job_digest: str, nonce: str, now: int, ttl_seconds: int
    ) -> ExecutionLease:
        self._counter += 1
        lease = ExecutionLease(
            lease_id=f"lease-{self._counter:08d}",
            job_digest=job_digest,
            nonce=nonce,
            issued_at=now,
            ttl_seconds=ttl_seconds,
        )
        self._leases[lease.lease_id] = lease
        return lease

    def consume(
        self, *, lease_id: str, job_digest: str, nonce: str, now: int
    ) -> LeaseRefusal | None:
        """Spend the lease for one arming. ``None`` means armed.

        Order matters: replay is checked before expiry so a spent lease
        is always reported as spent, and every refusal leaves the lease
        exactly as it was — refusals are free to retry with the right
        material only while the lease is live and unspent.
        """

        lease = self._leases.get(lease_id)
        if lease is None:
            return LeaseRefusal.UNKNOWN_LEASE
        if lease_id in self._consumed:
            return LeaseRefusal.ALREADY_CONSUMED
        if lease.job_digest != job_digest:
            return LeaseRefusal.JOB_DIGEST_MISMATCH
        if lease.nonce != nonce:
            return LeaseRefusal.NONCE_MISMATCH
        if now < lease.issued_at:
            return LeaseRefusal.NOT_YET_VALID
        if now >= lease.expires_at():
            return LeaseRefusal.EXPIRED
        self._consumed.add(lease_id)
        return None
