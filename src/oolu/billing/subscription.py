"""The subscription lifecycle: a commitment, not a settings knob.

The plan a user is on has money attached, so it must never change through
a casual dropdown. This service owns the only three moves that exist:

* **choose** a plan (with a billing cycle) — allowed only from ``free``.
* **cancel** the current plan — records the moment and, for a paid plan,
  the unused remainder of the period as a **credit** (the "deduction"
  applied when the next plan is chosen).
* **change = cancel, then choose.** There is no in-place upgrade: the
  console makes the user end one commitment before starting another, and
  the credit makes ending early fair.

Pre-launch honesty: the launch guard keeps the real transaction port
closed, so no money moves — prices and credits are computed and shown so
the flow can be verified end to end, and the moment charging opens the
same numbers become real charges against the payment profile.

The settings node displays the plan (managed fields, display-only there);
this service mirrors its state into it via ``reflect`` so what Settings
and the assistant show is always the lifecycle's truth.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Callable

# Pre-launch placeholder prices in micros (USD x 1e6). Deliberately data:
# repriced by editing this table, never by a code path.
PLAN_PRICES_MICROS: dict[str, dict[str, int]] = {
    "free": {"monthly": 0, "yearly": 0},
    "plus": {"monthly": 12_000_000, "yearly": 120_000_000},
    "pro": {"monthly": 39_000_000, "yearly": 390_000_000},
    "enterprise": {"monthly": 99_000_000, "yearly": 990_000_000},
}

_PERIOD_DAYS = {"monthly": 30, "yearly": 365}

_SCHEMA = """CREATE TABLE IF NOT EXISTS subscriptions (
    tenant_id TEXT PRIMARY KEY,
    plan TEXT NOT NULL,
    cycle TEXT NOT NULL,
    started_at TEXT NOT NULL,
    credit_micros INTEGER NOT NULL DEFAULT 0
)"""


class SubscriptionError(ValueError):
    """A refused move: the lifecycle's rules said no."""


class SubscriptionService:
    """Tenant subscriptions over the durable connection."""

    def __init__(
        self,
        conn,
        *,
        settings=None,  # settings_node.SettingsNode: display mirror
        # Whether the operator opened the real transaction port — the
        # launch guard's first gate, surfaced here so the console tells
        # the truth about whether choosing a plan will actually charge.
        charging_open: Callable[[], bool] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._settings = settings
        self._charging_open = charging_open or (lambda: False)
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    # ------------------------------------------------------------------ #
    def view(self, tenant: str) -> dict:
        """The console's whole screen: plan, cycle, period, prices, credit."""
        row = self._row(tenant)
        plan, cycle = row["plan"], row["cycle"]
        started = datetime.fromisoformat(row["started_at"])
        period_days = _PERIOD_DAYS[cycle]
        period_end = started + timedelta(days=period_days)
        return {
            "plan": plan,
            "cycle": cycle,
            "started_at": row["started_at"],
            "period_end": period_end.isoformat() if plan != "free" else None,
            "price_micros": PLAN_PRICES_MICROS[plan][cycle],
            "credit_micros": row["credit_micros"],
            "prices": PLAN_PRICES_MICROS,
            # Pre-launch: numbers are shown and verified, money never moves
            # until the launch guard opens the transaction port.
            "charging_open": bool(self._charging_open()),
        }

    def choose(self, tenant: str, plan: str, cycle: str) -> dict:
        """Start a plan. Only from free — ending the old one is its own,
        deliberate move; there is no silent in-place switch."""
        if plan not in PLAN_PRICES_MICROS:
            raise SubscriptionError(f"unknown plan '{plan}'")
        if cycle not in _PERIOD_DAYS:
            raise SubscriptionError(f"unknown billing cycle '{cycle}'")
        row = self._row(tenant)
        if row["plan"] != "free":
            raise SubscriptionError(
                "cancel your current plan first — changing terms (upgrade, "
                "downgrade, monthly/yearly) always starts by ending the "
                "current commitment"
            )
        if plan == "free":
            raise SubscriptionError("free is where you already are")
        price = PLAN_PRICES_MICROS[plan][cycle]
        credit = min(row["credit_micros"], price)
        due = price - credit
        self._write(
            tenant,
            plan=plan,
            cycle=cycle,
            started_at=self._clock().isoformat(),
            credit_micros=row["credit_micros"] - credit,
        )
        self._mirror(tenant, plan, cycle)
        return {
            "plan": plan,
            "cycle": cycle,
            "price_micros": price,
            "credit_applied_micros": credit,  # the deduction, spelled out
            "due_micros": due,
            "charged": False,  # pre-launch: the port is closed
        }

    def cancel(self, tenant: str) -> dict:
        """End the current plan now. The unused remainder of the period
        becomes credit — the deduction a future choose() applies."""
        row = self._row(tenant)
        plan, cycle = row["plan"], row["cycle"]
        if plan == "free":
            raise SubscriptionError("there is no paid plan to cancel")
        started = datetime.fromisoformat(row["started_at"])
        period = timedelta(days=_PERIOD_DAYS[cycle])
        elapsed = max(timedelta(0), min(self._clock() - started, period))
        unused = 1.0 - (elapsed / period)
        credit = int(PLAN_PRICES_MICROS[plan][cycle] * unused)
        total_credit = row["credit_micros"] + credit
        self._write(
            tenant,
            plan="free",
            cycle="monthly",
            started_at=self._clock().isoformat(),
            credit_micros=total_credit,
        )
        self._mirror(tenant, "free", "monthly")
        return {
            "cancelled": plan,
            "credit_micros": credit,
            "total_credit_micros": total_credit,
        }

    # ------------------------------------------------------------------ #
    def _row(self, tenant: str) -> dict:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT plan, cycle, started_at, credit_micros"
                " FROM subscriptions WHERE tenant_id = ?",
                (tenant,),
            ).fetchone()
        if row is None:
            return {
                "plan": "free",
                "cycle": "monthly",
                "started_at": self._clock().isoformat(),
                "credit_micros": 0,
            }
        return {
            "plan": row["plan"],
            "cycle": row["cycle"],
            "started_at": row["started_at"],
            "credit_micros": row["credit_micros"],
        }

    def _write(self, tenant: str, **fields) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO subscriptions
                     (tenant_id, plan, cycle, started_at, credit_micros)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(tenant_id) DO UPDATE SET
                     plan = excluded.plan,
                     cycle = excluded.cycle,
                     started_at = excluded.started_at,
                     credit_micros = excluded.credit_micros""",
                (
                    tenant,
                    fields["plan"],
                    fields["cycle"],
                    fields["started_at"],
                    fields["credit_micros"],
                ),
            )

    def _mirror(self, tenant: str, plan: str, cycle: str) -> None:
        """Settings display the lifecycle's truth (managed fields)."""
        if self._settings is None:
            return
        self._settings.reflect(tenant, "subscription.plan", plan)
        self._settings.reflect(tenant, "subscription.billing_cycle", cycle)
