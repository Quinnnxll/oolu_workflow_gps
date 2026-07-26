"""Agent delegation: a principal's typed, expiring, revocable grant of
commercial authority to an agent.

An OoLu agent is never a free actor — it is a delegate, and the delegation
record is the whole of its authority: which actions, which categories, how
much per intent, which counterparties and destinations, and until when.
Anything the record does not grant, the agent does not have; the gaps this
module computes flow into the policy ladder as deny reasons.

Revocation is the sharpest law in the spec's acceptance tests: when a
principal revokes a delegation, every unexecuted intent it minted is blocked
IMMEDIATELY. The service enforces that twice — a sweep at revocation time,
and a live re-check at execution time, so even an intent approved seconds
before the revocation cannot slip through after it.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

_SCHEMA = """CREATE TABLE IF NOT EXISTS market_delegations (
    delegation_id TEXT PRIMARY KEY,
    tenant TEXT NOT NULL,
    principal TEXT NOT NULL,
    agent TEXT NOT NULL,
    payload_json TEXT NOT NULL
)"""


class AgentDelegation(BaseModel):
    """The spec's ``agent_delegation`` fields, frozen. Empty allow-lists on
    counterparties/destinations mean unrestricted; ``allowed_actions`` and
    ``category_scope`` are always explicit ("*" grants all categories) —
    an agent with no stated actions can do nothing."""

    model_config = ConfigDict(frozen=True)

    delegation_id: str
    tenant_id: str
    principal_id: str
    agent_id: str
    allowed_actions: tuple[str, ...]
    category_scope: tuple[str, ...] = ("*",)
    maximum_single_micros: int = Field(ge=0)
    daily_limit_micros: int = Field(ge=0)
    monthly_limit_micros: int = Field(ge=0)
    allowed_counterparties: tuple[str, ...] = ()
    allowed_destinations: tuple[str, ...] = ()
    valid_from: datetime
    expires_at: datetime
    revoked_at: datetime | None = None


def delegation_gaps(
    delegation: AgentDelegation | None,
    *,
    action: str,
    category: str,
    amount_micros: int,
    counterparty: str,
    destination: str,
    now: datetime,
) -> tuple[str, ...]:
    """Every way this ask exceeds the delegation. Empty = fully covered."""
    if delegation is None:
        return ("the agent holds no delegation from this principal",)
    gaps: list[str] = []
    if delegation.revoked_at is not None:
        gaps.append("the agent's delegation is revoked")
    if now < delegation.valid_from:
        gaps.append("the agent's delegation is not yet valid")
    if now >= delegation.expires_at:
        gaps.append("the agent's delegation has expired")
    if action not in delegation.allowed_actions:
        gaps.append(f"the delegation does not allow the action '{action}'")
    if "*" not in delegation.category_scope and category not in delegation.category_scope:
        gaps.append(f"the delegation does not cover the category '{category}'")
    if amount_micros > delegation.maximum_single_micros:
        gaps.append("the amount exceeds the delegation's single-intent maximum")
    if (
        delegation.allowed_counterparties
        and counterparty not in delegation.allowed_counterparties
    ):
        gaps.append(
            f"the delegation does not allow the counterparty '{counterparty}'"
        )
    if (
        delegation.allowed_destinations
        and destination not in delegation.allowed_destinations
    ):
        gaps.append(
            f"the delegation does not allow the destination '{destination}'"
        )
    return tuple(gaps)


class DelegationStore:
    """Durable delegations over the shared durable connection."""

    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def grant(self, record: AgentDelegation) -> bool:
        with self._conn.transaction() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO market_delegations"
                " (delegation_id, tenant, principal, agent, payload_json)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    record.delegation_id,
                    record.tenant_id,
                    record.principal_id,
                    record.agent_id,
                    record.model_dump_json(),
                ),
            )
            return cursor.rowcount > 0

    def get(self, delegation_id: str, *, tenant: str) -> AgentDelegation | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json FROM market_delegations"
                " WHERE delegation_id = ? AND tenant = ?",
                (delegation_id, tenant),
            ).fetchone()
        if row is None:
            return None
        return AgentDelegation.model_validate_json(row["payload_json"])

    def revoke(self, delegation_id: str, *, tenant: str, now: datetime) -> bool:
        """Stamp ``revoked_at``. Idempotent: an already-revoked record stays
        revoked at its original instant."""
        record = self.get(delegation_id, tenant=tenant)
        if record is None or record.revoked_at is not None:
            return False
        revoked = record.model_copy(update={"revoked_at": now})
        with self._conn.transaction() as db:
            db.execute(
                "UPDATE market_delegations SET payload_json = ?"
                " WHERE delegation_id = ? AND tenant = ?",
                (revoked.model_dump_json(), delegation_id, tenant),
            )
        return True

    def active_for(
        self, *, tenant: str, principal: str, agent: str, now: datetime
    ) -> AgentDelegation | None:
        """The newest live delegation this principal gave this agent."""
        candidates = [
            record
            for record in self.list_for(tenant=tenant, principal=principal)
            if record.agent_id == agent
            and record.revoked_at is None
            and record.valid_from <= now < record.expires_at
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda r: r.valid_from.isoformat())

    def list_for(self, *, tenant: str, principal: str) -> list[AgentDelegation]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT payload_json FROM market_delegations"
                " WHERE tenant = ? AND principal = ?",
                (tenant, principal),
            ).fetchall()
        records = [
            AgentDelegation.model_validate_json(row["payload_json"]) for row in rows
        ]
        records.sort(key=lambda r: (r.valid_from.isoformat(), r.delegation_id))
        return records
