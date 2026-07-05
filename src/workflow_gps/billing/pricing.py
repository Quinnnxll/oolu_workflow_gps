from __future__ import annotations

from dataclasses import dataclass

from ..metering.models import NoderShare
from .models import to_micros


@dataclass(frozen=True)
class PricingResult:
    net_micros: int
    platform_micros: int
    noder_micros: dict[str, int]

    def conserves(self) -> bool:
        return self.platform_micros + sum(self.noder_micros.values()) == self.net_micros


class PricingEngine:
    def __init__(self, *, rho: float = 0.30) -> None:
        if not 0.0 <= rho <= 1.0:
            raise ValueError("rho (platform commission rate) must be in [0, 1]")
        self._rho = rho

    def price(
        self,
        *,
        gross: float,
        provider_cost: float,
        shares: list[NoderShare],
    ) -> PricingResult:
        net = max(0, to_micros(gross) - to_micros(provider_cost))
        weights = [(s.noder_principal, s.weight * s.multiplier) for s in shares]
        total_weight = sum(weight for _, weight in weights if weight > 0)

        if net <= 0 or total_weight <= 0:
            return PricingResult(net_micros=net, platform_micros=net, noder_micros={})

        platform = round(net * self._rho)
        pool = net - platform
        noder: dict[str, int] = {}
        allocated = 0
        positive = [(p, w) for p, w in weights if w > 0]
        for index, (principal, weight) in enumerate(positive):
            if index < len(positive) - 1:
                amount = int(pool * weight / total_weight)
                allocated += amount
            else:
                amount = pool - allocated
            noder[principal] = noder.get(principal, 0) + amount
        return PricingResult(
            net_micros=net, platform_micros=platform, noder_micros=noder
        )
