"""Inventory (M2): atomic reservations, expiry, oversell prevention.

Stock is a counter that only guarded SQL touches. A reservation is one
UPDATE with the availability check in its WHERE clause — under any
concurrency, the database serializes the decrement and exactly one of two
buyers gets the last unit; the other's UPDATE matches zero rows and is
refused. The pulse's claim discipline, applied to stock.

A reservation's life: RESERVED (stock decremented, clock running) →
COMMITTED (the sale happened; stock stays gone) or RELEASED (cancelled,
declined, or expired; stock returns). Expiry is lazy — every reserve and
availability read sweeps ripe reservations first, so stock never rots in a
dead cart. Listings not seeded here are simply not inventory-tracked (an
external seller's stock is their own problem).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

_STOCK_SCHEMA = """CREATE TABLE IF NOT EXISTS market_stock (
    listing_id TEXT PRIMARY KEY,
    available INTEGER NOT NULL
)"""

_RESERVATIONS_SCHEMA = """CREATE TABLE IF NOT EXISTS market_reservations (
    reservation_id TEXT PRIMARY KEY,
    listing_id TEXT NOT NULL,
    holder TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    state TEXT NOT NULL,
    expires_at TEXT NOT NULL
)"""

DEFAULT_RESERVATION_MINUTES = 30


class Reservation(BaseModel):
    model_config = ConfigDict(frozen=True)

    reservation_id: str
    listing_id: str
    holder: str  # the intent or order the units are held for
    quantity: int = Field(gt=0)
    state: str  # reserved | committed | released
    expires_at: datetime


class InventoryService:
    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_STOCK_SCHEMA)
            db.execute(_RESERVATIONS_SCHEMA)

    # ------------------------------------------------------------------ #
    # Stock.                                                              #
    # ------------------------------------------------------------------ #
    def seed(self, listing_id: str, quantity: int) -> None:
        """Set a listing's stock (publication, restock). Absolute, not
        additive — the seller states what is on the shelf."""
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO market_stock (listing_id, available)
                   VALUES (?, ?)
                   ON CONFLICT(listing_id) DO UPDATE SET
                     available = excluded.available""",
                (listing_id, max(0, quantity)),
            )

    def tracked(self, listing_id: str) -> bool:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT 1 FROM market_stock WHERE listing_id = ?", (listing_id,)
            ).fetchone()
        return row is not None

    def available(self, listing_id: str, *, now: datetime) -> int | None:
        """Live availability (after sweeping ripe holds); None = untracked."""
        self.sweep_expired(now)
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT available FROM market_stock WHERE listing_id = ?",
                (listing_id,),
            ).fetchone()
        return None if row is None else int(row["available"])

    # ------------------------------------------------------------------ #
    # Reservations.                                                       #
    # ------------------------------------------------------------------ #
    def reserve(
        self,
        listing_id: str,
        *,
        quantity: int,
        holder: str,
        now: datetime,
        ttl_minutes: int = DEFAULT_RESERVATION_MINUTES,
    ) -> Reservation | None:
        """Atomically hold ``quantity`` units, or None when stock cannot
        cover it. The WHERE clause is the whole oversell prevention."""
        if quantity < 1:
            return None
        self.sweep_expired(now)
        record = Reservation(
            reservation_id=uuid4().hex,
            listing_id=listing_id,
            holder=holder,
            quantity=quantity,
            state="reserved",
            expires_at=now + timedelta(minutes=ttl_minutes),
        )
        with self._conn.transaction() as db:
            cursor = db.execute(
                "UPDATE market_stock SET available = available - ?"
                " WHERE listing_id = ? AND available >= ?",
                (quantity, listing_id, quantity),
            )
            if cursor.rowcount == 0:
                return None
            db.execute(
                "INSERT INTO market_reservations"
                " (reservation_id, listing_id, holder, quantity, state,"
                "  expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    record.reservation_id,
                    listing_id,
                    holder,
                    quantity,
                    "reserved",
                    record.expires_at.isoformat(),
                ),
            )
        return record

    def for_holder(self, holder: str) -> Reservation | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT * FROM market_reservations"
                " WHERE holder = ? AND state = 'reserved'",
                (holder,),
            ).fetchone()
        return None if row is None else self._row(row)

    def commit(self, reservation_id: str) -> bool:
        """The sale happened: the held units are gone for good."""
        with self._conn.transaction() as db:
            cursor = db.execute(
                "UPDATE market_reservations SET state = 'committed'"
                " WHERE reservation_id = ? AND state = 'reserved'",
                (reservation_id,),
            )
            return cursor.rowcount > 0

    def release(self, reservation_id: str) -> bool:
        """The hold ends without a sale: stock returns, exactly once —
        the guarded state flip decides, so a release racing an expiry
        sweep can never double-credit the shelf."""
        with self._conn.transaction() as db:
            row = db.execute(
                "SELECT listing_id, quantity FROM market_reservations"
                " WHERE reservation_id = ? AND state = 'reserved'",
                (reservation_id,),
            ).fetchone()
            if row is None:
                return False
            cursor = db.execute(
                "UPDATE market_reservations SET state = 'released'"
                " WHERE reservation_id = ? AND state = 'reserved'",
                (reservation_id,),
            )
            if cursor.rowcount == 0:
                return False
            db.execute(
                "UPDATE market_stock SET available = available + ?"
                " WHERE listing_id = ?",
                (row["quantity"], row["listing_id"]),
            )
        return True

    def sweep_expired(self, now: datetime) -> int:
        """Release every ripe reservation. Lazy: reads and reserves call
        this first, so dead carts free their stock on the next touch."""
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT reservation_id FROM market_reservations"
                " WHERE state = 'reserved' AND expires_at <= ?",
                (now.isoformat(),),
            ).fetchall()
        released = 0
        for row in rows:
            if self.release(row["reservation_id"]):
                released += 1
        return released

    @staticmethod
    def _row(row) -> Reservation:
        return Reservation(
            reservation_id=row["reservation_id"],
            listing_id=row["listing_id"],
            holder=row["holder"],
            quantity=row["quantity"],
            state=row["state"],
            expires_at=datetime.fromisoformat(row["expires_at"]),
        )
