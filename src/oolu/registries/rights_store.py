"""R1's rights contract, made durable and revocable.

The seam promised in ``oolu.evidence.rights`` closes here: the same
frozen ``EvidenceRights`` model, the same ``require_right`` gate, now
persisted on the durable connection with a revocation timestamp beside
it. Revocation follows the house rule — a status with a time, never a
deletion — so "was this use permitted when it happened" stays
answerable forever, while every check made after the revocation refuses
by name.
"""

from __future__ import annotations

from ..durable import DurableConnection
from ..evidence.rights import EvidenceRights, RightsError, require_right

_SCHEMA = """CREATE TABLE IF NOT EXISTS protocol_evidence_rights (
    rights_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    revoked_at INTEGER,
    saved_at INTEGER NOT NULL
)"""


class RightsStore:
    def __init__(self, conn: DurableConnection) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def save(self, rights: EvidenceRights, *, now: int) -> None:
        with self._conn.transaction() as db:
            existing = db.execute(
                "SELECT rights_id FROM protocol_evidence_rights WHERE rights_id = ?",
                (rights.rights_id,),
            ).fetchone()
            if existing is not None:
                raise ValueError(
                    f"rights {rights.rights_id!r} already saved; a changed "
                    "grant is a new rights contract"
                )
            db.execute(
                "INSERT INTO protocol_evidence_rights VALUES (?, ?, NULL, ?)",
                (rights.rights_id, rights.model_dump_json(), now),
            )

    def get(self, rights_id: str) -> EvidenceRights | None:
        with self._conn.transaction() as db:
            row = db.execute(
                "SELECT payload_json FROM protocol_evidence_rights WHERE rights_id = ?",
                (rights_id,),
            ).fetchone()
        if row is None:
            return None
        return EvidenceRights.model_validate_json(row["payload_json"])

    def revoke(self, rights_id: str, *, at: int) -> None:
        with self._conn.transaction() as db:
            cursor = db.execute(
                "UPDATE protocol_evidence_rights SET revoked_at = ?"
                " WHERE rights_id = ? AND revoked_at IS NULL",
                (at, rights_id),
            )
            if not cursor.rowcount:
                raise KeyError(f"unknown or already revoked rights {rights_id!r}")

    def require(
        self, rights_id: str, permission: str, *, evidence_id: str, now: int
    ) -> None:
        """The durable gate: load, check revocation, then R1's checks."""

        with self._conn.transaction() as db:
            row = db.execute(
                "SELECT payload_json, revoked_at FROM protocol_evidence_rights"
                " WHERE rights_id = ?",
                (rights_id,),
            ).fetchone()
        if row is None:
            raise RightsError(f"unknown_rights:{rights_id}")
        if row["revoked_at"] is not None and now >= row["revoked_at"]:
            raise RightsError(f"rights_revoked:{rights_id}")
        rights = EvidenceRights.model_validate_json(row["payload_json"])
        require_right(rights, permission, evidence_id=evidence_id, now=now)
