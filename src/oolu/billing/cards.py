"""Card on file: how a user saves a payment method — safely, twice over.

The Stripe shape (live mode, once the transaction port opens):

1. The server creates a **Customer** (our ``consumer_ref``) and a
   **SetupIntent**, and hands its ``client_secret`` to the app.
2. **Stripe.js confirms the card in the browser** — the card number goes
   from the user's keyboard to Stripe directly; our servers never see a
   PAN, only the resulting ``pm_...`` payment-method reference.
3. We store metadata (brand, last4, expiry) for display, the ``pm_`` ref
   for charging, and later charge through ``StripeConnectAdapter.charge``
   with the customer ref — behind ``require_production_money`` AND the
   :class:`~oolu.billing.launch.LaunchGuard`.

The pre-launch shape (this build): the **transaction port is not opened**.
``FakeCardVault`` stands in for Stripe — it accepts only named TEST cards
(never a card number; there is no field for one), mints fake ``pm_test``
refs, and can never move money because ``charge`` does not exist on it.
The UI flow, storage, and API are the real ones, so switching to live is
swapping the adapter and opening the guard — not rebuilding the feature.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .payout import PaymentError

# The only cards the pre-launch vault accepts: brand -> display last4.
TEST_CARDS: dict[str, str] = {
    "visa": "4242",
    "mastercard": "4444",
    "amex": "0005",
    "unionpay": "0005",
}


class CardSummary(BaseModel):
    """What we keep about a saved card: display metadata + the provider
    reference. Never a number, never a CVC."""

    model_config = ConfigDict(frozen=True)

    pm_ref: str
    brand: str
    last4: str
    exp_month: int = Field(ge=1, le=12)
    exp_year: int
    added_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PaymentProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    principal: str
    customer_ref: str  # the provider's customer id ("cus_..." live)
    default_pm: str | None = None
    cards: list[CardSummary] = Field(default_factory=list)


@runtime_checkable
class CardVault(Protocol):
    """The provider port for card-on-file. Live = Stripe; pre-launch = fake."""

    @property
    def mode(self) -> str: ...  # "live" | "test"
    def create_customer(self, principal: str) -> str: ...
    def setup_intent(self, customer_ref: str) -> dict: ...
    def attach_test_card(self, customer_ref: str, brand: str) -> CardSummary: ...
    def detach(self, customer_ref: str, pm_ref: str) -> None: ...


class StripeCardVault:
    """Live card vault over the Stripe API (same transport/vault discipline
    as ``StripeConnectAdapter``). Constructed only when a real key exists;
    ``attach_test_card`` is refused — live mode saves cards exclusively via
    a client-confirmed SetupIntent, so no card data ever transits here."""

    def __init__(self, *, vault, transport, api_key_ref, base_url="https://api.stripe.com"):
        self._vault = vault
        self._transport = transport
        self._api_key_ref = api_key_ref
        self._base_url = base_url.rstrip("/")

    @property
    def mode(self) -> str:
        return "live"

    def _post(self, path: str, body: dict) -> dict:
        headers = self._vault.authorize_header(self._api_key_ref, scheme="Bearer")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        response = self._transport.request(
            "POST", f"{self._base_url}{path}", headers=headers, body=body
        )
        if response.status >= 400:
            raise PaymentError(f"stripe {path} failed: status {response.status}")
        return response.json

    def create_customer(self, principal: str) -> str:
        return self._post("/v1/customers", {"metadata[principal]": principal})["id"]

    def setup_intent(self, customer_ref: str) -> dict:
        data = self._post(
            "/v1/setup_intents",
            {"customer": customer_ref, "usage": "off_session"},
        )
        # The client_secret goes to Stripe.js in the browser; the card
        # number never reaches our servers.
        return {"client_secret": data["client_secret"]}

    def attach_test_card(self, customer_ref: str, brand: str) -> CardSummary:
        raise PaymentError(
            "live mode saves cards through a client-confirmed SetupIntent, "
            "never server-side"
        )

    def detach(self, customer_ref: str, pm_ref: str) -> None:
        self._post(f"/v1/payment_methods/{pm_ref}/detach", {})


class FakeCardVault:
    """The pre-launch vault: the transaction port is not opened.

    Only named test cards exist; there is no input that accepts a card
    number, and no ``charge`` method to call — money cannot move through
    this class even by accident."""

    @property
    def mode(self) -> str:
        return "test"

    def create_customer(self, principal: str) -> str:
        return f"cus_test_{uuid4().hex[:12]}"

    def setup_intent(self, customer_ref: str) -> dict:
        return {"client_secret": f"seti_test_{uuid4().hex[:12]}"}

    def attach_test_card(self, customer_ref: str, brand: str) -> CardSummary:
        wanted = brand.strip().lower()
        if wanted not in TEST_CARDS:
            allowed = ", ".join(sorted(TEST_CARDS))
            raise PaymentError(f"test cards only in pre-launch: {allowed}")
        now = datetime.now(UTC)
        return CardSummary(
            pm_ref=f"pm_test_{uuid4().hex[:12]}",
            brand=wanted,
            last4=TEST_CARDS[wanted],
            exp_month=now.month,
            exp_year=now.year + 3,
        )

    def detach(self, customer_ref: str, pm_ref: str) -> None:
        return None


_SCHEMA = """CREATE TABLE IF NOT EXISTS payment_profiles (
    principal TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL
)"""


class PaymentProfileStore:
    """Per-principal payment profile (customer ref + card metadata) on the
    durable connection — display data and references, nothing sensitive."""

    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def get(self, principal: str) -> PaymentProfile | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json FROM payment_profiles WHERE principal = ?",
                (principal,),
            ).fetchone()
        return PaymentProfile.model_validate_json(row["payload_json"]) if row else None

    def save(self, profile: PaymentProfile) -> PaymentProfile:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO payment_profiles (principal, payload_json)
                   VALUES (?, ?)
                   ON CONFLICT(principal) DO UPDATE SET
                     payload_json = excluded.payload_json""",
                (profile.principal, profile.model_dump_json()),
            )
        return profile


