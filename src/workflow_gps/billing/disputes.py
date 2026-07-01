from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from ..durable.idempotency import IdempotencyLedger
from ..identity.tokens import ProviderConfig
from .guard import require_production_money
from .ledger import EarningsLedger
from .models import Dispute, DisputeState, EarningsEntry, EarningsKind

_SCHEMA = """CREATE TABLE IF NOT EXISTS disputes (
    dispute_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
)"""


class DisputeStore:
    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def add(self, dispute: Dispute) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO disputes (dispute_id, event_id, state, payload_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    dispute.dispute_id,
                    dispute.event_id,
                    dispute.state.value,
                    dispute.model_dump_json(),
                    dispute.created_at.isoformat(),
                ),
            )

    def update(self, dispute: Dispute) -> None:
        with self._conn.transaction() as db:
            db.execute(
                "UPDATE disputes SET state = ?, payload_json = ? WHERE dispute_id = ?",
                (dispute.state.value, dispute.model_dump_json(), dispute.dispute_id),
            )

    def get(self, dispute_id: str) -> Dispute | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json FROM disputes WHERE dispute_id = ?", (dispute_id,)
            ).fetchone()
        return Dispute.model_validate_json(row["payload_json"]) if row else None

    def for_event(self, event_id: str) -> list[Dispute]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT payload_json FROM disputes WHERE event_id = ? ORDER BY created_at ASC",
                (event_id,),
            ).fetchall()
        return [Dispute.model_validate_json(row["payload_json"]) for row in rows]


class DisputeService:
    def __init__(
        self,
        *,
        ledger: EarningsLedger,
        disputes: DisputeStore,
        durable: Any,
        providers: Iterable[ProviderConfig],
        idempotency: IdempotencyLedger,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._ledger = ledger
        self._disputes = disputes
        self._durable = durable
        self._providers = list(providers)
        self._idem = idempotency
        self._clock = clock or (lambda: datetime.now(UTC))

    def for_event(self, event_id: str) -> list[Dispute]:
        return self._disputes.for_event(event_id)

    def open(self, *, event_id: str, reason: str = "") -> Dispute:
        dispute = Dispute(event_id=event_id, reason=reason, state=DisputeState.OPEN)
        self._disputes.add(dispute)
        return dispute

    def reject(self, dispute_id: str) -> Dispute:
        dispute = self._require(dispute_id)
        resolved = dispute.model_copy(
            update={"state": DisputeState.REJECTED, "resolution": "rejected"}
        )
        self._disputes.update(resolved)
        return resolved

    def uphold(self, dispute_id: str) -> dict:
        require_production_money(self._durable, self._providers)
        dispute = self._require(dispute_id)

        def run() -> dict:
            clawed = self._apply_clawback(dispute.event_id)
            self._disputes.update(
                dispute.model_copy(
                    update={
                        "state": DisputeState.UPHELD,
                        "resolution": f"clawed_back {clawed} micros",
                    }
                )
            )
            return {"upheld": True, "clawed_back_micros": clawed}

        return self._idem.run(f"dispute:{dispute_id}:uphold", run, scope="disputes")

    def refund(self, *, event_id: str, reason: str = "refund") -> dict:
        return self.uphold(self.open(event_id=event_id, reason=reason).dispute_id)

    def _apply_clawback(self, event_id: str) -> int:
        now = self._clock()
        clawed = 0
        for entry in self._ledger.entries_for_event(event_id):
            if entry.kind != EarningsKind.ACCRUAL:
                continue
            appended = self._ledger.append(
                EarningsEntry(
                    noder_principal=entry.noder_principal,
                    event_id=event_id,
                    amount_micros=-entry.amount_micros,
                    kind=EarningsKind.CLAWBACK,
                    available_at=now,
                )
            )
            if appended:
                clawed += entry.amount_micros
        return clawed

    def _require(self, dispute_id: str) -> Dispute:
        dispute = self._disputes.get(dispute_id)
        if dispute is None:
            raise ValueError(f"no such dispute: {dispute_id}")
        return dispute
