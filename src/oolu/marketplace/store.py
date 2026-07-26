"""Durable stores for the commercial spine: intents, approvals, policies.

All three ride the shared durable connection (the same physical database as
the audit chain, so a state change and its audit entry can commit close
together), with the repository's standing store discipline: payload-JSON
rows behind typed records, tenant scoping on every query, INSERT OR IGNORE
for idempotent creation, and ordering by the record's own clock — never by
SQLite's implicit row order.

The intent table's UNIQUE (tenant, idempotency_key) is the spec's
duplicate-execution acceptance test at the intent level: the same key
returns the existing intent, never a second one. Approval consumption is a
single guarded UPDATE — the row is spent exactly once even under concurrent
executors, the pulse claim's own pattern.
"""

from __future__ import annotations

from datetime import datetime

from .models import ApprovalRecord, CommercialIntent
from .policy import PolicyVerdict, PurchasePolicy, SalesPolicy

_INTENTS_SCHEMA = """CREATE TABLE IF NOT EXISTS commercial_intents (
    intent_id TEXT PRIMARY KEY,
    tenant TEXT NOT NULL,
    principal TEXT NOT NULL,
    agent TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    state TEXT NOT NULL,
    digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    verdict_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (tenant, idempotency_key)
)"""

_APPROVALS_SCHEMA = """CREATE TABLE IF NOT EXISTS market_approvals (
    approval_id TEXT PRIMARY KEY,
    tenant TEXT NOT NULL,
    intent_id TEXT NOT NULL,
    digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    consumed_at TEXT
)"""

_POLICIES_SCHEMA = """CREATE TABLE IF NOT EXISTS market_policies (
    tenant TEXT NOT NULL,
    principal TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (tenant, principal)
)"""

_SALES_POLICIES_SCHEMA = """CREATE TABLE IF NOT EXISTS market_sales_policies (
    tenant TEXT NOT NULL,
    principal TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (tenant, principal)
)"""

# The states an intent can wait in. Anything not terminal is blockable by a
# delegation revocation.
ACTIVE_STATES = ("auto_approved", "notify", "approval_pending", "approved")
TERMINAL_STATES = ("authorized", "denied", "rejected", "blocked", "expired")


class StoredIntent:
    """One intent row: the frozen record plus its mutable spine state."""

    def __init__(
        self,
        intent: CommercialIntent,
        *,
        state: str,
        digest: str,
        verdict: PolicyVerdict,
    ) -> None:
        self.intent = intent
        self.state = state
        self.digest = digest
        self.verdict = verdict


class IntentStore:
    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_INTENTS_SCHEMA)

    def add(
        self,
        intent: CommercialIntent,
        *,
        state: str,
        digest: str,
        verdict: PolicyVerdict,
    ) -> tuple[StoredIntent, bool]:
        """Insert, or return the existing row for this idempotency key."""
        existing = self.by_idempotency_key(
            intent.idempotency_key, tenant=intent.tenant_id
        )
        if existing is not None:
            return existing, False
        with self._conn.transaction() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO commercial_intents"
                " (intent_id, tenant, principal, agent, idempotency_key, state,"
                "  digest, payload_json, verdict_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    intent.intent_id,
                    intent.tenant_id,
                    intent.principal_id,
                    intent.agent_id,
                    intent.idempotency_key,
                    state,
                    digest,
                    intent.model_dump_json(),
                    verdict.model_dump_json(),
                    intent.created_at.isoformat(),
                ),
            )
            created = cursor.rowcount > 0
        if not created:
            # A concurrent writer won the unique race; theirs is the record.
            settled = self.by_idempotency_key(
                intent.idempotency_key, tenant=intent.tenant_id
            )
            assert settled is not None
            return settled, False
        return StoredIntent(intent, state=state, digest=digest, verdict=verdict), True

    def get(self, intent_id: str, *, tenant: str) -> StoredIntent | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json, verdict_json, state, digest"
                " FROM commercial_intents WHERE intent_id = ? AND tenant = ?",
                (intent_id, tenant),
            ).fetchone()
        return None if row is None else self._row(row)

    def by_idempotency_key(self, key: str, *, tenant: str) -> StoredIntent | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json, verdict_json, state, digest"
                " FROM commercial_intents"
                " WHERE tenant = ? AND idempotency_key = ?",
                (tenant, key),
            ).fetchone()
        return None if row is None else self._row(row)

    def set_state(
        self,
        intent_id: str,
        *,
        tenant: str,
        state: str,
        expected: tuple[str, ...] | None = None,
    ) -> bool:
        """Guarded transition: moves only from an ``expected`` state."""
        with self._conn.transaction() as db:
            if expected is None:
                cursor = db.execute(
                    "UPDATE commercial_intents SET state = ?"
                    " WHERE intent_id = ? AND tenant = ?",
                    (state, intent_id, tenant),
                )
            else:
                marks = ",".join("?" for _ in expected)
                cursor = db.execute(
                    "UPDATE commercial_intents SET state = ?"
                    f" WHERE intent_id = ? AND tenant = ? AND state IN ({marks})",
                    (state, intent_id, tenant, *expected),
                )
            return cursor.rowcount > 0

    def list_for(
        self,
        *,
        tenant: str,
        principal: str | None = None,
        state: str | None = None,
    ) -> list[StoredIntent]:
        query = (
            "SELECT payload_json, verdict_json, state, digest"
            " FROM commercial_intents WHERE tenant = ?"
        )
        args: list = [tenant]
        if principal is not None:
            query += " AND principal = ?"
            args.append(principal)
        if state is not None:
            query += " AND state = ?"
            args.append(state)
        with self._conn.lock:
            rows = self._conn.db.execute(query, args).fetchall()
        records = [self._row(row) for row in rows]
        records.sort(
            key=lambda r: (r.intent.created_at.isoformat(), r.intent.intent_id)
        )
        return records

    def block_for_agent(
        self, *, tenant: str, principal: str, agent: str
    ) -> list[str]:
        """Every ACTIVE intent this agent minted for this principal →
        ``blocked``, atomically. Returns the blocked intent ids."""
        marks = ",".join("?" for _ in ACTIVE_STATES)
        with self._conn.transaction() as db:
            rows = db.execute(
                "SELECT intent_id FROM commercial_intents"
                " WHERE tenant = ? AND principal = ? AND agent = ?"
                f" AND state IN ({marks})",
                (tenant, principal, agent, *ACTIVE_STATES),
            ).fetchall()
            blocked = [row["intent_id"] for row in rows]
            if blocked:
                id_marks = ",".join("?" for _ in blocked)
                db.execute(
                    "UPDATE commercial_intents SET state = 'blocked'"
                    f" WHERE tenant = ? AND intent_id IN ({id_marks})",
                    (tenant, *blocked),
                )
        return blocked

    @staticmethod
    def _row(row) -> StoredIntent:
        return StoredIntent(
            CommercialIntent.model_validate_json(row["payload_json"]),
            state=row["state"],
            digest=row["digest"],
            verdict=PolicyVerdict.model_validate_json(row["verdict_json"]),
        )


