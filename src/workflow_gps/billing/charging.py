from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta
from typing import Any

from ..durable.idempotency import IdempotencyLedger
from ..identity.tokens import ProviderConfig
from ..metering.models import AttributionRecord, MeteringEvent, NoderShare
from .guard import require_production_money
from .ledger import EarningsLedger
from .models import EarningsEntry, EarningsKind, to_micros
from .payout import PayoutAdapter
from .policy import DEFAULT_HOLDBACK_DAYS, DEFAULT_RHO
from .pricing import PricingEngine


class ChargingService:
    def __init__(
        self,
        *,
        ledger: EarningsLedger,
        payout: PayoutAdapter,
        durable: Any,
        providers: Iterable[ProviderConfig],
        idempotency: IdempotencyLedger,
        rho: float = DEFAULT_RHO,
        holdback_days: int = DEFAULT_HOLDBACK_DAYS,
        currency: str = "usd",
    ) -> None:
        self._ledger = ledger
        self._payout = payout
        self._durable = durable
        self._providers = list(providers)
        self._idem = idempotency
        self._engine = PricingEngine(rho=rho)
        self._holdback = timedelta(days=holdback_days)
        self._currency = currency

    def charge_and_accrue(
        self,
        event: MeteringEvent,
        attributions: list[AttributionRecord],
        *,
        consumer_ref: str,
    ) -> dict:
        if event.gross is None:
            return {"charged": False, "reason": "no billable gross on event"}
        require_production_money(self._durable, self._providers)

        def run() -> dict:
            receipt = self._payout.charge(
                idempotency_key=event.idempotency_key,
                amount_micros=to_micros(event.gross),
                currency=self._currency,
                consumer_ref=consumer_ref,
            )
            result = self._engine.price(
                gross=event.gross,
                provider_cost=event.provider_cost or 0.0,
                shares=[
                    NoderShare(
                        noder_principal=a.noder_principal,
                        weight=a.weight,
                        multiplier=a.multiplier,
                    )
                    for a in attributions
                ],
            )
            available_at = event.occurred_at + self._holdback
            for noder_principal, amount_micros in result.noder_micros.items():
                self._ledger.append(
                    EarningsEntry(
                        noder_principal=noder_principal,
                        event_id=event.event_id,
                        amount_micros=amount_micros,
                        kind=EarningsKind.ACCRUAL,
                        available_at=available_at,
                    )
                )
            return {
                "charged": True,
                "charge_ref": receipt.provider_ref,
                "charged_micros": to_micros(event.gross),
                "noder_accruals": dict(result.noder_micros),
            }

        return self._idem.run(f"charge:{event.idempotency_key}", run, scope="billing")
