from __future__ import annotations

import json
import re

from .models import Listing, ListingStatus, Node, NodeVersion, PricingPolicy, Visibility

# The one tokenizer discovery speaks: plain lowercase word runs. The FTS
# body is indexed by unicode61 (which splits the same way on ':', '_',
# and punctuation), so a query tokenized HERE meets the index there.
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _query_tokens(query: str) -> list[str]:
    return _TOKEN_RE.findall(str(query or "").lower())


def _listing_body(title: str, summary: str, tags_json: str) -> str:
    """The one text the index holds for a listing: its words and its
    function-derived capability tokens, flattened."""
    return f"{title} {summary} {tags_json}"


def _fts_match(tokens: list[str]) -> str:
    """A sanitized FTS5 MATCH expression: every query word as a quoted
    prefix term, conjoined — 'deploy' still meets 'deployment' the way
    the LIKE scan's substring did, and no user byte reaches the MATCH
    grammar unquoted."""
    return " ".join(f'"{token}"*' for token in tokens)

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
    # V5: the denormalized FACE of a node — the words its scans need
    # (title, goal sentence, whether a script stands behind it) written
    # once at publish, so the own-desk and registry scans stop fetching
    # and parsing a version per node. A projection, never authoritative:
    # the version row remains the truth, and the ctor backfills faces a
    # pre-V5 store never wrote.
    """CREATE TABLE IF NOT EXISTS node_faces (
        node_id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        noder_principal TEXT NOT NULL,
        skill_id TEXT NOT NULL,
        title TEXT NOT NULL,
        goal TEXT NOT NULL,
        has_script INTEGER NOT NULL DEFAULT 0,
        capabilities_json TEXT NOT NULL DEFAULT '[]',
        updated_at TEXT NOT NULL
    )""",
)

# V5: the inverted index discovery reads — one FTS row per listing over
# title + summary + tags + derived capabilities, maintained beside the
# listings table. Created separately from _SCHEMA so the ctor can probe
# for it and backfill a pre-V5 store's rows.
_FTS_SCHEMA = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS listing_fts USING fts5("
    "listing_id UNINDEXED, body)"
)


