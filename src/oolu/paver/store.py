"""PaveStore — the surveyed map, persisted so it is durably inspectable.

The map is a PROJECTION over the registry (the M1 law): the survey rebuilds
it every tick and REPLACES the tenant's stored webs wholesale, so a stale
web never lingers past the survey that stopped finding it. The store's only
job in W1 is to hold the latest survey per tenant so ``GET /v1/paver/webs``
reads a durable answer between ticks — not to be a second source of truth.
"""

from __future__ import annotations

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

# The promoted contract of a PAVED web (W2): stored so a trigger (W4) can
# execute the exact SubgraphBody the Paver verified, deterministically,
# without re-deriving it. Keyed by web — one paved contract per web.
# ``owner_principal`` (P4) is the principal the promoted node was
# contributed under; ``anchor`` (P2) lets a promotion find and retire a
# superseded predecessor at the same trigger door.
_PAVED_SCHEMA = """CREATE TABLE IF NOT EXISTS paver_paved (
    web_id TEXT NOT NULL,
    tenant TEXT NOT NULL,
    node_id TEXT NOT NULL,
    version_id TEXT,
    contract_json TEXT NOT NULL,
    signature TEXT NOT NULL DEFAULT '',
    paved_at TEXT NOT NULL,
    owner_principal TEXT NOT NULL DEFAULT '',
    anchor TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (tenant, web_id)
)"""


class PaveStore:
    """The tenant's surveyed webs on the shared durable connection."""

    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)
            db.execute(_PAVED_SCHEMA)
        self._migrate_paved_columns()

    def _migrate_paved_columns(self) -> None:
        # Tables born before P2/P4 lack the owner and anchor columns; old
        # promotions read as ownerless and anchorless — retired only by
        # the same-web replacement path, never a false anchor sweep.
        try:
            with self._conn.transaction() as db:
                db.execute("SELECT owner_principal FROM paver_paved LIMIT 1")
            return
        except Exception:
            pass
        with self._conn.transaction() as db:
            db.execute(
                "ALTER TABLE paver_paved"
                " ADD COLUMN owner_principal TEXT NOT NULL DEFAULT ''"
            )
            db.execute(
                "ALTER TABLE paver_paved"
                " ADD COLUMN anchor TEXT NOT NULL DEFAULT ''"
            )

    def replace_tenant(self, report: SurveyReport, *, now) -> None:
        """Swap the tenant's whole SURVEY map for this survey's — one
        transaction, so a reader never sees a half-replaced map. The paved
        table is left alone: a promotion is durable truth, not a projection
        the next survey overwrites."""
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

    def record_paved(
        self,
        tenant: str,
        web_id: str,
        *,
        node_id: str,
        version_id: str | None,
        contract_json: str,
        signature: str = "",
        owner_principal: str = "",
        anchor: str = "",
        now,
    ) -> None:
        """Persist a web's promoted contract — the exact SubgraphBody a
        trigger will fire — plus its content ``signature``, so the next
        tick skips an UNCHANGED web (idempotence) but re-paves one whose
        shape moved. Replaces any prior promotion of the same web."""
        stamp = now.isoformat() if hasattr(now, "isoformat") else str(now)
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO paver_paved
                     (web_id, tenant, node_id, version_id, contract_json,
                      signature, paved_at, owner_principal, anchor)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(tenant, web_id) DO UPDATE SET
                     node_id = excluded.node_id,
                     version_id = excluded.version_id,
                     contract_json = excluded.contract_json,
                     signature = excluded.signature,
                     paved_at = excluded.paved_at,
                     owner_principal = excluded.owner_principal,
                     anchor = excluded.anchor""",
                (
                    web_id,
                    tenant,
                    node_id,
                    version_id,
                    contract_json,
                    signature,
                    stamp,
                    owner_principal,
                    anchor,
                ),
            )

    def retire_paved(self, tenant: str, web_id: str) -> bool:
        """Drop a SUPERSEDED promotion (P2) — called by the promotion that
        replaces it, after the registry node is revoked. A retired web can
        no longer fire (nothing deterministic remains to run)."""
        with self._conn.transaction() as db:
            cursor = db.execute(
                "DELETE FROM paver_paved WHERE tenant = ? AND web_id = ?",
                (tenant, web_id),
            )
        return bool(getattr(cursor, "rowcount", 0) or 0)

    def paved(self, tenant: str, web_id: str) -> dict | None:
        """A web's promoted contract row, or None if it was never paved."""
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT node_id, version_id, contract_json, signature, "
                "paved_at, owner_principal, anchor "
                "FROM paver_paved WHERE tenant = ? AND web_id = ?",
                (tenant, web_id),
            ).fetchone()
        return self._paved_row(row) if row is not None else None

    def paved_at_anchor(self, tenant: str, anchor: str) -> list[dict]:
        """Every promotion standing at one trigger door (P2) — the set a
        NEW promotion at that door supersedes. ``web_id`` rides each row
        so the caller can spare itself."""
        if not anchor:
            return []
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT web_id, node_id, version_id, contract_json, "
                "signature, paved_at, owner_principal, anchor "
                "FROM paver_paved WHERE tenant = ? AND anchor = ? "
                "ORDER BY web_id",
                (tenant, anchor),
            ).fetchall()
        return [
            {**self._paved_row(row), "web_id": row["web_id"]} for row in rows
        ]

    def owner_webs(self, tenant: str, owner_principal: str) -> list[str]:
        """The web ids promoted under one principal (P4) — the standing
        property an agent's own surfaces consult ("does MY web stand?")."""
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT web_id FROM paver_paved"
                " WHERE tenant = ? AND owner_principal = ? ORDER BY web_id",
                (tenant, owner_principal),
            ).fetchall()
        return [str(row["web_id"]) for row in rows]

    def promoted_node_ids(self, tenant: str) -> set[str]:
        """The registry node ids the Paver's promotions minted — excluded
        from the survey whoever owns them (P4: an agent-owned web node is
        still the Paver's PRODUCT, never its raw material)."""
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT node_id FROM paver_paved WHERE tenant = ?",
                (tenant,),
            ).fetchall()
        return {str(row["node_id"]) for row in rows}

    @staticmethod
    def _paved_row(row) -> dict:
        return {
            "node_id": row["node_id"],
            "version_id": row["version_id"],
            "contract_json": row["contract_json"],
            "signature": row["signature"],
            "paved_at": row["paved_at"],
            "owner_principal": str(row["owner_principal"] or ""),
            "anchor": str(row["anchor"] or ""),
        }

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
