from __future__ import annotations

from ..metering.models import AttributionRecord, MeteringEvent, NoderShare
from .ledger import BalanceProjection, EarningsLedger
from .models import EarningsEntry, EarningsKind, NoderBalance
from .pricing import PricingEngine, PricingResult


class BillingService:
    def __init__(self, ledger: EarningsLedger, *, rho: float = 0.30) -> None:
        self._ledger = ledger
        self._engine = PricingEngine(rho=rho)
        self._projection = BalanceProjection(ledger)

    def price(
        self, event: MeteringEvent, attributions: list[AttributionRecord]
    ) -> PricingResult:
        shares = [
            NoderShare(
                noder_principal=a.noder_principal, weight=a.weight, multiplier=a.multiplier
            )
            for a in attributions
        ]
        return self._engine.price(
            gross=event.gross or 0.0,
            provider_cost=event.provider_cost or 0.0,
            shares=shares,
        )

    def accrue(
        self, event: MeteringEvent, attributions: list[AttributionRecord]
    ) -> list[EarningsEntry]:
        if event.gross is None:
            return []
        result = self.price(event, attributions)
        appended: list[EarningsEntry] = []
        for noder_principal, amount_micros in result.noder_micros.items():
            entry = EarningsEntry(
                noder_principal=noder_principal,
                event_id=event.event_id,
                amount_micros=amount_micros,
                kind=EarningsKind.ACCRUAL,
                available_at=event.occurred_at,
            )
            if self._ledger.append(entry):
                appended.append(entry)
        return appended

    def balance(self, noder_principal: str) -> NoderBalance:
        return self._projection.balance(noder_principal)
