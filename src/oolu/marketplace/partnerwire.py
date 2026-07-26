"""HTTP partner adapters (M4 closer): real quotes over the provider seam.

The financing and insurance ports get their production shape: an injected
HTTP transport, a vaulted API key minted into the header at call time and
nowhere else, documented wire shapes, and typed validation of what comes
back — a quote that does not parse as a quote is an error, never a guess.
The reference arithmetic partners in ``partners.py`` remain the offline
defaults; these adapters are what a composed host points at a real lender
or insurer.
"""

from __future__ import annotations

from pydantic import ValidationError

from .errors import MarketplaceError
from .partners import FinancingQuote, InsuranceQuote


class PartnerError(MarketplaceError):
    """The partner refused, failed, or answered nonsense."""


class _HttpPartner:
    def __init__(
        self, *, partner_id: str, vault, transport, api_key_ref, base_url: str
    ) -> None:
        self.partner_id = partner_id
        self._vault = vault
        self._transport = transport
        self._api_key_ref = api_key_ref
        self._base_url = base_url.rstrip("/")

    def _post(self, path: str, body: dict) -> dict:
        headers = self._vault.authorize_header(self._api_key_ref, scheme="Bearer")
        headers["Content-Type"] = "application/json"
        response = self._transport.request(
            "POST", f"{self._base_url}{path}", headers=headers, body=body
        )
        if response.status >= 400:
            raise PartnerError(
                f"partner '{self.partner_id}' {path} failed: "
                f"status {response.status}"
            )
        return response.json


class HttpFinancingPartner(_HttpPartner):
    """POST /quotes → a typed FinancingQuote, or a refusal — never a guess."""

    def quote(
        self, *, principal_micros: int, installments: int, period_days: int
    ) -> FinancingQuote:
        payload = self._post(
            "/quotes",
            {
                "principal_micros": principal_micros,
                "installments": installments,
                "period_days": period_days,
            },
        )
        try:
            quote = FinancingQuote.model_validate(
                {**payload, "partner_id": self.partner_id}
            )
        except ValidationError as exc:
            raise PartnerError(
                f"partner '{self.partner_id}' answered something that is "
                f"not a financing quote: {exc}"
            ) from exc
        if quote.principal_micros != principal_micros:
            raise PartnerError(
                "the partner quoted a different principal than was asked"
            )
        return quote


class HttpInsurancePartner(_HttpPartner):
    """POST /policies/quote → a typed InsuranceQuote."""

    def quote(
        self, *, coverage_micros: int, term_days: int
    ) -> InsuranceQuote:
        payload = self._post(
            "/policies/quote",
            {"coverage_micros": coverage_micros, "term_days": term_days},
        )
        try:
            quote = InsuranceQuote.model_validate(
                {**payload, "partner_id": self.partner_id}
            )
        except ValidationError as exc:
            raise PartnerError(
                f"partner '{self.partner_id}' answered something that is "
                f"not an insurance quote: {exc}"
            ) from exc
        if quote.coverage_micros != coverage_micros:
            raise PartnerError(
                "the partner quoted different coverage than was asked"
            )
        return quote
