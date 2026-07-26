"""The milestone book (M3): a service order's payment schedule, durably.

Each milestone is one tranche of the offer's subtotal with its own little
life: PENDING → DELIVERED (evidence aboard) → RELEASED, or FAILED — and a
failure freezes every unreleased sibling. The book only records; the order
machine decides, captures, and releases, so money arithmetic stays in one
place.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

_SCHEMA = """CREATE TABLE IF NOT EXISTS market_milestones (
    order_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    title TEXT NOT NULL,
    amount_micros INTEGER NOT NULL,
    state TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (order_id, idx)
)"""


class MilestoneState(BaseModel):
    model_config = ConfigDict(frozen=True)

    order_id: str
    index: int = Field(ge=0)
    title: str
    amount_micros: int = Field(gt=0)
    state: str  # pending | delivered | released | failed
    evidence: str = ""


class MilestoneBook:
    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def seed(
        self, order_id: str, milestones: tuple[tuple[str, int], ...]
    ) -> None:
        """Write the approved schedule once, at placement. Idempotent —
        the (order, index) key refuses a second seeding."""
        with self._conn.transaction() as db:
            for index, (title, amount) in enumerate(milestones):
                db.execute(
                    "INSERT OR IGNORE INTO market_milestones"
                    " (order_id, idx, title, amount_micros, state)"
                    " VALUES (?, ?, ?, ?, 'pending')",
                    (order_id, index, title, amount),
                )

    def for_order(self, order_id: str) -> list[MilestoneState]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT * FROM market_milestones WHERE order_id = ?"
                " ORDER BY idx",
                (order_id,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def get(self, order_id: str, index: int) -> MilestoneState | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT * FROM market_milestones"
                " WHERE order_id = ? AND idx = ?",
                (order_id, index),
            ).fetchone()
        return None if row is None else self._row(row)

    def set_state(
        self,
        order_id: str,
        index: int,
        *,
        state: str,
        expected: tuple[str, ...],
        evidence: str | None = None,
    ) -> bool:
        marks = ",".join("?" for _ in expected)
        with self._conn.transaction() as db:
            if evidence is None:
                cursor = db.execute(
                    "UPDATE market_milestones SET state = ?"
                    f" WHERE order_id = ? AND idx = ? AND state IN ({marks})",
                    (state, order_id, index, *expected),
                )
            else:
                cursor = db.execute(
                    "UPDATE market_milestones SET state = ?, evidence = ?"
                    f" WHERE order_id = ? AND idx = ? AND state IN ({marks})",
                    (state, evidence, order_id, index, *expected),
                )
            return cursor.rowcount > 0

    @staticmethod
    def _row(row) -> MilestoneState:
        return MilestoneState(
            order_id=row["order_id"],
            index=row["idx"],
            title=row["title"],
            amount_micros=row["amount_micros"],
            state=row["state"],
            evidence=row["evidence"],
        )
