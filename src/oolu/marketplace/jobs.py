"""Physical execution jobs (M3): typed work, priced exactly, or not at all.

The spec's physical-world workflow: a confirmed service order dispatches a
TYPED job to an execution node; the node acknowledges capability, price,
and schedule; and any changed term re-enters policy — the same digest law
that governs purchases governs jobs. An acknowledgement whose price is not
the order's approved price does not "renegotiate": it invalidates, is
audited, and the answer is a new intent through the ordinary door.

Dispatch is a port. Locally the desk records the job; the production
adapter hands it to the worker control plane's signed, single-use leases —
the standing seam for privileged execution. Evidence uploads ride the same
content-addressed discipline as delivery evidence.
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .errors import DigestMismatch, MarketNotFound, WrongState
from .orders import StoredOrder

_SCHEMA = """CREATE TABLE IF NOT EXISTS market_jobs (
    job_id TEXT PRIMARY KEY,
    tenant TEXT NOT NULL,
    order_id TEXT NOT NULL,
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL
)"""


class ExecutionJob(BaseModel):
    model_config = ConfigDict(frozen=True)

    job_id: str
    tenant_id: str
    order_id: str
    node_id: str
    parameters: tuple[tuple[str, str], ...] = ()
    # The order's approved price — the term an acknowledgement must match.
    price_micros: int = Field(ge=0)
    state: str = "dispatched"
    # dispatched | acknowledged | terms_changed | completed | failed
    scheduled_at: datetime | None = None
    evidence: str = ""
    created_at: datetime


class JobDesk:
    def __init__(self, conn, *, audit, dispatcher=None) -> None:
        self._conn = conn
        self._audit = audit
        # The production seam: a callable handing the typed job to the
        # worker control plane (signed, single-use leases). None = the
        # desk records; a later adapter dispatches.
        self._dispatcher = dispatcher
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def _save(self, job: ExecutionJob) -> ExecutionJob:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO market_jobs
                   (job_id, tenant, order_id, state, payload_json)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(job_id) DO UPDATE SET
                     state = excluded.state,
                     payload_json = excluded.payload_json""",
                (job.job_id, job.tenant_id, job.order_id, job.state, job.model_dump_json()),
            )
        return job

    def get(self, job_id: str, *, tenant: str) -> ExecutionJob:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json FROM market_jobs"
                " WHERE job_id = ? AND tenant = ?",
                (job_id, tenant),
            ).fetchone()
        if row is None:
            raise MarketNotFound("no such job")
        return ExecutionJob.model_validate_json(row["payload_json"])

    def dispatch(
        self,
        order: StoredOrder,
        *,
        node_id: str,
        parameters: dict[str, str] | None = None,
        now: datetime,
    ) -> ExecutionJob:
        """Send the typed job. The price it carries is the ORDER's — the
        approved terms, not an opening bid."""
        if order.state not in ("confirmed", "fulfilling"):
            raise WrongState(f"order is {order.state}, not in fulfillment")
        job = ExecutionJob(
            job_id=uuid4().hex,
            tenant_id=order.record.tenant_id,
            order_id=order.record.order_id,
            node_id=node_id,
            parameters=tuple(sorted((parameters or {}).items())),
            price_micros=order.record.offer.total_micros,
            created_at=now,
        )
        self._save(job)
        if self._dispatcher is not None:
            self._dispatcher(job)
        self._audit.append(
            "market.job.dispatched",
            {
                "tenant": job.tenant_id,
                "job_id": job.job_id,
                "order_id": job.order_id,
                "node_id": node_id,
                "price_micros": job.price_micros,
            },
        )
        return job

    def acknowledge(
        self,
        job_id: str,
        *,
        tenant: str,
        price_micros: int,
        scheduled_at: datetime,
        now: datetime,
    ) -> ExecutionJob:
        """The node answers with capability, price, and schedule. The same
        price proceeds; a different price is a changed term — the prior
        approval is invalid for it, deterministically."""
        job = self.get(job_id, tenant=tenant)
        if job.state != "dispatched":
            raise WrongState(f"the job is {job.state}")
        if price_micros != job.price_micros:
            self._save(job.model_copy(update={"state": "terms_changed"}))
            self._audit.append(
                "market.job.terms_changed",
                {
                    "tenant": tenant,
                    "job_id": job_id,
                    "order_id": job.order_id,
                    "approved_micros": job.price_micros,
                    "acknowledged_micros": price_micros,
                },
            )
            raise DigestMismatch(
                "the node's terms differ from the approved order — a new "
                "intent and approval cycle is required"
            )
        updated = job.model_copy(
            update={"state": "acknowledged", "scheduled_at": scheduled_at}
        )
        self._save(updated)
        self._audit.append(
            "market.job.acknowledged",
            {
                "tenant": tenant,
                "job_id": job_id,
                "scheduled_at": scheduled_at.isoformat(),
            },
        )
        return updated

    def complete(
        self, job_id: str, *, tenant: str, evidence: str, now: datetime
    ) -> ExecutionJob:
        """Execution evidence comes home; the order's delivery path takes
        it from here (the buyer confirms or opens an exception)."""
        job = self.get(job_id, tenant=tenant)
        if job.state != "acknowledged":
            raise WrongState(f"the job is {job.state}, not acknowledged")
        if not evidence:
            raise WrongState("completion needs execution evidence")
        updated = job.model_copy(
            update={"state": "completed", "evidence": evidence}
        )
        self._save(updated)
        self._audit.append(
            "market.job.completed",
            {
                "tenant": tenant,
                "job_id": job_id,
                "order_id": job.order_id,
                "evidence": evidence,
            },
        )
        return updated
