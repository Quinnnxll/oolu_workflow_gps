"""PaveStore — the surveyed map, persisted so it is durably inspectable.

The map is a PROJECTION over the registry (the M1 law): the survey rebuilds
it every tick and REPLACES the tenant's stored webs wholesale, so a stale
web never lingers past the survey that stopped finding it. The store's only
job in W1 is to hold the latest survey per tenant so ``GET /v1/paver/webs``
reads a durable answer between ticks — not to be a second source of truth.
"""

from __future__ import annotations

import json

from .contracts import RouteWeb, SurveyReport

__all__ = ["PaveStore"]

_SCHEMA = """CREATE TABLE IF NOT EXISTS paver_webs (
    web_id TEXT NOT NULL,
    tenant TEXT NOT NULL,
    anchor TEXT,
    anchor_kind TEXT,
    web_json TEXT NOT NULL,
    surveyed_at TEXT NOT NULL,
    PRIMARY KEY (tenant, web_id)
)"""


class PaveStore:
    """The tenant's surveyed webs on the shared durable connection."""

    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def replace_tenant(self, report: SurveyReport, *, now) -> None:
        """Swap the tenant's whole map for this survey's — one transaction,
        so a reader never sees a half-replaced map. The survey is the
        authority; the store just remembers its latest word."""
        stamp = now.isoformat() if hasattr(now, "isoformat") else str(now)
        with self._conn.transaction() as db:
            db.execute(
                "DELETE FROM paver_webs WHERE tenant = ?", (report.tenant,)
            )
            for web in report.webs:
                db.execute(
                    """INSERT INTO paver_webs
                         (web_id, tenant, anchor, anchor_kind, web_json,
                          surveyed_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        web.web_id,
                        report.tenant,
                        web.anchor,
                        web.anchor_kind,
                        web.model_dump_json(),
                        stamp,
                    ),
                )

    def webs(self, tenant: str, *, anchor: str | None = None) -> list[RouteWeb]:
        """The tenant's stored webs, newest survey — optionally only those
        anchored at one node (the trigger-door view)."""
        with self._conn.lock:
            if anchor is None:
                rows = self._conn.db.execute(
                    "SELECT web_json FROM paver_webs WHERE tenant = ? "
                    "ORDER BY web_id",
                    (tenant,),
                ).fetchall()
            else:
                rows = self._conn.db.execute(
                    "SELECT web_json FROM paver_webs "
                    "WHERE tenant = ? AND anchor = ? ORDER BY web_id",
                    (tenant, anchor),
                ).fetchall()
        return [RouteWeb.model_validate_json(row["web_json"]) for row in rows]

    def web(self, tenant: str, web_id: str) -> RouteWeb | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT web_json FROM paver_webs WHERE tenant = ? AND web_id = ?",
                (tenant, web_id),
            ).fetchone()
        return RouteWeb.model_validate_json(row["web_json"]) if row else None
