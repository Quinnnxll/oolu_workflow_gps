from __future__ import annotations

from typing import Sequence

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

# The stats and earnings paths ask "events for THIS version / run", and an
# answer must not cost a walk of everything ever metered.
_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_metering_events_version"
    " ON metering_events(version_id)",
    "CREATE INDEX IF NOT EXISTS idx_metering_events_run"
    " ON metering_events(run_id)",
)

# SQLite's default parameter ceiling is 999; IN lists chunk under it.
_CHUNK = 500


class MeteringLedger:
    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)
            for statement in _INDEXES:
                db.execute(statement)

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
        """Did this consumer have a verified SUCCESSFUL run of the version?
        Failed evidence lives in the same ledger now, and a failed run
        must never unlock rating — the filter keeps that invariant."""
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT 1 FROM metering_events"
                " WHERE version_id = ? AND consumer_principal = ?"
                " AND outcome = 'succeeded' LIMIT 1",
                (version_id, consumer_principal),
            ).fetchone()
        return row is not None

    def events(self) -> list[MeteringEvent]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT payload_json FROM metering_events ORDER BY audit_seq ASC"
            ).fetchall()
        return [MeteringEvent.model_validate_json(row["payload_json"]) for row in rows]

    def get_by_event_id(self, event_id: str) -> MeteringEvent | None:
        """One event by its id — the billing-entry join, as a key lookup
        instead of materializing the whole ledger per question."""
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json FROM metering_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        return (
            MeteringEvent.model_validate_json(row["payload_json"]) if row else None
        )

    def events_for_version(
        self, version_id: str, run_ids: Sequence[str] = ()
    ) -> list[MeteringEvent]:
        """Every event touching one version: recorded against it directly,
        or belonging to a run the caller knows the version participated in
        (the attribution store's participation index). Indexed both ways —
        the cost follows the version's own history, never the ledger's."""
        seen: dict[str, MeteringEvent] = {}
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT payload_json FROM metering_events WHERE version_id = ?",
                (version_id,),
            ).fetchall()
            ids = list(dict.fromkeys(run_ids))
            for start in range(0, len(ids), _CHUNK):
                chunk = ids[start : start + _CHUNK]
                marks = ",".join("?" for _ in chunk)
                rows += self._conn.db.execute(
                    "SELECT payload_json FROM metering_events"
                    f" WHERE run_id IN ({marks})",
                    tuple(chunk),
                ).fetchall()
        for row in rows:
            event = MeteringEvent.model_validate_json(row["payload_json"])
            seen[event.event_id] = event
        return sorted(seen.values(), key=lambda e: e.audit_seq)
