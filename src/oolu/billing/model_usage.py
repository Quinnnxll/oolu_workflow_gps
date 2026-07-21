"""The hosted brain's books: platform keys, per-tenant usage, plan allowances.

``model.source = "subscription"`` promises a brain that comes with the plan.
This module is what makes that promise real on a host whose operator has
configured *platform* model keys:

- The keys live in the same encrypted :class:`~oolu.providers.keyring.ModelKeyring`
  every tenant uses, under the reserved :data:`PLATFORM_TENANT` row — one
  storage discipline, no second secret path.
- Every consultation a tenant makes through the platform's keys is booked
  per tenant and per month in :class:`ModelUsageStore` (durable — quotas
  must survive restarts, unlike the in-memory telemetry meter).
- :class:`SubscriptionBrain` is the router's view of both: the keys, the
  plan's monthly allowance, and the spend so far.

Allowances are data, not code: repriced by editing the table.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Callable

# The keyring row platform keys live under. Reserved: registration tenants
# are operator-chosen short names ("main", "t1"); nothing may claim this.
PLATFORM_TENANT = "oolu-platform"

# What each PAID plan includes per month, in the meter's USD unit. The
# free plan's entry stays 0 here because free is not a monthly allowance —
# it is the one-time trial below.
PLAN_MODEL_ALLOWANCE_USD: dict[str, float] = {
    "free": 0.0,
    "plus": 5.0,
    "pro": 20.0,
    "enterprise": 100.0,
}

# The free tier's taste of the hosted brain: a LIFETIME total across the
# platform's providers (OpenAI + Anthropic alike — the meter counts USD,
# not vendors). It never renews; a paid plan or the user's own key is the
# door onward. Data, not policy: repriced by editing this number.
FREE_TRIAL_ALLOWANCE_USD = 10.0

_SCHEMA = """CREATE TABLE IF NOT EXISTS model_usage (
    tenant_id TEXT NOT NULL,
    month TEXT NOT NULL,
    source TEXT NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, month, source)
)"""


class ModelUsageStore:
    """Durable per-tenant model usage, bucketed by calendar month and by
    source (subscription | own-api | local) — the quota reads subscription
    rows only; the others are the tenant's own money or machine."""

    def __init__(self, conn, *, clock: Callable[[], datetime] | None = None) -> None:
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def _month(self, month: str | None = None) -> str:
        return month or self._clock().strftime("%Y-%m")

    def record(
        self,
        tenant: str,
        *,
        source: str,
        cost: float,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO model_usage
                     (tenant_id, month, source, calls, prompt_tokens,
                      completion_tokens, cost_usd)
                   VALUES (?, ?, ?, 1, ?, ?, ?)
                   ON CONFLICT(tenant_id, month, source) DO UPDATE SET
                     calls = model_usage.calls + 1,
                     prompt_tokens =
                       model_usage.prompt_tokens + excluded.prompt_tokens,
                     completion_tokens =
                       model_usage.completion_tokens
                         + excluded.completion_tokens,
                     cost_usd = model_usage.cost_usd + excluded.cost_usd""",
                (
                    tenant,
                    self._month(),
                    source,
                    int(prompt_tokens),
                    int(completion_tokens),
                    float(cost),
                ),
            )

    def month_cost(
        self, tenant: str, *, source: str | None = None, month: str | None = None
    ) -> float:
        query = (
            "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM model_usage"
            " WHERE tenant_id = ? AND month = ?"
        )
        args: tuple = (tenant, self._month(month))
        if source is not None:
            query += " AND source = ?"
            args = (*args, source)
        with self._conn.lock:
            row = self._conn.db.execute(query, args).fetchone()
        return float(row["total"] if row else 0.0)

    def total_cost(self, tenant: str, *, source: str | None = None) -> float:
        """All-time spend — what a lifetime trial is measured against."""
        query = (
            "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM model_usage"
            " WHERE tenant_id = ?"
        )
        args: tuple = (tenant,)
        if source is not None:
            query += " AND source = ?"
            args = (*args, source)
        with self._conn.lock:
            row = self._conn.db.execute(query, args).fetchone()
        return float(row["total"] if row else 0.0)

    def tenants(self) -> list[str]:
        """Every tenant that ever drew a model call — the admin monitor's
        roster, read from the books themselves, never a separate list."""
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT DISTINCT tenant_id FROM model_usage ORDER BY tenant_id"
            ).fetchall()
        return [row["tenant_id"] for row in rows]

    def all_time(self, tenant: str) -> dict:
        """The tenant's whole ledger line, all months and sources summed —
        what the admin's monitor shows next to the quota view."""
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT COALESCE(SUM(calls), 0) AS calls,"
                " COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,"
                " COALESCE(SUM(completion_tokens), 0) AS completion_tokens,"
                " COALESCE(SUM(cost_usd), 0) AS cost_usd"
                " FROM model_usage WHERE tenant_id = ?",
                (tenant,),
            ).fetchone()
        return {
            "calls": int(row["calls"]),
            "prompt_tokens": int(row["prompt_tokens"]),
            "completion_tokens": int(row["completion_tokens"]),
            "cost_usd": float(row["cost_usd"]),
        }

    def reset(self, tenant: str, *, source: str = "subscription") -> float:
        """The give-back: erase a tenant's booked spend for one source
        (all months — a trial is measured lifetime, so a give-back must
        reach the whole history) and return the amount forgiven. Only
        the named source moves: own-api and local rows are the tenant's
        own money and machine, never the platform's to forgive."""
        forgiven = self.total_cost(tenant, source=source)
        with self._conn.transaction() as db:
            db.execute(
                "DELETE FROM model_usage WHERE tenant_id = ? AND source = ?",
                (tenant, source),
            )
        return forgiven

    def view(self, tenant: str, *, month: str | None = None) -> list[dict]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT source, calls, prompt_tokens, completion_tokens,"
                " cost_usd FROM model_usage"
                " WHERE tenant_id = ? AND month = ? ORDER BY source",
                (tenant, self._month(month)),
            ).fetchall()
        return [
            {
                "source": row["source"],
                "calls": row["calls"],
                "prompt_tokens": row["prompt_tokens"],
                "completion_tokens": row["completion_tokens"],
                "cost_usd": row["cost_usd"],
            }
            for row in rows
        ]


