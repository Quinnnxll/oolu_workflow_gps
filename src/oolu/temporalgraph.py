"""The temporal graph — M1 of the memory-stack plan.

Facts about HOW things relate get what facts about things got in M0:
validity intervals, provenance, supersession-not-deletion. Edges are
adjacency rows on the same durable connection (no graph database until
a measured bottleneck, per the capability-web doc's own rule); the
vocabulary is the doc's §9.2; and every read is time-scoped — "what
depended on X when Y happened" is one query, and a closed or
superseded edge never contributes proximity again.

Projections stay FUNCTIONS: a state card is derived from the stores on
every call, never stored — rebuild-equals-read by construction.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

_SCHEMA = """CREATE TABLE IF NOT EXISTS graph_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    edge_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    attributes TEXT NOT NULL DEFAULT '{}',
    valid_from TEXT NOT NULL,
    valid_until TEXT,
    confidence REAL NOT NULL,
    provenance TEXT NOT NULL,
    superseded_by INTEGER
)"""

_VALID = (
    "superseded_by IS NULL AND valid_from <= ?"
    " AND (valid_until IS NULL OR valid_until > ?)"
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class TemporalGraph:
    """Typed, time-scoped edges over every entity the platform names."""

    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)
            db.execute(
                "CREATE INDEX IF NOT EXISTS graph_edges_source_idx"
                " ON graph_edges (source_id, edge_type)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS graph_edges_target_idx"
                " ON graph_edges (target_id, edge_type)"
            )

    # ------------------------------------------------------------------ #
    def connect(
        self,
        edge_type: str,
        source_id: str,
        target_id: str,
        *,
        provenance: tuple[str, ...] | list[str],
        confidence: float = 0.8,
        attributes: dict | None = None,
        valid_until: str | None = None,
    ) -> int:
        """One relationship onto the graph. Provenance is mandatory —
        an edge nobody can trace is a rumor, not a relation."""
        if not provenance:
            raise ValueError("an edge without provenance is a rumor")
        with self._conn.transaction() as db:
            cursor = db.execute(
                """INSERT INTO graph_edges
                   (edge_type, source_id, target_id, attributes,
                    valid_from, valid_until, confidence, provenance,
                    superseded_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                (
                    str(edge_type),
                    str(source_id),
                    str(target_id),
                    json.dumps(attributes or {}, ensure_ascii=False, default=str),
                    _now(),
                    valid_until,
                    max(0.0, min(1.0, float(confidence))),
                    json.dumps([str(p) for p in provenance]),
                ),
            )
            return int(cursor.lastrowid)

    def close(self, edge_id: int, *, superseded_by: int | None = None) -> None:
        """End an edge's validity now — history keeps the interval."""
        with self._conn.transaction() as db:
            db.execute(
                "UPDATE graph_edges SET valid_until = ?, superseded_by = ?"
                " WHERE id = ? AND valid_until IS NULL",
                (_now(), superseded_by, int(edge_id)),
            )

    # ------------------------------------------------------------------ #
    def neighbors(
        self,
        node_id: str,
        *,
        edge_types: tuple[str, ...] = (),
        at: str | None = None,
    ) -> list[dict]:
        """Edges touching one node, valid AT a moment (now by default) —
        closed and superseded edges are excluded by the query's shape."""
        moment = at or _now()
        with self._conn.transaction() as db:
            rows = db.execute(
                f"""SELECT id, edge_type, source_id, target_id, confidence
                    FROM graph_edges
                    WHERE (source_id = ? OR target_id = ?) AND {_VALID}
                    ORDER BY id DESC LIMIT 500""",
                (str(node_id), str(node_id), moment, moment),
            ).fetchall()
        found = [
            {
                "edge_id": int(r[0]),
                "edge_type": str(r[1]),
                "source_id": str(r[2]),
                "target_id": str(r[3]),
                "confidence": float(r[4]),
            }
            for r in rows
        ]
        if edge_types:
            found = [e for e in found if e["edge_type"] in edge_types]
        return found

    def neighborhood(
        self, node_id: str, *, hops: int = 1, at: str | None = None
    ) -> set[str]:
        """Every id within N valid hops — the proximity term retrieval
        adds to its score; never includes the seed itself."""
        seen = {str(node_id)}
        frontier = {str(node_id)}
        for _ in range(max(1, int(hops))):
            grown: set[str] = set()
            for nid in frontier:
                for edge in self.neighbors(nid, at=at):
                    grown.add(edge["source_id"])
                    grown.add(edge["target_id"])
            frontier = grown - seen
            seen |= grown
            if not frontier:
                break
        seen.discard(str(node_id))
        return seen

    def dependents_at(self, target_id: str, at: str) -> list[dict]:
        """The acceptance query: what depended on X when Y happened —
        one time-scoped select, no replay."""
        with self._conn.transaction() as db:
            rows = db.execute(
                f"""SELECT id, edge_type, source_id, confidence
                    FROM graph_edges
                    WHERE target_id = ?
                      AND edge_type IN ('depends_on', 'consumes')
                      AND {_VALID}
                    ORDER BY id DESC""",
                (str(target_id), at, at),
            ).fetchall()
        return [
            {
                "edge_id": int(r[0]),
                "edge_type": str(r[1]),
                "source_id": str(r[2]),
                "confidence": float(r[3]),
            }
            for r in rows
        ]