class PaymentMethodsService:
    """Store + vault, composed: what the /v1/payment-methods routes call."""

    def __init__(self, store: PaymentProfileStore, vault: CardVault):
        self._store = store
        self._vault = vault

    @property
    def mode(self) -> str:
        return self._vault.mode

    def profile(self, principal: str) -> PaymentProfile:
        existing = self._store.get(principal)
        if existing is not None:
            return existing
        return self._store.save(
            PaymentProfile(
                principal=principal,
                customer_ref=self._vault.create_customer(principal),
            )
        )

    def add_test_card(self, principal: str, brand: str) -> CardSummary:
        profile = self.profile(principal)
        card = self._vault.attach_test_card(profile.customer_ref, brand)
        cards = [*profile.cards, card]
        self._store.save(
            profile.model_copy(
                update={"cards": cards, "default_pm": profile.default_pm or card.pm_ref}
            )
        )
        return card

    def remove_card(self, principal: str, pm_ref: str) -> bool:
        profile = self.profile(principal)
        remaining = [c for c in profile.cards if c.pm_ref != pm_ref]
        if len(remaining) == len(profile.cards):
            return False
        self._vault.detach(profile.customer_ref, pm_ref)
        default = profile.default_pm
        if default == pm_ref:
            default = remaining[0].pm_ref if remaining else None
        self._store.save(
            profile.model_copy(update={"cards": remaining, "default_pm": default})
        )
        return True

    def set_default(self, principal: str, pm_ref: str) -> bool:
        profile = self.profile(principal)
        if not any(c.pm_ref == pm_ref for c in profile.cards):
            return False
        self._store.save(profile.model_copy(update={"default_pm": pm_ref}))
        return True
