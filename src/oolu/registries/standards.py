"""The standards registry — editions with status, never years in prose.

The build plan's own standards table cited editions that do not exist;
its own rule ("a deployment cannot assume that a standard remains
current") is enforced here by making currency a *query*. Documents and
methods cite ``(standard_id, edition)`` pairs; registering a newer
edition supersedes the old one in the same transaction, and withdrawal
is a status, not a deletion — a method validated against a withdrawn
edition still names exactly what it was validated against.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from ..durable import DurableConnection

_SCHEMA = """CREATE TABLE IF NOT EXISTS protocol_standards (
    standard_id TEXT NOT NULL,
    edition TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    registered_at INTEGER NOT NULL,
    superseded_by TEXT,
    PRIMARY KEY (standard_id, edition)
)"""


class StandardEdition(BaseModel):
    model_config = ConfigDict(frozen=True)

    standard_id: str
    edition: str
    title: str
    status: str  # current | superseded | withdrawn
    registered_at: int
    superseded_by: str | None = None


def _row_to_edition(row) -> StandardEdition:
    return StandardEdition(
        standard_id=row["standard_id"],
        edition=row["edition"],
        title=row["title"],
        status=row["status"],
        registered_at=row["registered_at"],
        superseded_by=row["superseded_by"],
    )


class StandardsRegistry:
    def __init__(self, conn: DurableConnection) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def register(
        self, *, standard_id: str, edition: str, title: str, now: int
    ) -> StandardEdition:
        """Register an edition as current, superseding any prior current."""

        with self._conn.transaction() as db:
            existing = db.execute(
                "SELECT edition FROM protocol_standards"
                " WHERE standard_id = ? AND edition = ?",
                (standard_id, edition),
            ).fetchone()
            if existing is not None:
                raise ValueError(
                    f"{standard_id} {edition} is already registered; "
                    "editions are immutable"
                )
            db.execute(
                "UPDATE protocol_standards SET status = 'superseded',"
                " superseded_by = ? WHERE standard_id = ? AND status = 'current'",
                (edition, standard_id),
            )
            db.execute(
                "INSERT INTO protocol_standards"
                " (standard_id, edition, title, status, registered_at)"
                " VALUES (?, ?, ?, 'current', ?)",
                (standard_id, edition, title, now),
            )
        return StandardEdition(
            standard_id=standard_id,
            edition=edition,
            title=title,
            status="current",
            registered_at=now,
        )

    def withdraw(self, *, standard_id: str, edition: str) -> None:
        with self._conn.transaction() as db:
            cursor = db.execute(
                "UPDATE protocol_standards SET status = 'withdrawn'"
                " WHERE standard_id = ? AND edition = ?",
                (standard_id, edition),
            )
            if not cursor.rowcount:
                raise KeyError(f"unknown edition {standard_id} {edition}")

    def current(self, standard_id: str) -> StandardEdition | None:
        with self._conn.transaction() as db:
            row = db.execute(
                "SELECT * FROM protocol_standards"
                " WHERE standard_id = ? AND status = 'current'",
                (standard_id,),
            ).fetchone()
        return None if row is None else _row_to_edition(row)

    def get(self, standard_id: str, edition: str) -> StandardEdition | None:
        with self._conn.transaction() as db:
            row = db.execute(
                "SELECT * FROM protocol_standards"
                " WHERE standard_id = ? AND edition = ?",
                (standard_id, edition),
            ).fetchone()
        return None if row is None else _row_to_edition(row)
