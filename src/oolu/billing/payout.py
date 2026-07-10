from __future__ import annotations

from typing import Protocol, runtime_checkable
from uuid import uuid4

from ..providers.base import HttpTransport
from ..providers.vault import CredentialRef, SecretVault
from .models import (
    ChargeReceipt,
    ChargeStatus,
    KycStatus,
    PayoutAccount,
    PayoutReceipt,
    PayoutStatus,
)

_MICROS_PER_MINOR = 10_000  # 1 currency unit = 100 minor units = 1_000_000 micros


class PaymentError(RuntimeError):
    pass


def _to_minor(amount_micros: int) -> int:
    return round(amount_micros / _MICROS_PER_MINOR)


@runtime_checkable
class PayoutAdapter(Protocol):
    def create_account(
        self, *, noder_principal: str, country: str, currency: str
    ) -> PayoutAccount: ...

    def account_status(self, provider_account_id: str) -> KycStatus: ...

    def charge(
        self,
        *,
        idempotency_key: str,
        amount_micros: int,
        currency: str,
        consumer_ref: str,
        metadata: dict[str, str] | None = None,
    ) -> ChargeReceipt: ...

    def payout(
        self,
        *,
        idempotency_key: str,
        provider_account_id: str,
        amount_micros: int,
        currency: str,
        metadata: dict[str, str] | None = None,
    ) -> PayoutReceipt: ...


class StripeConnectAdapter:
    """Production ``PayoutAdapter`` over the Stripe API via the injected transport.

    Never accepts raw card data: consumers are charged through an existing customer
    reference and noders are paid through a connected-account id. The Stripe secret
    key lives in the vault and is minted into a header only at call time.
    """

    def __init__(
        self,
        *,
        vault: SecretVault,
        transport: HttpTransport,
        api_key_ref: CredentialRef,
        base_url: str = "https://api.stripe.com",
    ) -> None:
        self._vault = vault
        self._transport = transport
        self._api_key_ref = api_key_ref
        self._base_url = base_url.rstrip("/")

    def _headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        headers = self._vault.authorize_header(self._api_key_ref, scheme="Bearer")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def _post(self, path: str, body: dict, idempotency_key: str | None = None) -> dict:
        response = self._transport.request(
            "POST",
            f"{self._base_url}{path}",
            headers=self._headers(idempotency_key),
            body=body,
        )
        if response.status >= 400:
            raise PaymentError(
                f"stripe {path} failed: status {response.status}: "
                + self._vault.redact(str(response.json))
            )
        return response.json

    def _get(self, path: str) -> dict:
        response = self._transport.request(
            "GET", f"{self._base_url}{path}", headers=self._headers()
        )
        if response.status >= 400:
            raise PaymentError(f"stripe {path} failed: status {response.status}")
        return response.json

    @staticmethod
    def _kyc(data: dict) -> KycStatus:
        if data.get("charges_enabled") and data.get("payouts_enabled"):
            return KycStatus.VERIFIED
        return KycStatus.PENDING

    def create_account(
        self, *, noder_principal: str, country: str, currency: str
    ) -> PayoutAccount:
        data = self._post(
            "/v1/accounts",
            {"type": "express", "country": country, "default_currency": currency},
        )
        return PayoutAccount(
            noder_principal=noder_principal,
            provider_account_id=data["id"],
            kyc_status=self._kyc(data),
            country=country,
            currency=currency,
        )

    def account_status(self, provider_account_id: str) -> KycStatus:
        return self._kyc(self._get(f"/v1/accounts/{provider_account_id}"))

    def charge(
        self,
        *,
        idempotency_key: str,
        amount_micros: int,
        currency: str,
        consumer_ref: str,
        metadata: dict[str, str] | None = None,
    ) -> ChargeReceipt:
        body = {
            "amount": _to_minor(amount_micros),
            "currency": currency,
            "customer": consumer_ref,
        }
        # Metadata rides to Stripe and comes BACK on webhook events — the
        # only reliable way a refund/dispute finds its metering event.
        for key, value in (metadata or {}).items():
            body[f"metadata[{key}]"] = value
        data = self._post("/v1/charges", body, idempotency_key=idempotency_key)
        status = (
            ChargeStatus.SUCCEEDED
            if data.get("status") in {"succeeded", "paid"}
            else ChargeStatus.PENDING
        )
        return ChargeReceipt(
            provider_ref=data["id"],
            amount_micros=amount_micros,
            currency=currency,
            status=status,
        )

    def payout(
        self,
        *,
        idempotency_key: str,
        provider_account_id: str,
        amount_micros: int,
        currency: str,
        metadata: dict[str, str] | None = None,
    ) -> PayoutReceipt:
        body = {
            "amount": _to_minor(amount_micros),
            "currency": currency,
            "destination": provider_account_id,
        }
        for key, value in (metadata or {}).items():
            body[f"metadata[{key}]"] = value
        data = self._post("/v1/transfers", body, idempotency_key=idempotency_key)
        status = (
            PayoutStatus.PAID
            if data.get("status") in {"paid", "pending", None}
            else PayoutStatus.FAILED
        )
        return PayoutReceipt(
            provider_ref=data["id"],
            amount_micros=amount_micros,
            currency=currency,
            status=status,
            fee_micros=int(data.get("fee", 0)) * _MICROS_PER_MINOR,
        )


class FakePayoutAdapter:
    """In-memory ``PayoutAdapter`` for tests/dev. Deterministic; dedups by
    idempotency key the way a real processor does. Never wire into production."""

    def __init__(self, *, kyc: KycStatus = KycStatus.VERIFIED) -> None:
        self._kyc = kyc
        self.accounts: dict[str, PayoutAccount] = {}
        self._charges: dict[str, ChargeReceipt] = {}
        self._payouts: dict[str, PayoutReceipt] = {}

    def create_account(
        self, *, noder_principal: str, country: str, currency: str
    ) -> PayoutAccount:
        account = PayoutAccount(
            noder_principal=noder_principal,
            provider_account_id="acct_" + uuid4().hex[:16],
            kyc_status=self._kyc,
            country=country,
            currency=currency,
        )
        self.accounts[account.provider_account_id] = account
        return account

    def account_status(self, provider_account_id: str) -> KycStatus:
        account = self.accounts.get(provider_account_id)
        return account.kyc_status if account else KycStatus.PENDING

    def charge(
        self,
        *,
        idempotency_key: str,
        amount_micros: int,
        currency: str,
        consumer_ref: str,
        metadata: dict[str, str] | None = None,
    ) -> ChargeReceipt:
        if idempotency_key in self._charges:
            return self._charges[idempotency_key]
        receipt = ChargeReceipt(
            provider_ref="ch_" + uuid4().hex[:16],
            amount_micros=amount_micros,
            currency=currency,
            status=ChargeStatus.SUCCEEDED,
        )
        self._charges[idempotency_key] = receipt
        return receipt

    def payout(
        self,
        *,
        idempotency_key: str,
        provider_account_id: str,
        amount_micros: int,
        currency: str,
        metadata: dict[str, str] | None = None,
    ) -> PayoutReceipt:
        if idempotency_key in self._payouts:
            return self._payouts[idempotency_key]
        receipt = PayoutReceipt(
            provider_ref="tr_" + uuid4().hex[:16],
            amount_micros=amount_micros,
            currency=currency,
            status=PayoutStatus.PAID,
        )
        self._payouts[idempotency_key] = receipt
        return receipt
