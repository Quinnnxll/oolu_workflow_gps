"""The payment-provider port: authorize first, capture later, refund exactly.

The marketplace's payment discipline (spec ``financial_layer``): reversible
before irreversible. An order AUTHORIZES at confirmation and CAPTURES only
when the fulfillment policy says so; a refund reverses a capture through the
provider that made it. Payment methods are tokenized references
(``pm_...``) — a card number has no field to arrive through, here or
anywhere in OoLu.

Two adapters, one port (the ``CardVault`` pattern):

- :class:`StripePaymentIntents` — live manual-capture PaymentIntents over
  the same vault/transport discipline as ``StripeConnectAdapter``. The
  secret stays in the credential vault; only an auth header is minted per
  call. Constructed only where a real key exists, and every caller is
  behind ``require_production_money``.
- :class:`FakePsp` — the pre-launch adapter: mints ``auth_test``/``ch_test``
  refs, is idempotent per key like the real provider, can simulate a
  decline, and moves no money because there is no money behind it.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from uuid import uuid4

from .payout import PaymentError, _to_minor


@runtime_checkable
class PaymentProviderPort(Protocol):
    """What the order machine needs from a licensed payment provider."""

    @property
    def mode(self) -> str: ...  # "live" | "test"

    def authorize(
        self,
        *,
        amount_micros: int,
        currency: str,
        customer_ref: str,
        payment_method_ref: str,
        idempotency_key: str,
        metadata: dict | None = None,
    ) -> str: ...

    def capture(self, auth_ref: str, *, amount_micros: int, idempotency_key: str) -> str: ...

    def void(self, auth_ref: str, *, idempotency_key: str) -> None: ...

    def refund(self, charge_ref: str, *, amount_micros: int, idempotency_key: str) -> str: ...


class StripePaymentIntents:
    """Manual-capture PaymentIntents: authorize now, capture on fulfillment.

    Same transport/vault seam as the payout adapter — an injected transport
    is the one production seam, so the contract tests run against a fake
    transport and the live class changes nothing.
    """

    def __init__(
        self,
        *,
        vault,
        transport,
        api_key_ref,
        base_url: str = "https://api.stripe.com",
    ) -> None:
        self._vault = vault
        self._transport = transport
        self._api_key_ref = api_key_ref
        self._base_url = base_url.rstrip("/")

    @property
    def mode(self) -> str:
        return "live"

    def _post(self, path: str, body: dict, idempotency_key: str | None = None) -> dict:
        headers = self._vault.authorize_header(self._api_key_ref, scheme="Bearer")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        response = self._transport.request(
            "POST", f"{self._base_url}{path}", headers=headers, body=body
        )
        if response.status >= 400:
            raise PaymentError(f"stripe {path} failed: status {response.status}")
        return response.json

    def authorize(
        self,
        *,
        amount_micros: int,
        currency: str,
        customer_ref: str,
        payment_method_ref: str,
        idempotency_key: str,
        metadata: dict | None = None,
    ) -> str:
        body = {
            "amount": str(_to_minor(amount_micros)),
            "currency": currency.lower(),
            "customer": customer_ref,
            "payment_method": payment_method_ref,
            "capture_method": "manual",
            "confirm": "true",
            "off_session": "true",
        }
        for key, value in (metadata or {}).items():
            body[f"metadata[{key}]"] = str(value)
        return self._post("/v1/payment_intents", body, idempotency_key)["id"]

    def capture(
        self, auth_ref: str, *, amount_micros: int, idempotency_key: str
    ) -> str:
        data = self._post(
            f"/v1/payment_intents/{auth_ref}/capture",
            {"amount_to_capture": str(_to_minor(amount_micros))},
            idempotency_key,
        )
        charges = data.get("latest_charge") or data.get("id")
        return str(charges)

    def void(self, auth_ref: str, *, idempotency_key: str) -> None:
        self._post(f"/v1/payment_intents/{auth_ref}/cancel", {}, idempotency_key)

    def refund(
        self, charge_ref: str, *, amount_micros: int, idempotency_key: str
    ) -> str:
        data = self._post(
            "/v1/refunds",
            {
                "payment_intent": charge_ref,
                "amount": str(_to_minor(amount_micros)),
            },
            idempotency_key,
        )
        return data["id"]


class FakePsp:
    """The pre-launch provider: real shape, no money.

    Idempotent per key exactly like the live provider — the same key
    returns the same reference — so the order machine's replay behavior is
    exercised honestly. ``decline_pm_refs`` simulates an issuer decline.
    """

    def __init__(self, *, decline_pm_refs: frozenset[str] = frozenset()) -> None:
        self._decline = decline_pm_refs
        self._by_key: dict[str, str] = {}
        self.calls: list[tuple] = []

    @property
    def mode(self) -> str:
        return "test"

    def _idempotent(self, key: str, prefix: str) -> tuple[str, bool]:
        if key in self._by_key:
            return self._by_key[key], False
        ref = f"{prefix}_test_{uuid4().hex[:12]}"
        self._by_key[key] = ref
        return ref, True

    def authorize(
        self,
        *,
        amount_micros: int,
        currency: str,
        customer_ref: str,
        payment_method_ref: str,
        idempotency_key: str,
        metadata: dict | None = None,
    ) -> str:
        if payment_method_ref in self._decline:
            raise PaymentError("card declined")
        ref, fresh = self._idempotent(idempotency_key, "auth")
        if fresh:
            self.calls.append(
                ("authorize", amount_micros, currency, payment_method_ref)
            )
        return ref

    def capture(
        self, auth_ref: str, *, amount_micros: int, idempotency_key: str
    ) -> str:
        ref, fresh = self._idempotent(idempotency_key, "ch")
        if fresh:
            self.calls.append(("capture", auth_ref, amount_micros))
        return ref

    def void(self, auth_ref: str, *, idempotency_key: str) -> None:
        _, fresh = self._idempotent(idempotency_key, "void")
        if fresh:
            self.calls.append(("void", auth_ref))

    def refund(
        self, charge_ref: str, *, amount_micros: int, idempotency_key: str
    ) -> str:
        ref, fresh = self._idempotent(idempotency_key, "re")
        if fresh:
            self.calls.append(("refund", charge_ref, amount_micros))
        return ref
