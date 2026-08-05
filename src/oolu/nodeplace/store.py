from __future__ import annotations

import json

from .models import Listing, ListingStatus, Node, NodeVersion, PricingPolicy, Visibility

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS nodes (
        node_id TEXT PRIMARY KEY,
        noder_principal TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        skill_id TEXT NOT NULL,
        visibility TEXT NOT NULL,
        revoked_at TEXT,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS node_versions (
        version_id TEXT PRIMARY KEY,
        node_id TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        semver TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        published_at TEXT NOT NULL,
        UNIQUE (node_id, content_hash)
    )""",
    """CREATE TABLE IF NOT EXISTS listings (
        listing_id TEXT PRIMARY KEY,
        version_id TEXT NOT NULL,
        status TEXT NOT NULL,
        title TEXT NOT NULL,
        summary TEXT NOT NULL,
        tags_json TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS pricing_policies (
        policy_id TEXT PRIMARY KEY,
        version_id TEXT NOT NULL UNIQUE,
        payload_json TEXT NOT NULL
    )""",
    # V4: a second, MEANINGFUL lookup key beside a node's random id — the
    # goal-derived alias a program node is findable by. Keyed per OWNER:
    # one alias, one node, per (tenant, principal) — a tenant sibling
    # publishing the same goal writes THEIR row, never repointing (and
    # so silently breaking) the first owner's routing. A rebuild by the
    # same owner repoints their own alias to the newest node.
    """CREATE TABLE IF NOT EXISTS node_aliases (
        tenant_id TEXT NOT NULL,
        noder_principal TEXT NOT NULL,
        alias TEXT NOT NULL,
        node_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (tenant_id, noder_principal, alias)
    )""",
)


class RegistryStore:
    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            # A node_aliases table from V4's first (principal-blind) cut
            # is rebuilt to the owner-keyed shape: aliases are a finding
            # aid regenerated at the next publish, so dropping the old
            # rows loses nothing durable.
            row = db.execute(
                "SELECT 1 FROM pragma_table_info('node_aliases')"
                " WHERE name = 'noder_principal'"
            ).fetchone()
            had_table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table'"
                " AND name = 'node_aliases'"
            ).fetchone()
            if had_table and row is None:
                db.execute("DROP TABLE node_aliases")
            for statement in _SCHEMA:
                db.execute(statement)

    def add_node(self, node: Node) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO nodes
                   (node_id, noder_principal, tenant_id, skill_id, visibility,
                    revoked_at, payload_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    node.node_id,
                    node.noder_principal,
                    node.tenant_id,
                    node.skill_id,
                    node.visibility.value,
                    node.revoked_at.isoformat() if node.revoked_at else None,
                    node.model_dump_json(),
                    node.created_at.isoformat(),
                ),
            )

    def update_node(self, node: Node) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """UPDATE nodes SET visibility = ?, revoked_at = ?, payload_json = ?
                   WHERE node_id = ?""",
                (
                    node.visibility.value,
                    node.revoked_at.isoformat() if node.revoked_at else None,
                    node.model_dump_json(),
                    node.node_id,
                ),
            )

    def get_node(self, node_id: str) -> Node | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json FROM nodes WHERE node_id = ?", (node_id,)
            ).fetchone()
        return Node.model_validate_json(row["payload_json"]) if row else None

    def all_nodes(self) -> list[Node]:
        """Every node on the install — the hygiene sweep's field of view."""
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT payload_json FROM nodes ORDER BY created_at"
            ).fetchall()
        return [Node.model_validate_json(row["payload_json"]) for row in rows]

    def list_nodes(self, tenant_id: str, noder_principal: str) -> list[Node]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                """SELECT payload_json FROM nodes
                   WHERE tenant_id = ? AND noder_principal = ?
                   ORDER BY created_at DESC""",
                (tenant_id, noder_principal),
            ).fetchall()
        return [Node.model_validate_json(row["payload_json"]) for row in rows]

    def add_version(self, version: NodeVersion) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO node_versions
                   (version_id, node_id, content_hash, semver, payload_json, published_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    version.version_id,
                    version.node_id,
                    version.content_hash,
                    version.semver,
                    version.model_dump_json(),
                    version.published_at.isoformat(),
                ),
            )

    def get_version(self, version_id: str) -> NodeVersion | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json FROM node_versions WHERE version_id = ?",
                (version_id,),
            ).fetchone()
        return NodeVersion.model_validate_json(row["payload_json"]) if row else None

    def list_versions(self, node_id: str) -> list[NodeVersion]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT payload_json FROM node_versions WHERE node_id = ?"
                " ORDER BY published_at ASC",
                (node_id,),
            ).fetchall()
        return [NodeVersion.model_validate_json(row["payload_json"]) for row in rows]

    def add_listing(self, listing: Listing) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO listings
                   (listing_id, version_id, status, title, summary, tags_json,
                    payload_json, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    listing.listing_id,
                    listing.version_id,
                    listing.status.value,
                    listing.title,
                    listing.summary,
                    # The search index: author tags AND function-derived
                    # capabilities, so discovery matches what the node DOES.
                    json.dumps(listing.tags + listing.capabilities),
                    listing.model_dump_json(),
                    listing.updated_at.isoformat(),
                ),
            )

    def update_listing(self, listing: Listing) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """UPDATE listings SET status = ?, title = ?, summary = ?,
                   tags_json = ?, payload_json = ?, updated_at = ?
                   WHERE listing_id = ?""",
                (
                    listing.status.value,
                    listing.title,
                    listing.summary,
                    json.dumps(listing.tags + listing.capabilities),
                    listing.model_dump_json(),
                    listing.updated_at.isoformat(),
                    listing.listing_id,
                ),
            )

    def get_listing(self, listing_id: str) -> Listing | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json FROM listings WHERE listing_id = ?", (listing_id,)
            ).fetchone()
        return Listing.model_validate_json(row["payload_json"]) if row else None

    def listing_for_version(self, version_id: str) -> Listing | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json FROM listings WHERE version_id = ?", (version_id,)
            ).fetchone()
        return Listing.model_validate_json(row["payload_json"]) if row else None

    def add_pricing(self, policy: PricingPolicy) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO pricing_policies (policy_id, version_id, payload_json)
                   VALUES (?, ?, ?)""",
                (policy.policy_id, policy.version_id, policy.model_dump_json()),
            )

    def get_pricing(self, version_id: str) -> PricingPolicy | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json FROM pricing_policies WHERE version_id = ?",
                (version_id,),
            ).fetchone()
        return PricingPolicy.model_validate_json(row["payload_json"]) if row else None

    def add_alias(
        self,
        tenant_id: str,
        noder_principal: str,
        alias: str,
        node_id: str,
        *,
        created_at: str,
    ) -> None:
        """Point the OWNER's alias at this node — newest wins, so their
        rebuilt goal resolves to the node that answers for it NOW, and
        a tenant sibling's same-goal publish never touches this row."""
        with self._conn.transaction() as db:
            db.execute(
                """INSERT OR REPLACE INTO node_aliases
                   (tenant_id, noder_principal, alias, node_id, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (tenant_id, noder_principal, alias, node_id, created_at),
            )

    def node_by_alias(
        self, tenant_id: str, noder_principal: str, alias: str
    ) -> Node | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                """SELECT n.payload_json AS payload_json
                   FROM node_aliases a JOIN nodes n ON a.node_id = n.node_id
                   WHERE a.tenant_id = ? AND a.noder_principal = ?
                     AND a.alias = ?""",
                (tenant_id, noder_principal, alias),
            ).fetchone()
        return Node.model_validate_json(row["payload_json"]) if row else None

    def tenant_listings(
        self, tenant_id: str, limit: int = 200
    ) -> list[tuple[Node, Listing]]:
        """The tenant's standing registry: every member's live PUBLIC
        nodes with their newest listing — DRAFT listings included,
        because a desk-standing node needn't be marketplace-published to
        be standing work, but unlisted and private nodes keep their
        word: neither is discoverable, to tenant siblings included. One
        pair per node (the newest listing speaks for it), newest first,
        bounded — the V4 capability search scans this; V5 indexes it."""
        with self._conn.lock:
            rows = self._conn.db.execute(
                """SELECT n.payload_json AS node_json,
                          l.payload_json AS listing_json
                   FROM listings l
                   JOIN node_versions v ON l.version_id = v.version_id
                   JOIN nodes n ON v.node_id = n.node_id
                   WHERE n.tenant_id = ? AND n.revoked_at IS NULL
                     AND n.visibility = ?
                     AND l.status IN (?, ?)
                   ORDER BY l.updated_at DESC
                   LIMIT ?""",
                (
                    tenant_id,
                    Visibility.PUBLIC.value,
                    ListingStatus.DRAFT.value,
                    ListingStatus.ACTIVE.value,
                    limit,
                ),
            ).fetchall()
        pairs: list[tuple[Node, Listing]] = []
        seen: set[str] = set()
        for row in rows:
            try:
                node = Node.model_validate_json(row["node_json"])
                listing = Listing.model_validate_json(row["listing_json"])
            except Exception:  # noqa: BLE001 - one bad row never hides the rest
                continue
            if node.node_id in seen:
                continue  # ordered newest-first: the first listing speaks
            seen.add(node.node_id)
            pairs.append((node, listing))
        return pairs

    def discover(self, query: str = "") -> list[Listing]:
        pattern = "%" + query.lower() + "%"
        with self._conn.lock:
            rows = self._conn.db.execute(
                """SELECT l.payload_json AS payload_json FROM listings l
                   JOIN node_versions v ON l.version_id = v.version_id
                   JOIN nodes n ON v.node_id = n.node_id
                   WHERE n.revoked_at IS NULL AND n.visibility = ? AND l.status = ?
                   AND (lower(l.title) LIKE ? OR lower(l.summary) LIKE ?
                        OR lower(l.tags_json) LIKE ?)
                   ORDER BY l.updated_at DESC""",
                (
                    Visibility.PUBLIC.value,
                    ListingStatus.ACTIVE.value,
                    pattern,
                    pattern,
                    pattern,
                ),
            ).fetchall()
        return [Listing.model_validate_json(row["payload_json"]) for row in rows]