class ApprovalStore:
    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_APPROVALS_SCHEMA)

    def add(self, record: ApprovalRecord) -> bool:
        with self._conn.transaction() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO market_approvals"
                " (approval_id, tenant, intent_id, digest, payload_json,"
                "  consumed_at) VALUES (?, ?, ?, ?, ?, NULL)",
                (
                    record.approval_id,
                    record.tenant_id,
                    record.intent_id,
                    record.intent_digest,
                    record.model_dump_json(),
                ),
            )
            return cursor.rowcount > 0

    def for_intent(
        self, intent_id: str, *, tenant: str
    ) -> list[tuple[ApprovalRecord, bool]]:
        """Every approval for the intent, with its consumed flag."""
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT payload_json, consumed_at FROM market_approvals"
                " WHERE intent_id = ? AND tenant = ?",
                (intent_id, tenant),
            ).fetchall()
        records = [
            (
                ApprovalRecord.model_validate_json(row["payload_json"]),
                row["consumed_at"] is not None,
            )
            for row in rows
        ]
        records.sort(key=lambda pair: (pair[0].created_at.isoformat(), pair[0].approval_id))
        return records

    def consume(self, approval_id: str, *, tenant: str, now: datetime) -> bool:
        """Spend the approval exactly once — the guarded-UPDATE claim."""
        with self._conn.transaction() as db:
            cursor = db.execute(
                "UPDATE market_approvals SET consumed_at = ?"
                " WHERE approval_id = ? AND tenant = ? AND consumed_at IS NULL",
                (now.isoformat(), approval_id, tenant),
            )
            return cursor.rowcount > 0


class PolicyStore:
    """Each principal's purchase policy — typed user values, versioned.

    Absent row = the conservative default policy; storing is explicit."""

    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_POLICIES_SCHEMA)

    def get(self, *, tenant: str, principal: str) -> PurchasePolicy:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json FROM market_policies"
                " WHERE tenant = ? AND principal = ?",
                (tenant, principal),
            ).fetchone()
        if row is None:
            return PurchasePolicy()
        return PurchasePolicy.model_validate_json(row["payload_json"])

    def put(self, policy: PurchasePolicy, *, tenant: str, principal: str) -> None:
        with self._conn.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO market_policies"
                " (tenant, principal, payload_json) VALUES (?, ?, ?)",
                (tenant, principal, policy.model_dump_json()),
            )


class SalesPolicyStore:
    """Each seller's signed automation boundary — floors, auto-accept
    price, discount and quantity limits. Absent row = conservative
    defaults; the ABSOLUTE floor in it is the line no model crosses."""

    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_SALES_POLICIES_SCHEMA)

    def get(self, *, tenant: str, principal: str) -> SalesPolicy:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json FROM market_sales_policies"
                " WHERE tenant = ? AND principal = ?",
                (tenant, principal),
            ).fetchone()
        if row is None:
            return SalesPolicy()
        return SalesPolicy.model_validate_json(row["payload_json"])

    def put(self, policy: SalesPolicy, *, tenant: str, principal: str) -> None:
        with self._conn.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO market_sales_policies"
                " (tenant, principal, payload_json) VALUES (?, ?, ?)",
                (tenant, principal, policy.model_dump_json()),
            )
