"""Tenant-isolated, versioned SQLite store for identity and RBAC records.

Every query is scoped to a tenant: the read methods take a ``tenant_id`` and filter
on it, and the resolver only ever passes the caller's own ``session.tenant_id``. A
record created in one tenant is therefore invisible to a query for another, which
is the storage-level half of tenant isolation (the policy layer guards request
inputs with :class:`CrossTenantError`).
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from ..persistence import Migration, migrate
from .models import (
    AuthorityGrant,
    Group,
    Identity,
    Membership,
    Organization,
    Role,
    Session,
    Tenant,
)


def _create_identity_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS tenants ("
        "tenant_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS organizations ("
        "org_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, payload_json TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS identities ("
        "principal_id TEXT NOT NULL, tenant_id TEXT NOT NULL, kind TEXT NOT NULL, "
        "payload_json TEXT NOT NULL, PRIMARY KEY (tenant_id, principal_id))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS roles ("
        "tenant_id TEXT NOT NULL, name TEXT NOT NULL, payload_json TEXT NOT NULL, "
        "PRIMARY KEY (tenant_id, name))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS groups ("
        "group_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, payload_json TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS memberships ("
        "tenant_id TEXT NOT NULL, principal_id TEXT NOT NULL, payload_json TEXT NOT NULL, "
        "PRIMARY KEY (tenant_id, principal_id))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS grants ("
        "grant_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, principal_id TEXT NOT NULL, "
        "role_name TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, "
        "payload_json TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sessions ("
        "session_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, principal_id TEXT NOT NULL, "
        "revoked INTEGER NOT NULL DEFAULT 0, payload_json TEXT NOT NULL)"
    )


def _drop_identity_schema(conn: sqlite3.Connection) -> None:
    for table in (
        "sessions",
        "grants",
        "memberships",
        "groups",
        "roles",
        "identities",
        "organizations",
        "tenants",
    ):
        conn.execute(f"DROP TABLE IF EXISTS {table}")


IDENTITY_MIGRATIONS: tuple[Migration, ...] = (
    Migration(up=_create_identity_schema, down=_drop_identity_schema),
)


class IdentityStore:
    def __init__(self, path: str | Path):
        db_path = str(path) if str(path) == ":memory:" else str(Path(path).expanduser())
        if db_path != ":memory:":
            Path(db_path).resolve().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            migrate(self._db, IDENTITY_MIGRATIONS, label="identity")

    # --- seeding / administration ---------------------------------------- #
    def add_tenant(self, tenant: Tenant) -> None:
        self._upsert("tenants", "tenant_id", tenant.tenant_id, tenant.model_dump_json())

    def add_organization(self, org: Organization) -> None:
        self._upsert(
            "organizations", "org_id", org.org_id, org.model_dump_json(), org.tenant_id
        )

    def add_identity(self, identity: Identity) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO identities (principal_id, tenant_id, kind, payload_json)"
                " VALUES (?, ?, ?, ?)",
                (
                    identity.principal_id,
                    identity.tenant_id,
                    identity.kind.value,
                    identity.model_dump_json(),
                ),
            )

    def add_role(self, role: Role) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO roles (tenant_id, name, payload_json)"
                " VALUES (?, ?, ?)",
                (role.tenant_id, role.name, role.model_dump_json()),
            )

    def add_group(self, group: Group) -> None:
        self._upsert(
            "groups",
            "group_id",
            group.group_id,
            group.model_dump_json(),
            group.tenant_id,
        )

    def add_membership(self, membership: Membership) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO memberships (tenant_id, principal_id, payload_json)"
                " VALUES (?, ?, ?)",
                (
                    membership.tenant_id,
                    membership.principal_id,
                    membership.model_dump_json(),
                ),
            )

    def add_grant(self, grant: AuthorityGrant) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO grants"
                " (grant_id, tenant_id, principal_id, role_name, revoked, payload_json)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    grant.grant_id,
                    grant.tenant_id,
                    grant.principal_id,
                    grant.role_name,
                    1 if grant.revoked else 0,
                    grant.model_dump_json(),
                ),
            )

    def revoke_grant(self, grant_id: str) -> bool:
        with self._lock, self._db:
            cursor = self._db.execute(
                "UPDATE grants SET revoked = 1 WHERE grant_id = ?", (grant_id,)
            )
        return cursor.rowcount > 0

    # --- tenant-scoped reads --------------------------------------------- #
    def get_identity(self, tenant_id: str, principal_id: str) -> Identity | None:
        row = self._one(
            "SELECT payload_json FROM identities WHERE tenant_id = ? AND principal_id = ?",
            (tenant_id, principal_id),
        )
        return Identity.model_validate_json(row["payload_json"]) if row else None

    def get_role(self, tenant_id: str, name: str) -> Role | None:
        row = self._one(
            "SELECT payload_json FROM roles WHERE tenant_id = ? AND name = ?",
            (tenant_id, name),
        )
        return Role.model_validate_json(row["payload_json"]) if row else None

    def get_group(self, tenant_id: str, group_id: str) -> Group | None:
        row = self._one(
            "SELECT payload_json FROM groups WHERE tenant_id = ? AND group_id = ?",
            (tenant_id, group_id),
        )
        return Group.model_validate_json(row["payload_json"]) if row else None

    def get_membership(self, tenant_id: str, principal_id: str) -> Membership | None:
        row = self._one(
            "SELECT payload_json FROM memberships WHERE tenant_id = ? AND principal_id = ?",
            (tenant_id, principal_id),
        )
        return Membership.model_validate_json(row["payload_json"]) if row else None

    def list_grants(self, tenant_id: str, principal_id: str) -> list[AuthorityGrant]:
        with self._lock:
            rows = self._db.execute(
                "SELECT payload_json FROM grants WHERE tenant_id = ? AND principal_id = ?",
                (tenant_id, principal_id),
            ).fetchall()
        return [AuthorityGrant.model_validate_json(row["payload_json"]) for row in rows]

    # --- sessions -------------------------------------------------------- #
    def save_session(self, session: Session) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO sessions"
                " (session_id, tenant_id, principal_id, revoked, payload_json)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    session.session_id,
                    session.tenant_id,
                    session.principal_id,
                    1 if session.revoked else 0,
                    session.model_dump_json(),
                ),
            )

    def get_session(self, session_id: str) -> Session | None:
        row = self._one(
            "SELECT payload_json FROM sessions WHERE session_id = ?", (session_id,)
        )
        return Session.model_validate_json(row["payload_json"]) if row else None

    def revoke_session(self, session_id: str) -> bool:
        session = self.get_session(session_id)
        if session is None:
            return False
        self.save_session(session.model_copy(update={"revoked": True}))
        return True

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # --------------------------------------------------------------------- #
    def _upsert(
        self,
        table: str,
        key_column: str,
        key: str,
        payload: str,
        tenant_id: str | None = None,
    ) -> None:
        with self._lock, self._db:
            if tenant_id is None:
                self._db.execute(
                    f"INSERT OR REPLACE INTO {table} ({key_column}, payload_json)"
                    " VALUES (?, ?)",
                    (key, payload),
                )
            else:
                self._db.execute(
                    f"INSERT OR REPLACE INTO {table} ({key_column}, tenant_id, payload_json)"
                    " VALUES (?, ?, ?)",
                    (key, tenant_id, payload),
                )

    def _one(self, sql: str, params: tuple) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(sql, params).fetchone()


def grant_is_active(grant: AuthorityGrant, *, now: datetime) -> bool:
    if grant.revoked:
        return False
    if grant.expires_at is not None and now > grant.expires_at:
        return False
    return True
