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
        choices=("en", "zh", "es", "fr"),
        description="Interface language.",
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
        key="account.autobuild_consent",
        group="account",
        label="Auto-build nodes on my paths",
        kind=SettingKind.BOOL,
        default=True,
        description="Let OoLu build missing nodes and publish them under my account.",
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
        key="model.provider",
        group="model",
        label="Model provider",
        kind=SettingKind.CHOICE,
        default="auto",
        choices=("auto", "anthropic", "openai"),
        description="Which provider answers chat. Auto tries Anthropic "
        "first, then OpenAI — whichever has a key configured.",
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
        maximum=10_000.0,
        description="Stop calling the model once metered chat spending "
        "reaches this many dollars (0 = no cap). Tasks still run.",
    ),
    # --- budget ----------------------------------------------------------
    SettingField(
        key="budget.hard_cap",
        group="budget",
        label="Hard spending cap",
        kind=SettingKind.NUMBER,
        default=0.0,
        minimum=0.0,
        maximum=100_000.0,
        description="Refuse any task estimated above this (0 = no cap).",
    ),
    SettingField(
        key="budget.review_threshold",
        group="budget",
        label="Review threshold",
        kind=SettingKind.NUMBER,
        default=0.0,
        minimum=0.0,
        maximum=100_000.0,
        description="Ask me to confirm tasks estimated above this (0 = off).",
    ),
    SettingField(
        key="budget.monthly_limit",
        group="budget",
        label="Monthly limit",
        kind=SettingKind.NUMBER,
        default=0.0,
        minimum=0.0,
        maximum=100_000.0,
        description="A soft monthly spending target (0 = none).",
    ),
)

_BY_KEY: dict[str, SettingField] = {f.key: f for f in SETTINGS_CATALOG}


def field_for(key: str) -> SettingField | None:
    return _BY_KEY.get(key.strip())


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
    apply bounded set operations. The only way to change configuration."""

    def __init__(self, store: SettingsStore):
        self._store = store

    def effective(self, tenant: str) -> dict[str, Any]:
        """Every declared setting's current value: stored, else its default."""
        stored = self._store.get_raw(tenant)
        return {f.key: stored.get(f.key, f.default) for f in SETTINGS_CATALOG}

    def describe(self, tenant: str) -> list[dict]:
        """The catalog joined with current values — what a UI or the
        assistant reads to know what CAN be set and to what."""
        values = self.effective(tenant)
        out = []
        for field in SETTINGS_CATALOG:
            item = field.model_dump(mode="json")
            item["value"] = values[field.key]
            out.append(item)
        return out

    def set(self, tenant: str, key: str, value: Any) -> Any:
        """Apply one setting. Unknown key, out-of-bounds value, or a managed
        field → refused.

        This method is the whole guarantee: it consults the catalog, coerces
        against the declared field, and stores the normalized value. There is
        no branch that executes caller-supplied code or invents a key — and
        no caller (UI, API, or the assistant) can write a managed field.
        """
        field = self._writable(key)
        coerced = field.coerce(value)
        self._store.set_raw(tenant, field.key, coerced)
        return coerced

    def set_many(self, tenant: str, changes: dict[str, Any]) -> dict[str, Any]:
        """All-or-nothing across the batch: validate every change against the
        catalog first, then commit — a bad key aborts the whole set."""
        validated = {}
        for key, value in changes.items():
            field = self._writable(key)
            validated[field.key] = field.coerce(value)
        for key, value in validated.items():
            self._store.set_raw(tenant, key, value)
        return validated

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
