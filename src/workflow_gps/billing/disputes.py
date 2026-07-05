from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from ..durable.idempotency import IdempotencyLedger
from ..identity.tokens import ProviderConfig
from .guard import require_production_money
from .ledger import BalanceProjection, EarningsLedger
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
        """Close a dispute in the noder's favor. A decision is final: a
        dispute already upheld cannot be re-decided the other way."""
        dispute = self._require(dispute_id)
        if dispute.state is DisputeState.UPHELD:
            raise ValueError("dispute already upheld; it cannot be rejected")
        if dispute.state is DisputeState.REJECTED:
            return dispute  # deciding the same way twice is a no-op
        resolved = dispute.model_copy(
            update={"state": DisputeState.REJECTED, "resolution": "rejected"}
        )
        self._disputes.update(resolved)
        self._audit(
            "dispute.rejected",
            {"dispute_id": dispute_id, "event_id": dispute.event_id},
        )
        return resolved

    def uphold(self, dispute_id: str) -> dict:
        """Uphold a dispute: claw back the event's earnings, reserve-first.

        Every accrual the event minted is reversed with a CLAWBACK entry.
        If a noder was already paid out, the reversal drives their balance
        negative — that shortfall is funded from their RESERVE (this is
        what the settlement holdback exists for), and only what the
        reserve cannot cover remains as debt that future accruals repay
        before anything else pays out. A decision is final; upholding the
        same dispute twice replays the first result.
        """
        require_production_money(self._durable, self._providers)
        dispute = self._require(dispute_id)
        if dispute.state is DisputeState.REJECTED:
            raise ValueError("dispute already rejected; it cannot be upheld")

        def run() -> dict:
            now = self._clock()
            clawed = self._apply_clawback(dispute.event_id, now)
            drawn: dict[str, int] = {}
            debt: dict[str, int] = {}
            projection = BalanceProjection(self._ledger)
            for noder in clawed:
                balance = projection.balance(noder, now=now)
                shortfall = -balance.available_micros
                if shortfall <= 0:
                    continue  # the clawback fit inside unpaid earnings
                draw = min(balance.reserved_micros, shortfall)
                if draw > 0:
                    # Release reserve to absorb the hit — a negative
                    # RESERVE entry, so the projection stays one formula.
                    self._ledger.append(
                        EarningsEntry(
                            noder_principal=noder,
                            event_id=dispute.event_id,
                            amount_micros=-draw,
                            kind=EarningsKind.RESERVE,
                            available_at=now,
                        )
                    )
                    drawn[noder] = draw
                if shortfall - draw > 0:
                    debt[noder] = shortfall - draw
            total_clawed = sum(clawed.values())
            self._disputes.update(
                dispute.model_copy(
                    update={
                        "state": DisputeState.UPHELD,
                        "resolution": f"clawed_back {total_clawed} micros",
                    }
                )
            )
            self._audit(
                "dispute.upheld",
                {
                    "dispute_id": dispute_id,
                    "event_id": dispute.event_id,
                    "clawed_back_micros": total_clawed,
                    "reserve_drawn_micros": sum(drawn.values()),
                    "outstanding_debt_micros": sum(debt.values()),
                },
            )
            return {
                "upheld": True,
                "clawed_back_micros": total_clawed,
                "reserve_drawn_micros": sum(drawn.values()),
                "outstanding_debt_micros": sum(debt.values()),
                "by_noder": {
                    noder: {
                        "clawed_back_micros": amount,
                        "reserve_drawn_micros": drawn.get(noder, 0),
                        "outstanding_debt_micros": debt.get(noder, 0),
                    }
                    for noder, amount in sorted(clawed.items())
                },
            }

        return self._idem.run(f"dispute:{dispute_id}:uphold", run, scope="disputes")

    def refund(self, *, event_id: str, reason: str = "refund") -> dict:
        return self.uphold(self.open(event_id=event_id, reason=reason).dispute_id)

    def _apply_clawback(self, event_id: str, now: datetime) -> dict[str, int]:
        """Reverse every accrual the event minted; returns micros per noder."""
        clawed: dict[str, int] = {}
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
                clawed[entry.noder_principal] = (
                    clawed.get(entry.noder_principal, 0) + entry.amount_micros
                )
        return clawed

    def _audit(self, event_type: str, payload: dict) -> None:
        audit = getattr(self._durable, "audit", None)
        if audit is not None:
            audit.append(event_type, payload)

    def _require(self, dispute_id: str) -> Dispute:
        dispute = self._disputes.get(dispute_id)
        if dispute is None:
            raise ValueError(f"no such dispute: {dispute_id}")
        return dispute
