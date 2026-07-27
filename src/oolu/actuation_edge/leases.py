"""Durable execution leases — a spent lease stays spent across restarts.

R0's ``LeaseRegistry`` defined the semantics (single use, nonce-bound,
expiry prevents a start and never forces a stop); this service persists
them on the durable connection with the same ``issue``/``consume``
surface, so R0's ``arm`` gate accepts either interchangeably. The
consumption record is the anti-replay memory the plan requires — losing
the process must not resurrect a lease.
"""

from __future__ import annotations

from ..actuation.lease import ExecutionLease, LeaseRefusal
from ..durable import DurableConnection

_SCHEMA = """CREATE TABLE IF NOT EXISTS protocol_execution_leases (
    lease_id TEXT PRIMARY KEY,
    job_digest TEXT NOT NULL,
    nonce TEXT NOT NULL,
    issued_at INTEGER NOT NULL,
    ttl_seconds INTEGER NOT NULL,
    consumed_at INTEGER
)"""


class DurableLeaseService:
    def __init__(self, conn: DurableConnection) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def issue(
        self, *, job_digest: str, nonce: str, now: int, ttl_seconds: int
    ) -> ExecutionLease:
        with self._conn.transaction() as db:
            count = db.execute(
                "SELECT COUNT(*) AS n FROM protocol_execution_leases"
            ).fetchone()["n"]
            lease = ExecutionLease(
                lease_id=f"lease-{count + 1:08d}",
                job_digest=job_digest,
                nonce=nonce,
                issued_at=now,
                ttl_seconds=ttl_seconds,
            )
            db.execute(
                "INSERT INTO protocol_execution_leases VALUES (?, ?, ?, ?, ?, NULL)",
                (lease.lease_id, job_digest, nonce, now, ttl_seconds),
            )
        return lease

    def consume(
        self, *, lease_id: str, job_digest: str, nonce: str, now: int
    ) -> LeaseRefusal | None:
        """Spend the lease once. Same refusal order as R0's registry."""

        with self._conn.transaction() as db:
            row = db.execute(
                "SELECT * FROM protocol_execution_leases WHERE lease_id = ?",
                (lease_id,),
            ).fetchone()
            if row is None:
                return LeaseRefusal.UNKNOWN_LEASE
            if row["consumed_at"] is not None:
                return LeaseRefusal.ALREADY_CONSUMED
            if row["job_digest"] != job_digest:
                return LeaseRefusal.JOB_DIGEST_MISMATCH
            if row["nonce"] != nonce:
                return LeaseRefusal.NONCE_MISMATCH
            if now < row["issued_at"]:
                return LeaseRefusal.NOT_YET_VALID
            if now >= row["issued_at"] + row["ttl_seconds"]:
                return LeaseRefusal.EXPIRED
            db.execute(
                "UPDATE protocol_execution_leases SET consumed_at = ?"
                " WHERE lease_id = ? AND consumed_at IS NULL",
                (now, lease_id),
            )
        return None
