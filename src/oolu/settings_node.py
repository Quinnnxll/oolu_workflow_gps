"""The settings node: configuring OoLu without rewriting OoLu.

The whole point of this node is a guarantee: an assistant (or anyone) can
change the app's configuration ONLY through a fixed catalog of declared
settings, each with a type and hard bounds. There is no code body here to
edit and no free-form value to smuggle — a set operation names a key from
the catalog and a value that must validate against that key's declared
bounds, or it is refused. Configuration is data, gated by a schema.

This mirrors ``ValueInput`` on a NodeContract (see docs/node-generation.md):
the node adapts the caller via its declared bounds, never the reverse.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict

from .currency import CURRENCY_CODES


class SettingKind(str, Enum):
    BOOL = "bool"
    NUMBER = "number"
    CHOICE = "choice"
    TEXT = "text"


class SettingField(BaseModel):
    """One declared, bounded setting — the unit of what may be configured."""

    model_config = ConfigDict(frozen=True)

    key: str
    group: str  # app | account | subscription | model | budget
    label: str
    kind: SettingKind
    default: Any = None
    description: str = ""
    # A managed field is DISPLAY-ONLY through the settings surface: its
    # value is owned by a dedicated service (e.g. the subscription
    # lifecycle) and set() refuses it for every caller — the UI, the
    # API, and OoLu alike. Changing it takes the owning flow, not a knob.
    managed: bool = False
    # What the number MEANS. The sentinel "currency" is resolved by
    # describe() to the tenant's chosen regional currency code, so money
    # fields always display in the unit the user actually pays in.
    unit: str | None = None
    # NUMBER: inclusive bounds. CHOICE: the closed admissible set. TEXT:
    # a max length. All optional; absent means unconstrained within type.
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] | None = None
    max_length: int | None = None

    def coerce(self, value: Any) -> Any:
        """Validate + normalize a value against this field, or raise.

        The one door every set operation passes through. A value that does
        not fit the declared type and bounds never reaches the store — the
        node refuses it rather than reshaping itself to accept it.
        """
        if self.kind is SettingKind.BOOL:
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.strip().lower() in {
                "true",
                "false",
                "on",
                "off",
                "yes",
                "no",
            }:
                return value.strip().lower() in {"true", "on", "yes"}
            raise SettingError(f"{self.key} expects true or false")

        if self.kind is SettingKind.NUMBER:
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise SettingError(f"{self.key} expects a number") from None
            if self.minimum is not None and number < self.minimum:
                raise SettingError(
                    f"{self.key} must be at least {self.minimum}"
                )
            if self.maximum is not None and number > self.maximum:
                raise SettingError(f"{self.key} must be at most {self.maximum}")
            return number

        if self.kind is SettingKind.CHOICE:
            text = str(value).strip()
            if self.choices and text not in self.choices:
                allowed = ", ".join(self.choices)
                raise SettingError(f"{self.key} must be one of: {allowed}")
            return text

        text = str(value)
        if self.max_length is not None and len(text) > self.max_length:
            raise SettingError(
                f"{self.key} must be at most {self.max_length} characters"
            )
        return text


class SettingError(ValueError):
    """A refused set: unknown key or a value outside declared bounds."""


# The prebuilt catalog. Adding a knob means adding a field here — a
# reviewed, bounded declaration — never a code path elsewhere.
SETTINGS_CATALOG: tuple[SettingField, ...] = (
    # --- app -------------------------------------------------------------
    SettingField(
        key="app.theme",
        group="app",
        label="Theme",
        kind=SettingKind.CHOICE,
        default="system",
        choices=("system", "light", "dark"),
        description="The app's colour theme.",
    ),
    SettingField(
        key="app.language",
        group="app",
        label="Language",
        kind=SettingKind.CHOICE,
        default="en",
        choices=("en", "zh", "zh-hant", "es", "fr"),
        description="Interface language.",
    ),
    SettingField(
        key="model.web_search",
        group="model",
        label="Model web search",
        kind=SettingKind.BOOL,
        default=True,
        description="Let the model search the web for current facts when "
        "it needs to (runs inside the provider's API call — Claude today; "
        "a local model never searches).",
    ),
    SettingField(
        key="app.notifications",
        group="app",
        label="Notifications",
        kind=SettingKind.BOOL,
        default=True,
        description="Notify me when a task finishes or needs me.",
    ),
    SettingField(
        key="app.voice_replies",
        group="app",
        label="Speak replies aloud",
        kind=SettingKind.BOOL,
        default=True,
        description="OoLu reads its replies out loud along with the message. "
        "Turn off here for silent conversations.",
    ),
    # --- account ---------------------------------------------------------
    SettingField(
        key="account.display_name",
        group="account",
        label="Display name",
        kind=SettingKind.TEXT,
        default="",
        max_length=80,
        description="The name shown on your account.",
    ),
    SettingField(
        key="account.currency",
        group="account",
        label="Spending currency",
        kind=SettingKind.CHOICE,
        default="USD",
        choices=CURRENCY_CODES,
        description="The legal currency of your region — every cap and "
        "spending amount is entered and shown in it. Conversion to the "
        "meter's internal unit uses fixed reference rates.",
    ),
    SettingField(
        key="account.units",
        group="account",
        label="Measurement units",
        kind=SettingKind.CHOICE,
        default="auto",
        choices=("auto", "metric", "imperial"),
        description="Which measurement system OoLu answers in — metres and "
        "kilograms (metric/SI) or feet and pounds (imperial). Auto follows "
        "your region: imperial for the US, SI everywhere else.",
    ),
    SettingField(
        key="account.log_retention_days",
        group="account",
        label="Execution log retention",
        kind=SettingKind.NUMBER,
        default=180.0,
        minimum=7.0,
        maximum=3650.0,
        unit="days",
        description="How long each node keeps its daily execution log "
        "files (in its Files drawer under logs/) before pruning. Set it to "
        "your legal record-keeping requirement.",
    ),
    SettingField(
        key="account.autobuild_consent",
        group="account",
        label="Auto-build nodes on my paths",
        kind=SettingKind.BOOL,
        default=False,
        description="Let OoLu build missing nodes and publish them under my "
        "account. Off by default: when a task has no existing path, OoLu "
        "asks you to turn this on before building anything new.",
    ),
    # --- subscription: DISPLAY-ONLY here. The plan is a commitment with
    # money attached, not a preference — changing it takes the account
    # console's cancel-first flow, never a settings knob.
    SettingField(
        key="subscription.plan",
        group="subscription",
        label="Plan",
        kind=SettingKind.CHOICE,
        default="free",
        choices=("free", "plus", "pro", "enterprise"),
        managed=True,
        description="Your current plan. Managed in the account console — "
        "cancel the current plan there to change terms.",
    ),
    SettingField(
        key="subscription.billing_cycle",
        group="subscription",
        label="Billing cycle",
        kind=SettingKind.CHOICE,
        default="monthly",
        choices=("monthly", "yearly"),
        managed=True,
        description="Monthly or yearly. Managed in the account console with "
        "the plan.",
    ),
    # --- model (the brain behind chat; keys live in the keyring, NOT here —
    # this catalog is visible data, so a secret must never be a setting) ----
    SettingField(
        key="model.source",
        group="model",
        label="Default model",
        kind=SettingKind.CHOICE,
        default="subscription",
        choices=("subscription", "own-api", "local"),
        description="Where the brain lives. Subscription follows your OoLu "
        "plan (Claude first). Own API makes the key you added below the "
        "default model, overriding the plan. Local uses a model server "
        "running on this machine — no key, no cloud.",
    ),
    SettingField(
        key="model.provider",
        group="model",
        label="Model provider",
        kind=SettingKind.CHOICE,
        default="auto",
        choices=("auto", "anthropic", "openai"),
        description="Which of your own keys answers when the default model "
        "is own API. Auto tries Anthropic first, then OpenAI — whichever "
        "has a key configured.",
    ),
    SettingField(
        key="model.local_url",
        group="model",
        label="Local model URL",
        kind=SettingKind.TEXT,
        default="http://127.0.0.1:11434/v1",
        max_length=200,
        description="The OpenAI-compatible endpoint of the model server on "
        "this machine (Ollama, LM Studio, llama.cpp server). Used only "
        "when the default model is local.",
    ),
    SettingField(
        key="model.local_model",
        group="model",
        label="Local model name",
        kind=SettingKind.TEXT,
        # Same family as the representative trainer's QLoRA base
        # (Qwen/Qwen3-4B-Instruct): the voice you train locally is the
        # model you chat with. The desktop pulls it at launch when Ollama
        # is installed; point this anywhere else to use your own.
        default="qwen3:4b",
        max_length=80,
        description="The model to request from the local server. The "
        "default (qwen3:4b) is pulled automatically at launch when Ollama "
        "is installed; change it to use any other local model.",
    ),
    SettingField(
        key="model.tier",
        group="model",
        label="Model tier",
        kind=SettingKind.CHOICE,
        default="fast",
        choices=("fast", "reasoning"),
        description="Fast answers cheaply; reasoning thinks harder and "
        "costs more per turn.",
    ),
    SettingField(
        key="budget.model_cap",
        group="model",
        label="Model spending cap",
        kind=SettingKind.NUMBER,
        default=0.0,
        minimum=0.0,
        maximum=10_000_000.0,
        unit="currency",
        description="Stop calling the model once metered chat spending "
        "reaches this amount in your spending currency (0 = no cap). "
        "Tasks still run.",
    ),
    # --- budget ----------------------------------------------------------
    SettingField(
        key="budget.hard_cap",
        group="budget",
        label="Hard spending cap",
        kind=SettingKind.NUMBER,
        default=0.0,
        minimum=0.0,
        maximum=100_000_000.0,
        unit="currency",
        description="Refuse any task estimated above this amount in your "
        "spending currency (0 = no cap).",
    ),
    SettingField(
        key="budget.review_threshold",
        group="budget",
        label="Review threshold",
        kind=SettingKind.NUMBER,
        default=0.0,
        minimum=0.0,
        maximum=100_000_000.0,
        unit="currency",
        description="Ask me to confirm tasks estimated above this amount "
        "in your spending currency (0 = off).",
    ),
    SettingField(
        key="budget.monthly_limit",
        group="budget",
        label="Monthly limit",
        kind=SettingKind.NUMBER,
        default=0.0,
        minimum=0.0,
        maximum=100_000_000.0,
        unit="currency",
        description="A soft monthly spending target in your spending "
        "currency (0 = none).",
    ),
)

_BY_KEY: dict[str, SettingField] = {f.key: f for f in SETTINGS_CATALOG}


def field_for(key: str) -> SettingField | None:
    return _BY_KEY.get(key.strip())


# Which setting GROUPS are PERSONAL — the working-style knobs (theme,
# language, voice, units, currency, display name, auto-build consent)
# that must never be shared between accounts on a shared tenant. The
# rest — subscription, model wiring, budget walls — are the TENANT's:
# they govern shared money and shared infrastructure, and staying
# tenant-scoped is what keeps per-tenant caches (the model router, the
# budget profile) safely shareable across accounts.
PERSONAL_GROUPS = frozenset({"app", "account"})


def personal_scope(tenant: str, principal: str) -> str:
    """The storage scope of one account's personal settings. '::' so a
    representative-style 'tenant:principal' string can never collide."""
    return f"{tenant}::{principal}"


def is_personal(key: str) -> bool:
    field = field_for(key)
    return field is not None and field.group in PERSONAL_GROUPS


_SCHEMA = """CREATE TABLE IF NOT EXISTS settings (
    tenant_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    PRIMARY KEY (tenant_id, key)
)"""


class SettingsStore:
    """Tenant-scoped setting values over the durable connection."""

    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def get_raw(self, tenant: str) -> dict[str, Any]:
        import json

        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT key, value_json FROM settings WHERE tenant_id = ?",
                (tenant,),
            ).fetchall()
        return {r["key"]: json.loads(r["value_json"]) for r in rows}

    def erase(self, scope: str) -> int:
        """Every stored value under one scope, gone — the personal layer's
        share of a data-subject erasure."""
        with self._conn.transaction() as db:
            cursor = db.execute(
                "DELETE FROM settings WHERE tenant_id = ?", (scope,)
            )
        return int(getattr(cursor, "rowcount", 0) or 0)

    def set_raw(self, tenant: str, key: str, value: Any) -> None:
        import json

        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO settings (tenant_id, key, value_json)
                   VALUES (?, ?, ?)
                   ON CONFLICT(tenant_id, key) DO UPDATE SET
                     value_json = excluded.value_json""",
                (tenant, key, json.dumps(value)),
            )


