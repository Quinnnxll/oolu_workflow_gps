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

# A node's egress grant names at most this many hosts — a grant is a
# short, reviewable list, never a mirror of the internet.
MAX_NETWORK_HOSTS = 8

# A Supernode's block list may be longer than a grant (an org refuses more
# than it curates), but it is still a reviewable list, not a firewall dump.
MAX_BLOCKED_HOSTS = 32

# How many principals a Supernode may block. Same spirit: a human-sized,
# reviewable list.
MAX_BLOCKED_USERS = 64


def normalize_network_hosts(
    hosts: object, *, max_hosts: int = MAX_NETWORK_HOSTS, kind: str = "grant"
) -> tuple[str, ...]:
    """A node's egress host list in canonical form: bare lowercase hostnames.

    Used for the allow-grant AND the Supernode's block list — both name
    hosts the same way. Refused in words: schemes/paths (the list names a
    HOST, not a URL), IP literals and localhost (the SSRF guard's
    territory — a grant must never be a tunnel into the machine's own
    network), wildcards, and silly lengths. Subdomains of a listed host
    are implicitly covered, matching the executor's semantics."""
    if hosts is None:
        return ()
    if not isinstance(hosts, (list, tuple)):
        raise ValueError("network hosts must be a list of hostnames")
    cleaned: list[str] = []
    for raw in hosts:
        host = str(raw).strip().lower()
        if not host:
            continue
        if "://" in host or "/" in host or ":" in host or " " in host:
            raise ValueError(
                f"'{host}' is not a bare hostname — a {kind} names hosts, "
                "not URLs or ports"
            )
        if "*" in host:
            raise ValueError(
                f"wildcards are not a {kind} — name the host; subdomains "
                "of a listed host are already covered"
            )
        if host == "localhost" or host.replace(".", "").isdigit():
            raise ValueError(
                f"'{host}' points at an address, not a public name — "
                "the machine's own network is never grantable"
            )
        if "." not in host or len(host) > 253:
            raise ValueError(f"'{host}' is not a valid public hostname")
        if host not in cleaned:
            cleaned.append(host)
    if len(cleaned) > max_hosts:
        raise ValueError(
            f"a {kind} names at most {max_hosts} hosts — a short, "
            "reviewable list, never a mirror of the internet"
        )
    return tuple(cleaned)


def normalize_blocked_hosts(hosts: object) -> tuple[str, ...]:
    """The Supernode's own deny list, canonicalized like a grant."""
    return normalize_network_hosts(
        hosts, max_hosts=MAX_BLOCKED_HOSTS, kind="block list"
    )


def normalize_blocked_users(users: object) -> tuple[str, ...]:
    """The principals a Supernode refuses, canonicalized: trimmed,
    deduplicated usernames — a human-sized list, refused past the cap."""
    if users is None:
        return ()
    if not isinstance(users, (list, tuple)):
        raise ValueError("blocked users must be a list of usernames")
    cleaned: list[str] = []
    for raw in users:
        name = str(raw).strip()
        if not name:
            continue
        if len(name) > 120 or "\n" in name:
            raise ValueError(f"'{name[:40]}…' is not a username")
        if name not in cleaned:
            cleaned.append(name)
    if len(cleaned) > MAX_BLOCKED_USERS:
        raise ValueError(
            f"a block list names at most {MAX_BLOCKED_USERS} users — "
            "a reviewable list, not a census"
        )
    return tuple(cleaned)


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
    # The node's egress CONSENT: the exact public hosts its http actions
    # may reach, granted (and withdrawable) by the responsible human.
    # Empty = no egress at all for this node — fail closed. Operational
    # like status, not fixed at creation: consent can change its mind.
    network_hosts: tuple[str, ...] = ()
    # The other direction, for Supernodes whose web stands OPEN (a verified
    # legal entity under the global account is not limited to an 8-host
    # grant): the hosts this org CHOOSES to refuse. Binds every node down
    # the membership chain. Mutable, like the grant — it is the same
    # consent, inverted.
    blocked_hosts: tuple[str, ...] = ()
    # The principals this Supernode refuses — like a user blocking a user:
    # a blocked principal's messages to the Supernode and its members are
    # refused in words. Mutable by the same humans who answer for the node.
    blocked_users: tuple[str, ...] = ()
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
    # The org template this Supernode resolved to (by key), recorded the
    # FIRST time the template button ran — so the choice is decided once
    # and never re-reasoned, model or not. Empty = not resolved yet.
    org_template: str = ""
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
        return [a for a in self._all() if a.supernode_id == supernode_id]

    def answered_by(self, principal: str) -> list[NodeAccount]:
        """Every account this principal answers for — as the responsible
        who onboarded it, or as its admin. This is how a node someone ELSE
        created (a Supernode's humans minting claim tickets) reaches the
        onboarder's own Work desk: responsibility lives on the account,
        not in the registry's creator column."""
        return [
            a for a in self._all() if principal in {a.responsible, a.admin}
        ]

    def _all(self) -> list[NodeAccount]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT payload_json FROM node_accounts"
            ).fetchall()
        return [
            NodeAccount.model_validate_json(row["payload_json"]) for row in rows
        ]
