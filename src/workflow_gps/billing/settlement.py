from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from ..durable.idempotency import IdempotencyLedger
from ..identity.tokens import ProviderConfig
from .accounts import PayoutStore
from .guard import require_production_money
from .ledger import BalanceProjection, EarningsLedger
from .models import EarningsEntry, EarningsKind, PayoutBatch
from .payout import PayoutAdapter
from .policy import DEFAULT_MIN_PAYOUT_MICROS, DEFAULT_RESERVE_FRACTION


class SettlementService:
    def __init__(
        self,
        *,
        ledger: EarningsLedger,
        payout_store: PayoutStore,
        payout: PayoutAdapter,
        durable: Any,
        providers: Iterable[ProviderConfig],
        idempotency: IdempotencyLedger,
        reserve_fraction: float = DEFAULT_RESERVE_FRACTION,
        min_payout_micros: int = DEFAULT_MIN_PAYOUT_MICROS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._ledger = ledger
        self._accounts = payout_store
        self._payout = payout
        self._durable = durable
        self._providers = list(providers)
        self._idem = idempotency
        self._reserve = reserve_fraction
        self._min_payout = min_payout_micros
        self._projection = BalanceProjection(ledger)
        self._clock = clock or (lambda: datetime.now(UTC))

    def settle(self, noder_principal: str, *, period_key: str) -> dict:
        require_production_money(self._durable, self._providers)

        def run() -> dict:
            now = self._clock()
            self._top_up_reserve(noder_principal, now)
            available = self._projection.balance(
                noder_principal, now=now
            ).available_micros

            if available < self._min_payout:
                return {"paid": False, "available_micros": available, "reason": "below_threshold"}
            if not self._accounts.is_verified(noder_principal):
                return {"paid": False, "available_micros": available, "reason": "kyc_pending"}

            account = self._accounts.get_account(noder_principal)
            batch = PayoutBatch(noder_principal=noder_principal, amount_micros=available)
            self._accounts.add_batch(batch)
            receipt = self._payout.payout(
                idempotency_key=f"payout:{noder_principal}:{period_key}",
                provider_account_id=account.provider_account_id,
                amount_micros=available,
                currency=account.currency,
            )
            self._accounts.update_batch(
                batch.model_copy(
                    update={"status": receipt.status, "provider_ref": receipt.provider_ref}
                )
            )
            self._ledger.append(
                EarningsEntry(
                    noder_principal=noder_principal,
                    event_id=batch.batch_id,
                    amount_micros=-available,
                    kind=EarningsKind.PAYOUT,
                    available_at=now,
                )
            )
            return {
                "paid": True,
                "batch_id": batch.batch_id,
                "amount_micros": available,
                "provider_ref": receipt.provider_ref,
            }

        return self._idem.run(
            f"settle:{noder_principal}:{period_key}", run, scope="settlement"
        )

    def _top_up_reserve(self, noder_principal: str, now: datetime) -> None:
        entries = self._ledger.entries(noder_principal)
        cleared_gross = sum(
            e.amount_micros
            for e in entries
            if e.kind == EarningsKind.ACCRUAL and e.available_at <= now
        )
        target = round(cleared_gross * self._reserve)
        held = sum(
            e.amount_micros for e in entries if e.kind == EarningsKind.RESERVE
        )
        delta = target - held
        if delta > 0:
            self._ledger.append(
                EarningsEntry(
                    noder_principal=noder_principal,
                    amount_micros=delta,
                    kind=EarningsKind.RESERVE,
                    available_at=now,
                )
            )
