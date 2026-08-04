"""The tenant SOP store (P1) — the human's standing law, durably kept.

``_paver_sops`` was the ONE seam a tenant SOP store was named to plug
into (algorithm-barriers-plan, W5): until now it returned ``[]`` and
production webs paved gate-free. This store closes that gap: a human
authors an SOP in the SAME strict YAML schema :func:`parse_sop`
compiles (a typo'd rule refuses at the door, never stores a weaker
law), the rows live per tenant, and the Paver's gate projection reads
them back as typed :class:`StandardOperatingProcedure` objects on
every pave.

The laws:

- **Only the human authors.** The door writes what a verified session
  submitted; no model, no desk, no observer ever writes a row here —
  models propose in words elsewhere, the owner disposes here.
- **Refusal at the door.** A document that does not parse under the
  strict schema is refused with the parser's own words; nothing
  half-valid is stored.
- **Whole replacement by name.** Re-authoring an SOP under the same
  name replaces it entirely — never a silent merge of old and new
  rules.
- **A stored row that no longer parses is skipped, named.** ``list``
  degrades per-row (the schema may tighten across versions); a broken
  row never silently weakens into a partial SOP.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Callable

from .sop import StandardOperatingProcedure, parse_sop

__all__ = ["TenantSopStore"]

_SCHEMA = """CREATE TABLE IF NOT EXISTS tenant_sops (
    tenant_id TEXT NOT NULL,
    name TEXT NOT NULL,
    yaml_text TEXT NOT NULL,
    authored_by TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, name)
)"""


class TenantSopStore:
    """Human-authored SOPs, one row per (tenant, name)."""

    def __init__(
        self, conn, *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def put(
        self, *, tenant: str, text: str, authored_by: str = ""
    ) -> StandardOperatingProcedure:
        """Store one SOP document — parsed STRICTLY first, so the door
        refuses a broken law instead of keeping it. Returns the parsed
        procedure; re-authoring the same name replaces whole."""
        sop = parse_sop(text)  # raises with the parser's own words
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO tenant_sops
                     (tenant_id, name, yaml_text, authored_by, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(tenant_id, name) DO UPDATE SET
                     yaml_text = excluded.yaml_text,
                     authored_by = excluded.authored_by,
                     updated_at = excluded.updated_at""",
                (
                    tenant,
                    sop.name,
                    text,
                    authored_by,
                    self._clock().isoformat(),
                ),
            )
        return sop

    def list(self, tenant: str) -> list[StandardOperatingProcedure]:
        """The tenant's standing SOPs, typed — what the Paver's gate
        projection compiles. A stored row that no longer parses is
        skipped (the schema may have tightened); it still shows in
        :meth:`rows` so the author can see and fix it."""
        out: list[StandardOperatingProcedure] = []
        for row in self.rows(tenant):
            try:
                out.append(parse_sop(row["yaml_text"]))
            except Exception:  # noqa: BLE001 - a broken row never weakens a law
                continue
        return out

    def rows(self, tenant: str) -> list[dict]:
        """The raw authored rows — the door's read view, parse or not."""
        with self._conn.lock:
            rows = self._conn.db.execute(
                """SELECT name, yaml_text, authored_by, updated_at
                   FROM tenant_sops WHERE tenant_id = ? ORDER BY name""",
                (tenant,),
            ).fetchall()
        return [
            {
                "name": str(row["name"]),
                "yaml_text": str(row["yaml_text"]),
                "authored_by": str(row["authored_by"]),
                "updated_at": str(row["updated_at"]),
            }
            for row in rows
        ]

    def delete(self, *, tenant: str, name: str) -> bool:
        with self._conn.transaction() as db:
            cursor = db.execute(
                "DELETE FROM tenant_sops WHERE tenant_id = ? AND name = ?",
                (tenant, name),
            )
        return bool(getattr(cursor, "rowcount", 0) or 0)
