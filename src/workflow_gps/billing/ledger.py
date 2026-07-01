from __future__ import annotations

from datetime import UTC, datetime

from .models import EarningsEntry, EarningsKind, NoderBalance

_SCHEMA = """CREATE TABLE IF NOT EXISTS earnings_entries (
    entry_id TEXT PRIMARY KEY,
    noder_principal TEXT NOT NULL,
    event_id TEXT,
    amount_micros INTEGER NOT NULL,
    kind TEXT NOT NULL,
    available_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (event_id, noder_principal, kind)
)"""


class EarningsLedger:
    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def append(self, entry: EarningsEntry) -> bool:
        with self._conn.transaction() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO earnings_entries
                   (entry_id, noder_principal, event_id, amount_micros, kind,
                    available_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry.entry_id,
                    entry.noder_principal,
                    entry.event_id,
                    entry.amount_micros,
                    entry.kind.value,
                    entry.available_at.isoformat(),
                    entry.created_at.isoformat(),
                ),
            )
            return cursor.rowcount > 0

    def entries(self, noder_principal: str) -> list[EarningsEntry]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT entry_id, noder_principal, event_id, amount_micros, kind,"
                " available_at, created_at FROM earnings_entries"
                " WHERE noder_principal = ? ORDER BY created_at ASC",
                (noder_principal,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def entries_for_event(self, event_id: str) -> list[EarningsEntry]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT entry_id, noder_principal, event_id, amount_micros, kind,"
                " available_at, created_at FROM earnings_entries"
                " WHERE event_id = ? ORDER BY created_at ASC",
                (event_id,),
            ).fetchall()
        return [self._row(row) for row in rows]

    @staticmethod
    def _row(row) -> EarningsEntry:
        return EarningsEntry(
            entry_id=row["entry_id"],
            noder_principal=row["noder_principal"],
            event_id=row["event_id"],
            amount_micros=row["amount_micros"],
            kind=EarningsKind(row["kind"]),
            available_at=datetime.fromisoformat(row["available_at"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )


class BalanceProjection:
    def __init__(self, ledger: EarningsLedger) -> None:
        self._ledger = ledger

    def balance(
        self, noder_principal: str, *, now: datetime | None = None
    ) -> NoderBalance:
        moment = now or datetime.now(UTC)
        earned = pending = reserved = paid = 0
        for entry in self._ledger.entries(noder_principal):
            if entry.kind == EarningsKind.RESERVE:
                reserved += entry.amount_micros
            elif entry.kind == EarningsKind.PAYOUT:
                paid += -entry.amount_micros
            elif entry.available_at <= moment:
                earned += entry.amount_micros
            else:
                pending += entry.amount_micros
        return NoderBalance(
            noder_principal=noder_principal,
            available_micros=earned - reserved - paid,
            pending_micros=pending,
            reserved_micros=reserved,
            lifetime_paid_micros=paid,
        )