class SubscriptionBrain:
    """What the router needs to serve ``model.source="subscription"``:
    the platform's keys, the tenant's plan allowance, and the month's
    subscription spend. Holds no policy of its own — the router decides."""

    def __init__(
        self,
        keyring,  # providers.ModelKeyring — platform keys under PLATFORM_TENANT
        usage: ModelUsageStore,
        *,
        plan_for: Callable[[str], str],
        allowances: dict[str, float] | None = None,
        trial_usd: float = FREE_TRIAL_ALLOWANCE_USD,
    ) -> None:
        self._keyring = keyring
        self._usage = usage
        self._plan_for = plan_for
        self._allowances = allowances or PLAN_MODEL_ALLOWANCE_USD
        self._trial_usd = float(trial_usd)

    def configured(self) -> bool:
        """Whether this host has ANY platform key — the difference between
        'the hosted brain exists here' and the honest 'not live yet'."""
        return bool(self._keyring.providers(PLATFORM_TENANT))

    def secret_for(self, provider: str) -> str | None:
        return self._keyring.secret_for(PLATFORM_TENANT, provider)

    def allowance_for(self, tenant: str) -> float:
        if self.is_trial(tenant):
            return self._trial_usd
        plan = str(self._plan_for(tenant) or "free")
        return float(self._allowances.get(plan, 0.0))

    def is_trial(self, tenant: str) -> bool:
        """Free tier drinks from the one-time trial, not a plan allowance."""
        return str(self._plan_for(tenant) or "free") == "free"

    def month_spend(self, tenant: str) -> float:
        return self._usage.month_cost(tenant, source="subscription")

    def spend_for(self, tenant: str) -> float:
        """What counts against the allowance: the month for a paid plan,
        ALL TIME for the free trial — a trial never renews."""
        if self.is_trial(tenant):
            return self._usage.total_cost(tenant, source="subscription")
        return self.month_spend(tenant)

    def record(self, tenant: str, record, source: str) -> None:
        """Book one consultation (a ``ModelCallRecord``) under the tenant."""
        self._usage.record(
            tenant,
            source=source,
            cost=float(getattr(record, "cost", 0.0) or 0.0),
            prompt_tokens=int(getattr(record, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(record, "completion_tokens", 0) or 0),
        )
