"""The Global Project Graph's storage — every write versioned, forever.

One SQLite home (the shell's own durable connection) for projects,
their objects at current revision, the FULL history of every committed
revision, per-principal scopes, and every proposal ever decided. The
store exposes reads and bookkeeping; committing state changes is the
transaction kernel's monopoly — ``apply_commit`` exists for the kernel
and writes an entire proposal's objects in ONE transaction, so a crash
can never leave half a commit behind.
"""

from __future__ import annotations

from datetime import UTC, datetime

from .models import GraphObject, GraphProposal, GraphScopes, ProposalResult

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS graph_projects (
        project_id TEXT PRIMARY KEY,
        tenant TEXT NOT NULL,
        owner TEXT NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS graph_objects (
        project_id TEXT NOT NULL,
        object_id TEXT NOT NULL,
        path TEXT NOT NULL,
        revision INTEGER NOT NULL,
        status TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        PRIMARY KEY (project_id, object_id)
    )""",
    """CREATE INDEX IF NOT EXISTS idx_graph_objects_path
       ON graph_objects(project_id, path)""",
    """CREATE TABLE IF NOT EXISTS graph_history (
        project_id TEXT NOT NULL,
        object_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        proposal_id TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        at TEXT NOT NULL,
        PRIMARY KEY (project_id, object_id, revision)
    )""",
    """CREATE TABLE IF NOT EXISTS graph_scopes (
        project_id TEXT NOT NULL,
        principal TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        PRIMARY KEY (project_id, principal)
    )""",
    """CREATE TABLE IF NOT EXISTS graph_proposals (
        proposal_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        status TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        result_json TEXT NOT NULL,
        at TEXT NOT NULL
    )""",
)


class ProjectGraphStore:
    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            for statement in _SCHEMA:
                db.execute(statement)

    # ------------------------------------------------------------------ #
    # Projects: tenant-walled, owned by whoever opened them.              #
    # ------------------------------------------------------------------ #
    def ensure_project(
        self, project_id: str, *, tenant: str, owner: str, name: str = ""
    ) -> dict | None:
        """The project row, creating it on first touch — the toucher
        becomes its OWNER (the same claim pattern as node onboarding).
        Another tenant's project answers None, never a hint."""
        with self._conn.transaction() as db:
            db.execute(
                """INSERT OR IGNORE INTO graph_projects
                   (project_id, tenant, owner, name, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    project_id,
                    tenant,
                    owner,
                    name,
                    datetime.now(UTC).isoformat(),
                ),
            )
            row = db.execute(
                "SELECT * FROM graph_projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if row is None or row["tenant"] != tenant:
            return None
        return dict(row)

    def project(self, project_id: str, *, tenant: str) -> dict | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT * FROM graph_projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if row is None or row["tenant"] != tenant:
            return None
        return dict(row)

    # ------------------------------------------------------------------ #
    # Objects and their history.                                          #
    # ------------------------------------------------------------------ #
    def get(self, project_id: str, object_id: str) -> GraphObject | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json FROM graph_objects"
                " WHERE project_id = ? AND object_id = ?",
                (project_id, object_id),
            ).fetchone()
        if row is None:
            return None
        return GraphObject.model_validate_json(row["payload_json"])

    def at_revision(
        self, project_id: str, object_id: str, revision: int
    ) -> GraphObject | None:
        """The object exactly as some committed revision left it —
        every write is versioned, so every past truth stays readable."""
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json FROM graph_history"
                " WHERE project_id = ? AND object_id = ? AND revision = ?",
                (project_id, object_id, revision),
            ).fetchone()
        if row is None:
            return None
        return GraphObject.model_validate_json(row["payload_json"])

    def list(self, project_id: str, *, path: str = "") -> list[GraphObject]:
        """Current objects, optionally under one subtree, path-ordered."""
        prefix = path.strip("/")
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT payload_json, path FROM graph_objects"
                " WHERE project_id = ? ORDER BY path ASC, object_id ASC",
                (project_id,),
            ).fetchall()
        objects = [
            GraphObject.model_validate_json(row["payload_json"])
            for row in rows
            if not prefix
            or row["path"] == prefix
            or row["path"].startswith(prefix + "/")
        ]
        return objects

    def apply_commit(
        self,
        project_id: str,
        objects: list[GraphObject],
        *,
        proposal_id: str,
    ) -> None:
        """The kernel's one write: current rows and history rows for an
        entire committed proposal, in a single transaction."""
        now = datetime.now(UTC).isoformat()
        with self._conn.transaction() as db:
            for obj in objects:
                payload = obj.model_dump_json()
                db.execute(
                    """INSERT INTO graph_objects
                       (project_id, object_id, path, revision, status,
                        payload_json)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(project_id, object_id) DO UPDATE SET
                         path = excluded.path,
                         revision = excluded.revision,
                         status = excluded.status,
                         payload_json = excluded.payload_json""",
                    (
                        project_id,
                        obj.object_id,
                        obj.path,
                        obj.revision,
                        obj.status,
                        payload,
                    ),
                )
                db.execute(
                    """INSERT INTO graph_history
                       (project_id, object_id, revision, proposal_id,
                        payload_json, at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        project_id,
                        obj.object_id,
                        obj.revision,
                        proposal_id,
                        payload,
                        now,
                    ),
                )

    # ------------------------------------------------------------------ #
    # Scopes: territory is granted, never assumed.                        #
    # ------------------------------------------------------------------ #
    def grant_scopes(self, project_id: str, scopes: GraphScopes) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO graph_scopes
                   (project_id, principal, payload_json)
                   VALUES (?, ?, ?)
                   ON CONFLICT(project_id, principal) DO UPDATE SET
                     payload_json = excluded.payload_json""",
                (project_id, scopes.principal, scopes.model_dump_json()),
            )

    def scopes_for(
        self, project_id: str, principal: str
    ) -> GraphScopes | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json FROM graph_scopes"
                " WHERE project_id = ? AND principal = ?",
                (project_id, principal),
            ).fetchone()
        if row is None:
            return None
        return GraphScopes.model_validate_json(row["payload_json"])

    # ------------------------------------------------------------------ #
    # The proposal ledger: every decision kept, either way.               #
    # ------------------------------------------------------------------ #
    def record_proposal(
        self, proposal: GraphProposal, result: ProposalResult
    ) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT OR REPLACE INTO graph_proposals
                   (proposal_id, project_id, status, payload_json,
                    result_json, at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    proposal.proposal_id,
                    proposal.project_id,
                    result.status,
                    proposal.model_dump_json(),
                    result.model_dump_json(),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def proposals(self, project_id: str, *, limit: int = 50) -> list[dict]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT payload_json, result_json FROM graph_proposals"
                " WHERE project_id = ? ORDER BY at DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        return [
            {
                "proposal": GraphProposal.model_validate_json(
                    row["payload_json"]
                ),
                "result": ProposalResult.model_validate_json(
                    row["result_json"]
                ),
            }
            for row in rows
        ]
