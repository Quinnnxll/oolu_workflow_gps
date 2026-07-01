from __future__ import annotations

from .models import AttributionRecord, RunBinding

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS run_bindings (
        run_id TEXT PRIMARY KEY,
        version_id TEXT NOT NULL,
        consumer_tenant TEXT NOT NULL,
        payload_json TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS attribution_records (
        event_id TEXT NOT NULL,
        noder_principal TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        PRIMARY KEY (event_id, noder_principal)
    )""",
)


class AttributionStore:
    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            for statement in _SCHEMA:
                db.execute(statement)

    def bind(self, binding: RunBinding) -> bool:
        with self._conn.transaction() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO run_bindings
                   (run_id, version_id, consumer_tenant, payload_json)
                   VALUES (?, ?, ?, ?)""",
                (
                    binding.run_id,
                    binding.version_id,
                    binding.consumer_tenant,
                    binding.model_dump_json(),
                ),
            )
            return cursor.rowcount > 0

    def get_binding(self, run_id: str) -> RunBinding | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json FROM run_bindings WHERE run_id = ?", (run_id,)
            ).fetchone()
        return RunBinding.model_validate_json(row["payload_json"]) if row else None

    def add_attribution(self, record: AttributionRecord) -> bool:
        with self._conn.transaction() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO attribution_records
                   (event_id, noder_principal, payload_json)
                   VALUES (?, ?, ?)""",
                (record.event_id, record.noder_principal, record.model_dump_json()),
            )
            return cursor.rowcount > 0

    def attributions(self, event_id: str) -> list[AttributionRecord]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT payload_json FROM attribution_records WHERE event_id = ?"
                " ORDER BY noder_principal ASC",
                (event_id,),
            ).fetchall()
        return [AttributionRecord.model_validate_json(row["payload_json"]) for row in rows]
