from __future__ import annotations

from .models import MeteringEvent

_SCHEMA = """CREATE TABLE IF NOT EXISTS metering_events (
    event_id TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE NOT NULL,
    run_id TEXT NOT NULL,
    version_id TEXT,
    consumer_tenant TEXT,
    consumer_principal TEXT,
    outcome TEXT NOT NULL,
    audit_seq INTEGER NOT NULL,
    occurred_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
)"""


class MeteringLedger:
    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def record(self, event: MeteringEvent) -> bool:
        with self._conn.transaction() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO metering_events
                   (event_id, idempotency_key, run_id, version_id, consumer_tenant,
                    consumer_principal, outcome, audit_seq, occurred_at, payload_json,
                    recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event.event_id,
                    event.idempotency_key,
                    event.run_id,
                    event.version_id,
                    event.consumer_tenant,
                    event.consumer_principal,
                    event.outcome,
                    event.audit_seq,
                    event.occurred_at.isoformat(),
                    event.model_dump_json(),
                    event.recorded_at.isoformat(),
                ),
            )
            return cursor.rowcount > 0

    def get(self, idempotency_key: str) -> MeteringEvent | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json FROM metering_events WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        return MeteringEvent.model_validate_json(row["payload_json"])

    def verified_run(self, version_id: str, consumer_principal: str) -> bool:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT 1 FROM metering_events"
                " WHERE version_id = ? AND consumer_principal = ? LIMIT 1",
                (version_id, consumer_principal),
            ).fetchone()
        return row is not None

    def events(self) -> list[MeteringEvent]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT payload_json FROM metering_events ORDER BY audit_seq ASC"
            ).fetchall()
        return [MeteringEvent.model_validate_json(row["payload_json"]) for row in rows]
