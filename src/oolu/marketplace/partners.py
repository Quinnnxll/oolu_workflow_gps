"""Financing and insurance partners (M4): partner products are offers.

The whole integration law in one sentence: a partner's product enters the
market as an ordinary typed offer, and therefore cannot skip anything — a
financing plan is a RECURRING offer (the ladder forces its approval, the
recurring book governs its renewals), an insurance policy is an offer with
its own terms, and both walk the intent door like a bottle of water. The
partner adapters behind the port are the production seam; the reference
implementations here are deterministic arithmetic, because a quote a model
invented is not a quote.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from .models import Offer


class FinancingQuote(BaseModel):
    model_config = ConfigDict(frozen=True)

    partner_id: str
    principal_micros: int = Field(gt=0)
    installments: int = Field(gt=0)
    period_days: int = Field(gt=0)
    installment_micros: int = Field(gt=0)
    total_repay_micros: int = Field(gt=0)
    currency: str = "USD"


class InsuranceQuote(BaseModel):
    model_config = ConfigDict(frozen=True)

    partner_id: str
    coverage_micros: int = Field(gt=0)
    premium_micros: int = Field(gt=0)
    term_days: int = Field(gt=0)
    currency: str = "USD"


@runtime_checkable
class FinancingPort(Protocol):
    def quote(
        self, *, principal_micros: int, installments: int, period_days: int
    ) -> FinancingQuote: ...


class SimpleInterestFinancing:
    """The deterministic reference partner: flat simple interest, integer
    arithmetic, remainder on the last installment implied by rounding up
    the per-period amount."""

    def __init__(self, *, partner_id: str, rate_bps: int) -> None:
        self._partner_id = partner_id
        self._rate_bps = rate_bps

    def quote(
        self, *, principal_micros: int, installments: int, period_days: int
    ) -> FinancingQuote:
        interest = principal_micros * self._rate_bps // 10_000
        total = principal_micros + interest
        installment = -(-total // installments)  # ceil division
        return FinancingQuote(
            partner_id=self._partner_id,
            principal_micros=principal_micros,
            installments=installments,
            period_days=period_days,
            installment_micros=installment,
            total_repay_micros=installment * installments,
        )


def financing_offer(quote: FinancingQuote) -> Offer:
    """The financing plan as the recurring offer it is: one installment
    per period, ``recurring_terms`` set — so the ladder REQUIRES approval
    for the first one, and the recurring book governs every renewal."""
    return Offer(
        offer_id=(
            f"financing:{quote.partner_id}:{quote.principal_micros}:"
            f"{quote.installments}x{quote.period_days}d"
        ),
        seller_id=quote.partner_id,
        offer_version=1,
        item_id=(
            f"financing {quote.principal_micros // 1_000_000} over "
            f"{quote.installments} installments"
        ),
        quantity=1,
        subtotal_micros=quote.installment_micros,
        currency=quote.currency,
        refundable=False,
        recurring_terms=(
            f"{quote.installments} installments, every {quote.period_days} days"
        ),
        fulfillment_terms="funds disbursed on approval",
        refund_terms="per the financing agreement",
    )


def insurance_offer(quote: InsuranceQuote) -> Offer:
    return Offer(
        offer_id=(
            f"insurance:{quote.partner_id}:{quote.coverage_micros}:"
            f"{quote.term_days}d"
        ),
        seller_id=quote.partner_id,
        offer_version=1,
        item_id=(
            f"coverage {quote.coverage_micros // 1_000_000} for "
            f"{quote.term_days} days"
        ),
        quantity=1,
        subtotal_micros=quote.premium_micros,
        currency=quote.currency,
        refundable=False,
        fulfillment_terms="policy issued on approval",
        refund_terms="per the policy terms",
    )
