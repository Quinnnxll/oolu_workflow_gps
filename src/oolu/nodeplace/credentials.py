"""Node credentials — WHICH key opens WHICH host, beside the host grants (V2).

A row binds one node's granted host to one sealed secret in the durable
vault: ``(tenant, node_id, host, header) -> ref``. The value itself lives
only in the vault (encrypted at rest under the machine key); this table
holds the binding the run stamps onto its actions as ``_egress_auth`` so
the web broker can mint the header host-side. Deleting a binding revokes
nothing by itself — the caller revokes the vault ref, then drops the row,
so a torn delete fails toward a dead ref, never a live orphan secret.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Callable

_SCHEMA = """CREATE TABLE IF NOT EXISTS node_credentials (
    tenant_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    host TEXT NOT NULL,
    header TEXT NOT NULL,
    scheme TEXT NOT NULL DEFAULT 'Bearer',
    ref_id TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, node_id, host, header)
)"""


class NodeCredentialStore:
    def __init__(self, conn, *, clock: Callable[[], datetime] | None = None):
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def bind(
        self,
        *,
        tenant: str,
        node_id: str,
        host: str,
        ref_id: str,
        header: str = "Authorization",
        scheme: str = "Bearer",
        label: str = "",
    ) -> str | None:
        """Bind (or replace) one host's credential. Returns the PREVIOUS
        ref_id when a binding stood — the caller revokes it in the vault,
        so a replaced key dies instead of lingering resolvable."""
        host = str(host or "").strip().lower()
        header = str(header or "Authorization").strip() or "Authorization"
        if not host:
            raise ValueError("a credential binds to a host")
        if not ref_id:
            raise ValueError("a credential binding needs its vault ref")
        with self._conn.transaction() as db:
            row = db.execute(
                """SELECT ref_id FROM node_credentials
                   WHERE tenant_id = ? AND node_id = ? AND host = ?
                     AND header = ?""",
                (tenant, node_id, host, header),
            ).fetchone()
            previous = row["ref_id"] if row else None
            db.execute(
                """INSERT INTO node_credentials
                     (tenant_id, node_id, host, header, scheme, ref_id,
                      label, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(tenant_id, node_id, host, header)
                   DO UPDATE SET scheme = excluded.scheme,
                                 ref_id = excluded.ref_id,
                                 label = excluded.label,
                                 created_at = excluded.created_at""",
                (
                    tenant,
                    node_id,
                    host,
                    header,
                    str(scheme),
                    str(ref_id),
                    str(label or ""),
                    self._clock().isoformat(),
                ),
            )
        return previous

    def for_node(self, *, tenant: str, node_id: str) -> list[dict]:
        """The node's bindings, ref ids only — what ``_egress_auth``
        stamps carry. Values never appear here."""
        with self._conn.lock:
            rows = self._conn.db.execute(
                """SELECT host, header, scheme, ref_id, label, created_at
                   FROM node_credentials
                   WHERE tenant_id = ? AND node_id = ?
                   ORDER BY host, header""",
                (tenant, node_id),
            ).fetchall()
        return [
            {
                "host": row["host"],
                "header": row["header"],
                "scheme": row["scheme"],
                "ref": row["ref_id"],
                "label": row["label"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def drop(
        self, *, tenant: str, node_id: str, host: str, header: str = "Authorization"
    ) -> str | None:
        """Remove one binding; returns its ref_id for the vault revoke."""
        host = str(host or "").strip().lower()
        with self._conn.transaction() as db:
            row = db.execute(
                """SELECT ref_id FROM node_credentials
                   WHERE tenant_id = ? AND node_id = ? AND host = ?
                     AND header = ?""",
                (tenant, node_id, host, header),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                """DELETE FROM node_credentials
                   WHERE tenant_id = ? AND node_id = ? AND host = ?
                     AND header = ?""",
                (tenant, node_id, host, header),
            )
        return row["ref_id"]

    def erase_node(self, *, tenant: str, node_id: str) -> list[str]:
        """Every binding of one node, dropped; refs returned for revoke."""
        with self._conn.transaction() as db:
            rows = db.execute(
                """SELECT ref_id FROM node_credentials
                   WHERE tenant_id = ? AND node_id = ?""",
                (tenant, node_id),
            ).fetchall()
            db.execute(
                "DELETE FROM node_credentials WHERE tenant_id = ? AND node_id = ?",
                (tenant, node_id),
            )
        return [row["ref_id"] for row in rows]