class SettingsNode:
    """The declared configuration surface: read the catalog + stored values,
    apply bounded set operations. The only way to change configuration.

    Two layers, split carefully (Issue 12): PERSONAL groups (app,
    account — the working-style knobs) live per account under
    ``tenant::principal`` and overlay the tenant layer; everything else
    (subscription, model wiring, budget walls) stays on the TENANT.
    The tenant layer doubles as the safe shared base: values set there
    reach every account as defaults — one place to configure an org —
    and per-tenant caches built on tenant-scoped settings (the model
    router, the budget profile) stay valid across accounts, because no
    personal value ever feeds them."""

    def __init__(self, store: SettingsStore):
        self._store = store

    def effective(self, tenant: str, principal: str | None = None) -> dict[str, Any]:
        """Every declared setting's current value: the account's own
        (personal groups, when ``principal`` is given), else the tenant's,
        else the catalog default."""
        stored = self._store.get_raw(tenant)
        values = {f.key: stored.get(f.key, f.default) for f in SETTINGS_CATALOG}
        if principal:
            personal = self._store.get_raw(personal_scope(tenant, principal))
            for field in SETTINGS_CATALOG:
                if field.group in PERSONAL_GROUPS and field.key in personal:
                    values[field.key] = personal[field.key]
        return values

    def describe(self, tenant: str, principal: str | None = None) -> list[dict]:
        """The catalog joined with current values — what a UI or the
        assistant reads to know what CAN be set and to what."""
        values = self.effective(tenant, principal)
        # Money fields display in the tenant's own regional currency: the
        # "currency" unit sentinel resolves to their chosen code.
        code = str(values.get("account.currency", "USD") or "USD")
        out = []
        for field in SETTINGS_CATALOG:
            item = field.model_dump(mode="json")
            item["value"] = values[field.key]
            if item.get("unit") == "currency":
                item["unit"] = code
            out.append(item)
        return out

    def set(
        self, tenant: str, key: str, value: Any, principal: str | None = None
    ) -> Any:
        """Apply one setting. Unknown key, out-of-bounds value, or a managed
        field → refused.

        This method is the whole guarantee: it consults the catalog, coerces
        against the declared field, and stores the normalized value. There is
        no branch that executes caller-supplied code or invents a key — and
        no caller (UI, API, or the assistant) can write a managed field.
        With a ``principal``, PERSONAL-group keys land on the account's own
        layer; tenant-group keys always land on the tenant."""
        field = self._writable(key)
        coerced = field.coerce(value)
        self._store.set_raw(self._scope_for(field, tenant, principal), field.key, coerced)
        return coerced

    def set_many(
        self,
        tenant: str,
        changes: dict[str, Any],
        principal: str | None = None,
    ) -> dict[str, Any]:
        """All-or-nothing across the batch: validate every change against the
        catalog first, then commit — a bad key aborts the whole set."""
        validated = {}
        for key, value in changes.items():
            field = self._writable(key)
            validated[field.key] = (field, field.coerce(value))
        for key, (field, value) in validated.items():
            self._store.set_raw(
                self._scope_for(field, tenant, principal), key, value
            )
        return {key: value for key, (_, value) in validated.items()}

    def erase_personal(self, tenant: str, principal: str) -> int:
        """The account's personal layer, gone — its share of erasure. The
        tenant layer stays: it belongs to the tenant, not the account."""
        return self._store.erase(personal_scope(tenant, principal))

    @staticmethod
    def _scope_for(
        field: SettingField, tenant: str, principal: str | None
    ) -> str:
        if principal and field.group in PERSONAL_GROUPS:
            return personal_scope(tenant, principal)
        return tenant

    def reflect(self, tenant: str, key: str, value: Any) -> Any:
        """The owning service's door for a MANAGED field: the subscription
        lifecycle (and only such owners) mirrors its state here so the
        settings surface displays truth. Still catalog-validated — an owner
        cannot invent keys or out-of-bounds values either."""
        field = field_for(key)
        if field is None:
            raise SettingError(f"no such setting '{key}'")
        coerced = field.coerce(value)
        self._store.set_raw(tenant, field.key, coerced)
        return coerced

    def _writable(self, key: str) -> SettingField:
        field = field_for(key)
        if field is None:
            raise SettingError(f"no such setting '{key}'")
        if field.managed:
            raise SettingError(
                f"{field.key} is managed in the account console — cancel the "
                "current plan there to change terms"
            )
        return field
