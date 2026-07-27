"""Digest-bound approval bundles for actuation plans.

The marketplace's approval law, applied to physical work: an approval
signs one plan digest and can never release another; each approval
spends exactly once; expired approvals are dead; and under dual control
two *distinct people* in the two required roles must sign — one person
holding both roles is one signature, not two.

Two protocol-specific rules sit on top:

* **A prohibited tier cannot open a bundle.** Review tier A5 (outside
  the certified cell or validated envelope) and A0 (simulation only)
  have no approval path — ``request`` refuses by name rather than
  creating a bundle nobody may sign. What cannot be approved cannot
  accumulate approvals.
* **Safety approval is not budget approval.** The roles recorded here
  are the review decision's *safety* roles; financial authorization
  stays in the marketplace's own approval flow and never substitutes.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from ..actuation.review import ReviewDecision
from ..durable import DurableConnection

_REQUIREMENTS_SCHEMA = """CREATE TABLE IF NOT EXISTS protocol_plan_approval_reqs (
    plan_digest TEXT PRIMARY KEY,
    tier TEXT NOT NULL,
    required_roles TEXT NOT NULL,
    dual_control INTEGER NOT NULL,
    ttl_seconds INTEGER NOT NULL,
    requested_at INTEGER NOT NULL,
    consumed_at INTEGER
)"""

_APPROVALS_SCHEMA = """CREATE TABLE IF NOT EXISTS protocol_plan_approvals (
    approval_id TEXT PRIMARY KEY,
    plan_digest TEXT NOT NULL,
    role TEXT NOT NULL,
    approver TEXT NOT NULL,
    decided_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
)"""


class ApprovalStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    plan_digest: str
    ready: bool
    missing_roles: tuple[str, ...] = ()
    refusals: tuple[str, ...] = ()


class ActuationApprovalService:
    """Durable bundles: request → approve per role → consume once."""

    def __init__(self, conn: DurableConnection) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_REQUIREMENTS_SCHEMA)
            db.execute(_APPROVALS_SCHEMA)

    def request(
        self,
        plan_digest: str,
        review: ReviewDecision,
        *,
        now: int,
        ttl_seconds: int,
    ) -> None:
        if not review.physical_execution_permitted:
            raise PermissionError(f"physical_execution_prohibited:{review.tier.value}")
        with self._conn.transaction() as db:
            existing = db.execute(
                "SELECT plan_digest FROM protocol_plan_approval_reqs"
                " WHERE plan_digest = ?",
                (plan_digest,),
            ).fetchone()
            if existing is not None:
                raise ValueError(
                    "an approval bundle already exists for this digest; "
                    "a changed plan is a new digest"
                )
            db.execute(
                "INSERT INTO protocol_plan_approval_reqs"
                " VALUES (?, ?, ?, ?, ?, ?, NULL)",
                (
                    plan_digest,
                    review.tier.value,
                    ",".join(review.required_safety_roles),
                    1 if review.dual_control else 0,
                    ttl_seconds,
                    now,
                ),
            )

    def approve(
        self,
        plan_digest: str,
        *,
        role: str,
        approver: str,
        now: int,
    ) -> str:
        """Record one signature against one digest, or refuse by name."""

        with self._conn.transaction() as db:
            requirement = db.execute(
                "SELECT * FROM protocol_plan_approval_reqs WHERE plan_digest = ?",
                (plan_digest,),
            ).fetchone()
            if requirement is None:
                raise KeyError(f"no_approval_bundle_for_digest:{plan_digest}")
            required_roles = tuple(
                item for item in requirement["required_roles"].split(",") if item
            )
            if role not in required_roles:
                raise PermissionError(f"role_not_required:{role}")
            if requirement["consumed_at"] is not None:
                raise PermissionError("bundle_already_consumed")
            approval_id = f"pa-{plan_digest[:12]}-{role}"
            db.execute(
                "INSERT OR REPLACE INTO protocol_plan_approvals"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    approval_id,
                    plan_digest,
                    role,
                    approver,
                    now,
                    now + requirement["ttl_seconds"],
                ),
            )
        return approval_id

    def status(self, plan_digest: str, *, now: int) -> ApprovalStatus:
        with self._conn.transaction() as db:
            requirement = db.execute(
                "SELECT * FROM protocol_plan_approval_reqs WHERE plan_digest = ?",
                (plan_digest,),
            ).fetchone()
            rows = db.execute(
                "SELECT * FROM protocol_plan_approvals WHERE plan_digest = ?",
                (plan_digest,),
            ).fetchall()
        if requirement is None:
            return ApprovalStatus(
                plan_digest=plan_digest,
                ready=False,
                refusals=("no_approval_bundle_for_digest",),
            )
        refusals: list[str] = []
        if requirement["consumed_at"] is not None:
            refusals.append("bundle_already_consumed")
        required_roles = tuple(
            item for item in requirement["required_roles"].split(",") if item
        )
        live = [row for row in rows if row["expires_at"] > now]
        expired = [row for row in rows if row["expires_at"] <= now]
        if expired:
            refusals.append("approval_expired")
        covered = {row["role"] for row in live}
        missing = tuple(role for role in required_roles if role not in covered)
        if requirement["dual_control"]:
            approvers = {row["approver"] for row in live}
            if len(approvers) < 2:
                refusals.append("dual_control_requires_two_distinct_approvers")
        ready = not missing and not refusals
        return ApprovalStatus(
            plan_digest=plan_digest,
            ready=ready,
            missing_roles=missing,
            refusals=tuple(refusals),
        )

    def consume(self, plan_digest: str, *, now: int) -> tuple[str, ...]:
        """Spend the bundle for one release. Empty tuple means spent."""

        current = self.status(plan_digest, now=now)
        if not current.ready:
            refusals = list(current.refusals)
            refusals.extend(
                f"missing_approval_role:{role}" for role in current.missing_roles
            )
            return tuple(refusals)
        with self._conn.transaction() as db:
            cursor = db.execute(
                "UPDATE protocol_plan_approval_reqs SET consumed_at = ?"
                " WHERE plan_digest = ? AND consumed_at IS NULL",
                (now, plan_digest),
            )
            if not cursor.rowcount:
                return ("bundle_already_consumed",)
        return ()
