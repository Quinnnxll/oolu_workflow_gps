"""Node accounts: the operator-facing identity a node carries in Work.

The registry knows what a node IS (skill, versions, listings); the account
says who ANSWERS for it and under what regime — the responsible noder, an
optional admin group (organizations and governments manage fleets), an
authority level (1-5), a lifecycle status, and whether it is an audit node
(every request must be committed manually before it runs).

Accounts are deliberately separate from the frozen registry records: they
change operationally (verification, incidents, stewardship handoffs)
without minting node versions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

AUTHORITY_MIN = 1
AUTHORITY_MAX = 5


class NodeStatus(str, Enum):
    LIVE = "live"
    NEEDS_VERIFICATION = "needs_verification"
    ERROR = "error"
    # Placed under the Node Policy's restriction (clone / fraud / zombie):
    # refuses new runs and leaves marketplace ranking until resolved.
    RESTRICTED = "restricted"


class NodeAccount(BaseModel):
    model_config = ConfigDict(frozen=True)

    node_id: str
    # The principal answering for this node's behavior. EMPTY means the
    # node is not onboarded yet: a node created under a Supernode starts
    # with no responsible account — the regime is fixed, but who answers
    # for it is decided by whoever onboards with their user account (the
    # node id is that claim ticket, so it must not be shared publicly
    # until the intended person has onboarded).
    responsible: str = ""
    # Optional management group above the responsible — organizations and
    # government deployments; personal nodes leave it unset.
    admin: str | None = None
    # Authority exists ONLY under a Supernode: a standalone node has no
    # level at all (None). Nodes created under a Supernode carry 1-5,
    # set by the Supernode's humans at creation and FIXED from then on —
    # nobody, the Supernode's humans included, can change it later.
    authority_level: int | None = Field(
        default=None, ge=AUTHORITY_MIN, le=AUTHORITY_MAX
    )
    # A Supernode is a node that manages many nodes — a group, a
    # corporation, or a government division — with humans in full control:
    # it is always an audit node.
    is_supernode: bool = False
    # The Supernode this node was created under, if any. Fixed at creation.
    supernode_id: str | None = None
    # New nodes are not live until verified; errors demote explicitly.
    status: NodeStatus = NodeStatus.NEEDS_VERIFICATION
    # An audit node never runs unattended: every request is held until a
    # human commits it. FIXED AT CREATION — a node cannot quietly shed
    # its audit regime later.
    audit_mode: bool = False
    # May data passing through this node feed auto-development (trace
    # learning, node synthesis)? Enterprise and government nodes that carry
    # customer or citizen data turn this OFF: their runs execute normally
    # but leave nothing behind for the growth loop to learn from.
    # FIXED AT CREATION, like audit_mode.
    allow_autodev_data: bool = True
    # Which Node Policy the creator agreed to, upfront, at creation —
    # the agreement that authorizes hygiene (clone/fraud/zombie
    # detection with restriction or removal). FIXED AT CREATION.
    policy_version: str = ""
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


_SCHEMA = """CREATE TABLE IF NOT EXISTS node_accounts (
    node_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL
)"""


class NodeAccountStore:
    """SQLite-backed accounts over the shell's own durable connection."""

    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def upsert(self, account: NodeAccount) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO node_accounts (node_id, payload_json)
                   VALUES (?, ?)
                   ON CONFLICT(node_id) DO UPDATE SET
                     payload_json = excluded.payload_json""",
                (account.node_id, account.model_dump_json()),
            )

    def get(self, node_id: str) -> NodeAccount | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json FROM node_accounts WHERE node_id = ?",
                (node_id,),
            ).fetchone()
        if row is None:
            return None
        return NodeAccount.model_validate_json(row["payload_json"])

    def under(self, supernode_id: str) -> list[NodeAccount]:
        """Every account created under one Supernode — the org's members.
        A human-sized scan: a Supernode manages a fleet, not a census."""
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT payload_json FROM node_accounts"
            ).fetchall()
        accounts = [
            NodeAccount.model_validate_json(row["payload_json"]) for row in rows
        ]
        return [a for a in accounts if a.supernode_id == supernode_id]
