"""The method-version lifecycle — only an approved version authorizes.

Method versions move ``developing → validated → approved`` and end as
``superseded`` or ``withdrawn``; the transitions are a whitelist, so a
developing method cannot jump to approved past validation, and nothing
ever moves *out* of a terminal status. ``authorized`` is the one
question the test-job registry asks, and the answer is a status check,
never a default.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict

from ..durable import DurableConnection


class MethodStatus(str, Enum):
    DEVELOPING = "developing"
    VALIDATED = "validated"
    APPROVED = "approved"
    SUPERSEDED = "superseded"
    WITHDRAWN = "withdrawn"


_LEGAL: dict[MethodStatus, frozenset[MethodStatus]] = {
    MethodStatus.DEVELOPING: frozenset(
        {MethodStatus.VALIDATED, MethodStatus.WITHDRAWN}
    ),
    MethodStatus.VALIDATED: frozenset(
        {MethodStatus.APPROVED, MethodStatus.DEVELOPING, MethodStatus.WITHDRAWN}
    ),
    MethodStatus.APPROVED: frozenset({MethodStatus.SUPERSEDED, MethodStatus.WITHDRAWN}),
    MethodStatus.SUPERSEDED: frozenset(),
    MethodStatus.WITHDRAWN: frozenset(),
}

_SCHEMA = """CREATE TABLE IF NOT EXISTS protocol_method_versions (
    method_id TEXT NOT NULL,
    version TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (method_id, version)
)"""


class MethodVersion(BaseModel):
    model_config = ConfigDict(frozen=True)

    method_id: str
    version: str
    title: str
    status: MethodStatus
    updated_at: int


class MethodRegistry:
    def __init__(self, conn: DurableConnection) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def publish(
        self, *, method_id: str, version: str, title: str, now: int
    ) -> MethodVersion:
        with self._conn.transaction() as db:
            db.execute(
                "INSERT INTO protocol_method_versions VALUES (?, ?, ?, ?, ?)",
                (method_id, version, title, MethodStatus.DEVELOPING.value, now),
            )
        return MethodVersion(
            method_id=method_id,
            version=version,
            title=title,
            status=MethodStatus.DEVELOPING,
            updated_at=now,
        )

    def set_status(
        self, *, method_id: str, version: str, status: MethodStatus, now: int
    ) -> None:
        with self._conn.transaction() as db:
            row = db.execute(
                "SELECT status FROM protocol_method_versions"
                " WHERE method_id = ? AND version = ?",
                (method_id, version),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown method version {method_id}@{version}")
            current = MethodStatus(row["status"])
            if status not in _LEGAL[current]:
                raise ValueError(
                    f"illegal method transition {current.value} -> {status.value}"
                )
            db.execute(
                "UPDATE protocol_method_versions SET status = ?, updated_at = ?"
                " WHERE method_id = ? AND version = ?",
                (status.value, now, method_id, version),
            )

    def get(self, method_id: str, version: str) -> MethodVersion | None:
        with self._conn.transaction() as db:
            row = db.execute(
                "SELECT * FROM protocol_method_versions"
                " WHERE method_id = ? AND version = ?",
                (method_id, version),
            ).fetchone()
        if row is None:
            return None
        return MethodVersion(
            method_id=row["method_id"],
            version=row["version"],
            title=row["title"],
            status=MethodStatus(row["status"]),
            updated_at=row["updated_at"],
        )

    def authorized(self, method_id: str, version: str) -> bool:
        record = self.get(method_id, version)
        return record is not None and record.status is MethodStatus.APPROVED