class RegistryStore:
    def __init__(self, conn) -> None:
        import sqlite3

        self._conn = conn
        # The FTS index and the schema probes are SQLite features; on the
        # PostgreSQL adapter the portable paths stand (the LIKE scan for
        # discovery, fresh CREATEs for the tables) — behavior, not the
        # index, is the contract.
        self._sqlite = isinstance(
            getattr(conn, "db", None), sqlite3.Connection
        )
        with self._conn.transaction() as db:
            had_fts = False
            if self._sqlite:
                # A node_aliases table from V4's first (principal-blind)
                # cut is rebuilt to the owner-keyed shape: aliases are a
                # finding aid regenerated at the next publish, so
                # dropping the old rows loses nothing durable.
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
                had_fts = bool(
                    db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table'"
                        " AND name = 'listing_fts'"
                    ).fetchone()
                )
            for statement in _SCHEMA:
                db.execute(statement)
            if self._sqlite:
                db.execute(_FTS_SCHEMA)
                if not had_fts:
                    # A pre-V5 store: index every standing listing once.
                    for l_row in db.execute(
                        "SELECT listing_id, title, summary, tags_json"
                        " FROM listings"
                    ).fetchall():
                        db.execute(
                            "INSERT INTO listing_fts (listing_id, body)"
                            " VALUES (?, ?)",
                            (
                                l_row["listing_id"],
                                _listing_body(
                                    l_row["title"],
                                    l_row["summary"],
                                    l_row["tags_json"],
                                ),
                            ),
                        )
        self._backfill_faces()

    def _backfill_faces(self) -> None:
        """Write faces a pre-V5 store never had: one parse per node that
        lacks a row, from its newest version — after which publishes keep
        them fresh and the scans never parse a version again."""
        with self._conn.lock:
            missing = self._conn.db.execute(
                """SELECT n.node_id FROM nodes n
                   WHERE NOT EXISTS (
                     SELECT 1 FROM node_faces f WHERE f.node_id = n.node_id
                   )"""
            ).fetchall()
        for row in missing:
            with self._conn.lock:
                version_row = self._conn.db.execute(
                    """SELECT payload_json FROM node_versions
                       WHERE node_id = ? ORDER BY published_at DESC LIMIT 1""",
                    (row["node_id"],),
                ).fetchone()
            if version_row is None:
                continue
            try:
                version = NodeVersion.model_validate_json(
                    version_row["payload_json"]
                )
            except Exception:  # noqa: BLE001 - a bad row earns no face
                continue
            self._write_face(version)

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
        # The face follows the newest version (V5): scans read words from
        # here, never by fetching and parsing a version per node.
        self._write_face(version)

    def _write_face(self, version: NodeVersion) -> None:
        node = self.get_node(version.node_id)
        if node is None:
            return
        try:
            skill = json.loads(version.sanitized_skill_json)
        except Exception:  # noqa: BLE001 - a bad skill earns no face
            return
        actions = skill.get("actions") or []
        has_script = any(
            a.get("adapter") == "script"
            and (a.get("parameters") or {}).get("script")
            for a in actions
        )
        with self._conn.transaction() as db:
            db.execute(
                """INSERT OR REPLACE INTO node_faces
                   (node_id, tenant_id, noder_principal, skill_id, title,
                    goal, has_script, capabilities_json, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?,
                           COALESCE((SELECT capabilities_json FROM node_faces
                                     WHERE node_id = ?), '[]'),
                           ?)""",
                (
                    node.node_id,
                    node.tenant_id,
                    node.noder_principal,
                    node.skill_id,
                    str(skill.get("name") or ""),
                    str(skill.get("description") or ""),
                    1 if has_script else 0,
                    node.node_id,
                    version.published_at.isoformat(),
                ),
            )

    def own_faces(self, tenant_id: str, noder_principal: str) -> list[dict]:
        """The caller's desk as words — one SQL, no version fetches: the
        rows every own-node scan (twin guard, goal re-find, reminder
        check, find_nodes) reads instead of parsing skills per node."""
        with self._conn.lock:
            rows = self._conn.db.execute(
                """SELECT f.node_id, f.skill_id, f.title, f.goal,
                          f.has_script, f.capabilities_json
                   FROM node_faces f JOIN nodes n ON f.node_id = n.node_id
                   WHERE f.tenant_id = ? AND f.noder_principal = ?
                   ORDER BY n.created_at DESC""",
                (tenant_id, noder_principal),
            ).fetchall()
        faces: list[dict] = []
        for row in rows:
            try:
                capabilities = json.loads(row["capabilities_json"])
            except Exception:  # noqa: BLE001
                capabilities = []
            faces.append(
                {
                    "node_id": row["node_id"],
                    "skill_id": row["skill_id"],
                    "title": row["title"],
                    "goal": row["goal"],
                    "has_script": bool(row["has_script"]),
                    "capabilities": capabilities,
                }
            )
        return faces

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
        tags_json = json.dumps(listing.tags + listing.capabilities)
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
                    tags_json,
                    listing.model_dump_json(),
                    listing.updated_at.isoformat(),
                ),
            )
            if self._sqlite:
                db.execute(
                    "INSERT INTO listing_fts (listing_id, body)"
                    " VALUES (?, ?)",
                    (
                        listing.listing_id,
                        _listing_body(
                            listing.title, listing.summary, tags_json
                        ),
                    ),
                )
        self._refresh_face_capabilities(listing)

    def update_listing(self, listing: Listing) -> None:
        tags_json = json.dumps(listing.tags + listing.capabilities)
        with self._conn.transaction() as db:
            db.execute(
                """UPDATE listings SET status = ?, title = ?, summary = ?,
                   tags_json = ?, payload_json = ?, updated_at = ?
                   WHERE listing_id = ?""",
                (
                    listing.status.value,
                    listing.title,
                    listing.summary,
                    tags_json,
                    listing.model_dump_json(),
                    listing.updated_at.isoformat(),
                    listing.listing_id,
                ),
            )
            if self._sqlite:
                db.execute(
                    "DELETE FROM listing_fts WHERE listing_id = ?",
                    (listing.listing_id,),
                )
                db.execute(
                    "INSERT INTO listing_fts (listing_id, body)"
                    " VALUES (?, ?)",
                    (
                        listing.listing_id,
                        _listing_body(
                            listing.title, listing.summary, tags_json
                        ),
                    ),
                )
        self._refresh_face_capabilities(listing)

    def _refresh_face_capabilities(self, listing: Listing) -> None:
        """Stamp the listing's derived capability tokens onto its node's
        face, so capability scoring reads the face alone."""
        with self._conn.transaction() as db:
            db.execute(
                """UPDATE node_faces SET capabilities_json = ?
                   WHERE node_id = (SELECT node_id FROM node_versions
                                    WHERE version_id = ?)""",
                (json.dumps(list(listing.capabilities)), listing.version_id),
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
        self, tenant_id: str, limit: int = 200, query: str = ""
    ) -> list[tuple[Node, Listing]]:
        """The tenant's standing registry: every member's live PUBLIC
        nodes with their newest listing — DRAFT listings included,
        because a desk-standing node needn't be marketplace-published to
        be standing work, but unlisted and private nodes keep their
        word: neither is discoverable, to tenant siblings included. One
        pair per node (the newest listing speaks for it), bounded.

        With a ``query`` (V5), the FTS index pre-narrows to listings
        sharing ANY of the ask's words — OR, not AND, because the
        capability scorer downstream judges partial overlap itself — and
        the newest-``limit`` scan stands in whenever the words find
        nothing (a trigram-only paraphrase still deserves its bounded
        look). Without one, newest-first exactly as before."""
        tokens = _query_tokens(query) if self._sqlite else []
        rows: list = []
        if tokens:
            with self._conn.lock:
                rows = self._conn.db.execute(
                    """SELECT n.payload_json AS node_json,
                              l.payload_json AS listing_json
                       FROM listing_fts
                       JOIN listings l
                         ON l.listing_id = listing_fts.listing_id
                       JOIN node_versions v ON l.version_id = v.version_id
                       JOIN nodes n ON v.node_id = n.node_id
                       WHERE listing_fts MATCH ?
                         AND n.tenant_id = ? AND n.revoked_at IS NULL
                         AND n.visibility = ?
                         AND l.status IN (?, ?)
                       ORDER BY bm25(listing_fts), l.updated_at DESC
                       LIMIT ?""",
                    (
                        " OR ".join(f'"{t}"*' for t in tokens),
                        tenant_id,
                        Visibility.PUBLIC.value,
                        ListingStatus.DRAFT.value,
                        ListingStatus.ACTIVE.value,
                        limit,
                    ),
                ).fetchall()
        if not rows:
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
        """Marketplace discovery over the inverted index (V5): the query's
        words match the FTS body (title + summary + tags + derived
        capabilities) as conjoined prefix terms, ranked by relevance then
        recency — bounded work at any registry size, where the LIKE scan
        read every row. An empty query still lists everything, newest
        first, exactly as before."""
        tokens = _query_tokens(query)
        if not tokens:
            with self._conn.lock:
                rows = self._conn.db.execute(
                    """SELECT l.payload_json AS payload_json FROM listings l
                       JOIN node_versions v ON l.version_id = v.version_id
                       JOIN nodes n ON v.node_id = n.node_id
                       WHERE n.revoked_at IS NULL AND n.visibility = ?
                         AND l.status = ?
                       ORDER BY l.updated_at DESC""",
                    (Visibility.PUBLIC.value, ListingStatus.ACTIVE.value),
                ).fetchall()
            return [
                Listing.model_validate_json(row["payload_json"]) for row in rows
            ]
        if not self._sqlite:
            # The portable path: the substring scan, exactly as before
            # the index existed — PostgreSQL keeps behavior, not speed.
            pattern = "%" + query.lower() + "%"
            with self._conn.lock:
                rows = self._conn.db.execute(
                    """SELECT l.payload_json AS payload_json FROM listings l
                       JOIN node_versions v ON l.version_id = v.version_id
                       JOIN nodes n ON v.node_id = n.node_id
                       WHERE n.revoked_at IS NULL AND n.visibility = ?
                         AND l.status = ?
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
            return [
                Listing.model_validate_json(row["payload_json"]) for row in rows
            ]
        with self._conn.lock:
            rows = self._conn.db.execute(
                """SELECT l.payload_json AS payload_json,
                          bm25(listing_fts) AS rank
                   FROM listing_fts
                   JOIN listings l ON l.listing_id = listing_fts.listing_id
                   JOIN node_versions v ON l.version_id = v.version_id
                   JOIN nodes n ON v.node_id = n.node_id
                   WHERE listing_fts MATCH ?
                     AND n.revoked_at IS NULL AND n.visibility = ?
                     AND l.status = ?
                   ORDER BY rank, l.updated_at DESC""",
                (
                    _fts_match(tokens),
                    Visibility.PUBLIC.value,
                    ListingStatus.ACTIVE.value,
                ),
            ).fetchall()
        return [Listing.model_validate_json(row["payload_json"]) for row in rows]
