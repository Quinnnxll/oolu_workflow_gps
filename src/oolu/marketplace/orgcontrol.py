"""Organization controls (M3): payout destinations move slowly, under four eyes.

A payout-destination change is the fraudster's favorite lever — capture a
seller account, point the money elsewhere, wait for settlement. The spec
answers with three locks this module enforces together: the change always
takes the MULTI-APPROVER path (two distinct humans, strong authentication),
the requester can never count as an approver (self-approval refused — the
standing identity rule, restated in money), and even a fully approved
change applies only after a DELAY window during which the real owner can
see it coming on the audit chain and kill it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from .errors import (
    DuplicateApprover,
    MarketNotFound,
    MarketplaceError,
    StrongAuthenticationRequired,
    WrongState,
)

_SCHEMA = """CREATE TABLE IF NOT EXISTS market_payout_changes (
    request_id TEXT PRIMARY KEY,
    tenant TEXT NOT NULL,
    principal TEXT NOT NULL,
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL
)"""

DEFAULT_DELAY_HOURS = 48
DEFAULT_REQUIRED_APPROVERS = 2


class SelfApproval(MarketplaceError):
    """The requester cannot be one of their own four eyes."""


class DelayNotElapsed(MarketplaceError):
    """Approved, but the protection window is still open."""


class PayoutChangeRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    request_id: str
    tenant_id: str
    principal_id: str
    current_destination: str
    new_destination: str
    approvals: tuple[str, ...] = ()
    state: str = "pending"  # pending | approved | applied | rejected
    created_at: datetime
    apply_after: datetime


class PayoutChangeDesk:
    def __init__(
        self,
        conn,
        *,
        audit,
        delay_hours: int = DEFAULT_DELAY_HOURS,
        required_approvers: int = DEFAULT_REQUIRED_APPROVERS,
    ) -> None:
        self._conn = conn
        self._audit = audit
        self._delay = timedelta(hours=delay_hours)
        self._required = max(2, required_approvers)  # never fewer than four eyes
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def _save(self, request: PayoutChangeRequest) -> PayoutChangeRequest:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO market_payout_changes
                   (request_id, tenant, principal, state, payload_json)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(request_id) DO UPDATE SET
                     state = excluded.state,
                     payload_json = excluded.payload_json""",
                (
                    request.request_id,
                    request.tenant_id,
                    request.principal_id,
                    request.state,
                    request.model_dump_json(),
                ),
            )
        return request

    def get(self, request_id: str, *, tenant: str) -> PayoutChangeRequest:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json FROM market_payout_changes"
                " WHERE request_id = ? AND tenant = ?",
                (request_id, tenant),
            ).fetchone()
        if row is None:
            raise MarketNotFound("no such payout change request")
        return PayoutChangeRequest.model_validate_json(row["payload_json"])

    def list_for(self, *, tenant: str) -> list[PayoutChangeRequest]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT payload_json FROM market_payout_changes"
                " WHERE tenant = ?",
                (tenant,),
            ).fetchall()
        records = [
            PayoutChangeRequest.model_validate_json(row["payload_json"])
            for row in rows
        ]
        records.sort(key=lambda r: (r.created_at.isoformat(), r.request_id))
        return records

    def request(
        self,
        *,
        tenant: str,
        principal: str,
        current_destination: str,
        new_destination: str,
        now: datetime,
    ) -> PayoutChangeRequest:
        record = PayoutChangeRequest(
            request_id=uuid4().hex,
            tenant_id=tenant,
            principal_id=principal,
            current_destination=current_destination,
            new_destination=new_destination,
            created_at=now,
            apply_after=now + self._delay,
        )
        self._save(record)
        self._audit.append(
            "market.payout_change.requested",
            {
                "tenant": tenant,
                "request_id": record.request_id,
                "principal": principal,
                "new_destination": new_destination,
                "apply_after": record.apply_after.isoformat(),
            },
        )
        return record

    def approve(
        self,
        request_id: str,
        *,
        tenant: str,
        approver: str,
        assurance_level: int,
        now: datetime,
    ) -> PayoutChangeRequest:
        request = self.get(request_id, tenant=tenant)
        if request.state != "pending":
            raise WrongState(f"the request is {request.state}")
        if assurance_level < 2:
            raise StrongAuthenticationRequired(
                "a payout change needs step-up authentication to approve"
            )
        if approver == request.principal_id:
            raise SelfApproval(
                "the requester cannot approve their own payout change"
            )
        if approver in request.approvals:
            raise DuplicateApprover("this approver has already decided")
        approvals = (*request.approvals, approver)
        state = "approved" if len(approvals) >= self._required else "pending"
        updated = request.model_copy(
            update={"approvals": approvals, "state": state}
        )
        self._save(updated)
        self._audit.append(
            "market.payout_change.approved",
            {
                "tenant": tenant,
                "request_id": request_id,
                "approver": approver,
                "approvals_recorded": len(approvals),
                "approvals_required": self._required,
            },
        )
        return updated

    def reject(
        self, request_id: str, *, tenant: str, actor: str, now: datetime
    ) -> PayoutChangeRequest:
        """Anyone with a seat at the desk — including the real owner who
        sees a hostile change coming — can kill a pending request."""
        request = self.get(request_id, tenant=tenant)
        if request.state not in ("pending", "approved"):
            raise WrongState(f"the request is {request.state}")
        updated = request.model_copy(update={"state": "rejected"})
        self._save(updated)
        self._audit.append(
            "market.payout_change.rejected",
            {"tenant": tenant, "request_id": request_id, "by": actor},
        )
        return updated

    def apply(
        self, request_id: str, *, tenant: str, now: datetime
    ) -> PayoutChangeRequest:
        """The change takes effect — approved, and past the window."""
        request = self.get(request_id, tenant=tenant)
        if request.state != "approved":
            raise WrongState(
                f"the request is {request.state}; "
                f"{self._required} distinct approvers are required"
            )
        if now < request.apply_after:
            raise DelayNotElapsed(
                "the protection window is open until "
                f"{request.apply_after.isoformat()}"
            )
        updated = request.model_copy(update={"state": "applied"})
        self._save(updated)
        self._audit.append(
            "market.payout_change.applied",
            {
                "tenant": tenant,
                "request_id": request_id,
                "new_destination": request.new_destination,
            },
        )
        return updated
