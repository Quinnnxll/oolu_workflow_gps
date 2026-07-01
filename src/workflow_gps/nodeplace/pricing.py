from __future__ import annotations

from .models import PricingModel, PricingPolicy


def gross_from_policy(policy: PricingPolicy) -> float:
    if policy.model == PricingModel.PER_SUCCESS:
        return policy.unit_price
    return 0.0
