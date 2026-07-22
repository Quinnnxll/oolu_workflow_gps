"""Node provenance: immutable commits, sealed releases, honest revocation.

The build policy's spine, in three rules:

    Draft nodes evolve by revision.  Every write to a node's function —
    build, revise, repair, hand edit — files an immutable, content-hashed
    COMMIT with its parent, its instruction, and its author. Nothing is
    overwritten; the drawer's current tree is just the head of a chain
    that preserves every attempt.

    Verified nodes are sealed.  When a run through the node's own
    function verifies it, the EXACT tree that ran is sealed as a release
    — content-addressed, append-only, pinned to its commit. Editing the
    drawer afterwards never edits the release; it starts a new draft the
    next verification can seal.

    Vulnerable releases are revoked, not modified.  A release's artifact
    rows never change; a separate CONTROL row carries its operational
    status (active | revoked). Revoking refuses new runs of that exact
    tree — the reason named — while a revised tree is a new draft and
    may run to earn a new seal. The revoked artifact stays on the ledger
    for the audit, forever.

Both ledgers are per-tenant, append-only, and idempotent: the same tree
is the same commit, the same sealed content is the same release, so a
retry files one row.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

# Past this, a commit keeps every file's HASH but not its bytes — the
# chain stays honest about what changed without swallowing a large tree.
MAX_COMMIT_CONTENT_BYTES = 512 * 1024

COMMIT_KINDS = ("build", "revise", "repair", "edit", "snapshot")


def _now() -> datetime:
    return datetime.now(UTC)


def tree_hash(files: dict[str, str]) -> str:
    """One hash for one exact source tree: every path and every byte,
    order-independent. The identity commits, releases, and the
    revocation guard all speak."""
    digest = hashlib.sha256()
    for path in sorted(files or {}):
        content = str(files[path])
        digest.update(path.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(content.encode()).digest())
        digest.update(b"\0")
    return digest.hexdigest()


class NodeCommit(BaseModel):
    model_config = ConfigDict(frozen=True)

    commit_id: str
    tenant_id: str
    node_id: str
    parent_id: str = ""
    tree_hash: str
    # path -> sha256 of content, always; the bytes ride too while the
    # tree is small enough to keep whole.
    file_hashes: dict[str, str] = Field(default_factory=dict)
    files: dict[str, str] = Field(default_factory=dict)
    kind: str = "build"
    instruction: str = ""
    by: str = ""
    created_at: datetime = Field(default_factory=_now)


class NodeRelease(BaseModel):
    model_config = ConfigDict(frozen=True)

    release_id: str
    tenant_id: str
    node_id: str
    commit_id: str = ""
    tree_hash: str
    semver: str = ""
    verified_by_run: str = ""
    sealed_at: datetime = Field(default_factory=_now)


_COMMITS_SCHEMA = """CREATE TABLE IF NOT EXISTS node_commits (
    tenant_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    commit_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (tenant_id, node_id, commit_id)
)"""

_HEADS_SCHEMA = """CREATE TABLE IF NOT EXISTS node_commit_heads (
    tenant_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    commit_id TEXT NOT NULL,
    PRIMARY KEY (tenant_id, node_id)
)"""

_RELEASES_SCHEMA = """CREATE TABLE IF NOT EXISTS node_releases (
    tenant_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    release_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (tenant_id, node_id, release_id)
)"""

_CONTROLS_SCHEMA = """CREATE TABLE IF NOT EXISTS node_release_controls (
    tenant_id TEXT NOT NULL,
    release_id TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    changed_by TEXT NOT NULL DEFAULT '',
    changed_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, release_id)
)"""


class NodeProvenance:
    """Both ledgers over one connection: ``commit`` files history,
    ``seal`` files releases, ``revoke`` flips a control row — and
    nothing here can update or delete an artifact row, by construction:
    the only SQL this module speaks is INSERT OR IGNORE (artifacts),
    head-pointer upsert, and control-row upsert."""

    def __init__(self, conn, *, clock: Callable[[], datetime] | None = None):
        self._conn = conn
        self._clock = clock or _now
        with self._conn.transaction() as db:
            db.execute(_COMMITS_SCHEMA)
            db.execute(_HEADS_SCHEMA)
            db.execute(_RELEASES_SCHEMA)
            db.execute(_CONTROLS_SCHEMA)

    # -- commits: the drawer's immutable history ------------------------- #
    def commit(
        self,
        tenant: str,
        node_id: str,
        files: dict[str, str],
        *,
        kind: str = "build",
        instruction: str = "",
        by: str = "",
    ) -> NodeCommit:
        """File the node's current source tree as an immutable commit,
        chained to the previous head. The same tree twice in a row is
        the SAME commit (no empty history); an oversized tree keeps
        every file hash and drops the bytes."""
        if kind not in COMMIT_KINDS:
            kind = "edit"
        files = {str(path): str(content) for path, content in (files or {}).items()}
        head = self.head(tenant, node_id)
        digest = tree_hash(files)
        if head is not None and head.tree_hash == digest:
            return head
        parent_id = head.commit_id if head is not None else ""
        commit_id = "nc" + hashlib.sha256(
            f"{tenant}|{node_id}|{parent_id}|{digest}".encode()
        ).hexdigest()[:20]
        total = sum(len(content.encode()) for content in files.values())
        record = NodeCommit(
            commit_id=commit_id,
            tenant_id=tenant,
            node_id=node_id,
            parent_id=parent_id,
            tree_hash=digest,
            file_hashes={
                path: hashlib.sha256(content.encode()).hexdigest()
                for path, content in files.items()
            },
            files=files if total <= MAX_COMMIT_CONTENT_BYTES else {},
            kind=kind,
            instruction=instruction[:2000],
            by=by,
            created_at=self._clock(),
        )
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO node_commits
                       (tenant_id, node_id, commit_id, payload_json)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(tenant_id, node_id, commit_id) DO NOTHING""",
                (tenant, node_id, commit_id, record.model_dump_json()),
            )
            db.execute(
                """INSERT INTO node_commit_heads (tenant_id, node_id, commit_id)
                   VALUES (?, ?, ?)
                   ON CONFLICT(tenant_id, node_id)
                   DO UPDATE SET commit_id = excluded.commit_id""",
                (tenant, node_id, commit_id),
            )
        return record

    def head(self, tenant: str, node_id: str) -> NodeCommit | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT commit_id FROM node_commit_heads"
                " WHERE tenant_id = ? AND node_id = ?",
                (tenant, node_id),
            ).fetchone()
        if row is None:
            return None
        return self.get_commit(tenant, node_id, row["commit_id"])

    def get_commit(
        self, tenant: str, node_id: str, commit_id: str
    ) -> NodeCommit | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json FROM node_commits"
                " WHERE tenant_id = ? AND node_id = ? AND commit_id = ?",
                (tenant, node_id, commit_id),
            ).fetchone()
        if row is None:
            return None
        return NodeCommit.model_validate_json(row["payload_json"])

    def history(
        self, tenant: str, node_id: str, *, limit: int = 50
    ) -> list[NodeCommit]:
        """Newest first, following parent links from the head — the
        chain the Code tab reads like a repo's log."""
        chain: list[NodeCommit] = []
        current = self.head(tenant, node_id)
        while current is not None and len(chain) < limit:
            chain.append(current)
            if not current.parent_id:
                break
            current = self.get_commit(tenant, node_id, current.parent_id)
        return chain

    # -- releases: what verification sealed ------------------------------ #
    def seal(
        self,
        tenant: str,
        node_id: str,
        *,
        tree: dict[str, str] | None = None,
        commit_id: str = "",
        semver: str = "",
        verified_by_run: str = "",
    ) -> NodeRelease:
        """Seal the exact verified tree as a release. Content-addressed
        and idempotent: re-verifying the same tree is the same release
        (its control row stays as it was — a revoked artifact cannot be
        laundered by re-sealing it)."""
        digest = tree_hash(tree or {}) if tree is not None else ""
        if not digest:
            head = self.head(tenant, node_id)
            digest = head.tree_hash if head is not None else ""
            commit_id = commit_id or (head.commit_id if head is not None else "")
        release_id = "nr" + hashlib.sha256(
            f"{tenant}|{node_id}|{digest}".encode()
        ).hexdigest()[:20]
        record = NodeRelease(
            release_id=release_id,
            tenant_id=tenant,
            node_id=node_id,
            commit_id=commit_id,
            tree_hash=digest,
            semver=semver,
            verified_by_run=verified_by_run,
            sealed_at=self._clock(),
        )
        with self._conn.transaction() as db:
            row = db.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM node_releases"
                " WHERE tenant_id = ? AND node_id = ?",
                (tenant, node_id),
            ).fetchone()
            db.execute(
                """INSERT INTO node_releases
                       (tenant_id, node_id, release_id, seq, payload_json)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(tenant_id, node_id, release_id) DO NOTHING""",
                (tenant, node_id, release_id, row["seq"], record.model_dump_json()),
            )
            # The control row is born active — but only ONCE: a revoked
            # release stays revoked through any re-seal attempt.
            db.execute(
                """INSERT INTO node_release_controls
                       (tenant_id, release_id, status, reason, changed_by,
                        changed_at)
                   VALUES (?, ?, 'active', '', '', ?)
                   ON CONFLICT(tenant_id, release_id) DO NOTHING""",
                (tenant, release_id, self._clock().isoformat()),
            )
        stored = self.get_release(tenant, node_id, release_id)
        return stored if stored is not None else record

    def get_release(
        self, tenant: str, node_id: str, release_id: str
    ) -> NodeRelease | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json FROM node_releases"
                " WHERE tenant_id = ? AND node_id = ? AND release_id = ?",
                (tenant, node_id, release_id),
            ).fetchone()
        if row is None:
            return None
        return NodeRelease.model_validate_json(row["payload_json"])

    def releases(self, tenant: str, node_id: str) -> list[dict[str, Any]]:
        """Newest first, each with its live control status riding along."""
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT payload_json FROM node_releases"
                " WHERE tenant_id = ? AND node_id = ? ORDER BY seq DESC",
                (tenant, node_id),
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            release = NodeRelease.model_validate_json(row["payload_json"])
            control = self.control(tenant, release.release_id)
            items.append(
                {
                    **json.loads(release.model_dump_json()),
                    "status": control.get("status", "active"),
                    "status_reason": control.get("reason", ""),
                }
            )
        return items

    def latest_release(self, tenant: str, node_id: str) -> NodeRelease | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json FROM node_releases"
                " WHERE tenant_id = ? AND node_id = ?"
                " ORDER BY seq DESC LIMIT 1",
                (tenant, node_id),
            ).fetchone()
        if row is None:
            return None
        return NodeRelease.model_validate_json(row["payload_json"])

    def control(self, tenant: str, release_id: str) -> dict[str, str]:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT status, reason, changed_by, changed_at"
                " FROM node_release_controls"
                " WHERE tenant_id = ? AND release_id = ?",
                (tenant, release_id),
            ).fetchone()
        if row is None:
            return {"status": "active", "reason": ""}
        return {
            "status": row["status"],
            "reason": row["reason"],
            "changed_by": row["changed_by"],
            "changed_at": row["changed_at"],
        }

    def revoke(
        self, tenant: str, release_id: str, *, reason: str, by: str = ""
    ) -> bool:
        """Flip the release's control row to revoked — the artifact rows
        never change. Idempotent; the FIRST reason stands."""
        with self._conn.transaction() as db:
            row = db.execute(
                "SELECT status FROM node_release_controls"
                " WHERE tenant_id = ? AND release_id = ?",
                (tenant, release_id),
            ).fetchone()
            if row is not None and row["status"] == "revoked":
                return False
            db.execute(
                """INSERT INTO node_release_controls
                       (tenant_id, release_id, status, reason, changed_by,
                        changed_at)
                   VALUES (?, ?, 'revoked', ?, ?, ?)
                   ON CONFLICT(tenant_id, release_id)
                   DO UPDATE SET status = 'revoked',
                                 reason = excluded.reason,
                                 changed_by = excluded.changed_by,
                                 changed_at = excluded.changed_at""",
                (
                    tenant,
                    release_id,
                    str(reason)[:500],
                    by,
                    self._clock().isoformat(),
                ),
            )
        return True

    def revoked_tree(self, tenant: str, node_id: str) -> tuple[str, str] | None:
        """(tree_hash, reason) when the node's LATEST release is revoked
        — the one artifact new runs must refuse while the drawer still
        holds that exact tree. A revised tree is a new draft and runs."""
        latest = self.latest_release(tenant, node_id)
        if latest is None:
            return None
        control = self.control(tenant, latest.release_id)
        if control.get("status") != "revoked":
            return None
        return latest.tree_hash, control.get("reason", "")
