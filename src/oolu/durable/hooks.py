"""Node webhooks — the inbound door that fires one node's own function.

A node's owner can mint ONE webhook per node: an unguessable token that,
presented on ``POST /v1/hooks/nodes/{node_id}/{token}``, runs the node's
stored function with the caller's payload staged as a file. The token is
the whole credential (the caller is an outside system with no account),
so it is treated like a password:

* only its SHA-256 lands in the store — a database read never yields a
  usable token, and the plaintext is shown exactly once, at minting;
* verification compares digests in constant time;
* re-minting rotates: the old token dies the moment a new one is born,
  which is also the recovery story for a leaked URL;
* the hook fires as the OWNER who minted it — the run wears their
  identity, their quotas, and their node's egress grants, and the
  confirmation walls (model-written code re-earns approval; audit-mode
  holds) bind exactly as if the owner had pressed run.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

_SCHEMA = """CREATE TABLE IF NOT EXISTS node_hooks (
    node_id TEXT PRIMARY KEY,
    tenant TEXT NOT NULL,
    principal TEXT NOT NULL,
    token_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
)"""


@dataclass(frozen=True)
class NodeHook:
    """One node's live webhook — identity only, never the token."""

    node_id: str
    tenant: str
    principal: str
    created_at: str


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class NodeHookStore:
    """Per-node webhook tokens over the shell's durable connection."""

    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def mint(self, node_id: str, *, tenant: str, principal: str) -> str:
        """Create (or rotate) the node's webhook; returns the plaintext
        token — the ONLY time it exists outside the caller's hands."""
        token = secrets.token_urlsafe(32)
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO node_hooks
                   (node_id, tenant, principal, token_sha256, created_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(node_id) DO UPDATE SET
                     tenant = excluded.tenant,
                     principal = excluded.principal,
                     token_sha256 = excluded.token_sha256,
                     created_at = excluded.created_at""",
                (
                    node_id,
                    tenant,
                    principal,
                    _digest(token),
                    datetime.now(UTC).isoformat(),
                ),
            )
        return token

    def revoke(self, node_id: str) -> bool:
        with self._conn.transaction() as db:
            cursor = db.execute("DELETE FROM node_hooks WHERE node_id = ?", (node_id,))
            return cursor.rowcount > 0

    def get(self, node_id: str) -> NodeHook | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT node_id, tenant, principal, created_at"
                " FROM node_hooks WHERE node_id = ?",
                (node_id,),
            ).fetchone()
        if row is None:
            return None
        return NodeHook(
            node_id=row["node_id"],
            tenant=row["tenant"],
            principal=row["principal"],
            created_at=row["created_at"],
        )

    def verify(self, node_id: str, token: str) -> NodeHook | None:
        """The hook iff the presented token is exactly the minted one —
        digest comparison in constant time; anything else is None (the
        route answers the same 404 for a wrong token and a node that
        never had a hook, so the door confirms nothing)."""
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT node_id, tenant, principal, token_sha256, created_at"
                " FROM node_hooks WHERE node_id = ?",
                (node_id,),
            ).fetchone()
        if row is None:
            return None
        if not hmac.compare_digest(_digest(token or ""), row["token_sha256"]):
            return None
        return NodeHook(
            node_id=row["node_id"],
            tenant=row["tenant"],
            principal=row["principal"],
            created_at=row["created_at"],
        )
