"""Delivery — typed events behind gates, and the conserved forecast (A4).

Only verified delivery will ever pay (invariant 7); A4 builds the
verification and keeps the money a PREVIEW. An impression is one row per
placement (dedupe is the primary key); a click needs its impression
first and lands once; a velocity gate throttles a viewer's event rate so
farming is loud and useless. The forecast split is the A5 dry run: the
same conserved arithmetic the PricingEngine enforces — platform plus
every contributor equals the price, to the micro — labeled a forecast,
never a balance.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Callable

from .campaigns import AdError
from .placement import Placement

# The platform's ad commission (α) — a working default; the shipped
# number is decision-log item 1. The complement is the contributor pool.
AD_COMMISSION_ALPHA = 0.30
VELOCITY_CAP_PER_HOUR = 30  # events per viewer per hour — farming is loud

_EVENTS_SCHEMA = """CREATE TABLE IF NOT EXISTS ad_events (
    placement_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    viewer TEXT NOT NULL,
    at TEXT NOT NULL,
    PRIMARY KEY (placement_id, kind)
)"""


class AdEventStore:
    def __init__(self, conn, *, clock: Callable[[], datetime] | None = None) -> None:
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_EVENTS_SCHEMA)

    def now(self) -> datetime:
        return self._clock()

    def _velocity_ok(self, *, tenant: str, viewer: str) -> bool:
        since = (self._clock() - timedelta(hours=1)).isoformat()
        with self._conn.lock:
            row = self._conn.db.execute(
                """SELECT COUNT(*) AS n FROM ad_events
                   WHERE tenant_id = ? AND viewer = ? AND at >= ?""",
                (tenant, viewer, since),
            ).fetchone()
        return int(row["n"]) < VELOCITY_CAP_PER_HOUR

    def record(
        self, placement: Placement, *, kind: str, viewer: str
    ) -> bool:
        """One event, gated. True = newly recorded; False = a replay
        (already counted once — counted once is the whole point)."""
        if kind not in ("impression", "click"):
            raise AdError("kind must be impression or click")
        if viewer != placement.viewer:
            # The occurrence was served to one member; only that member's
            # device can deliver its events.
            raise AdError("this placement was not served to you", status=403)
        if not self._velocity_ok(tenant=placement.tenant_id, viewer=viewer):
            raise AdError(
                "too many ad events this hour — slow down", status=429
            )
        if kind == "click" and not self.has(
            placement.placement_id, kind="impression"
        ):
            raise AdError("a click needs its impression first")
        with self._conn.transaction() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO ad_events
                     (placement_id, tenant_id, kind, viewer, at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    placement.placement_id,
                    placement.tenant_id,
                    kind,
                    viewer,
                    self._clock().isoformat(),
                ),
            )
        return bool(getattr(cursor, "rowcount", 0) or 0)

    def has(self, placement_id: str, *, kind: str) -> bool:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT 1 FROM ad_events WHERE placement_id = ? AND kind = ?",
                (placement_id, kind),
            ).fetchone()
        return row is not None

    def impressed_placements(self, *, tenant: str) -> set[str]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                """SELECT placement_id FROM ad_events
                   WHERE tenant_id = ? AND kind = 'impression'""",
                (tenant,),
            ).fetchall()
        return {str(row["placement_id"]) for row in rows}


def forecast_split(
    price_micros: int, shares: list[tuple[str, float]]
) -> tuple[int, dict[str, int]]:
    """``(platform_micros, {author: micros})`` — conserved exactly.

    The contributor pool is the α-complement of the price, split by the
    lineage weights with the last share absorbing the rounding remainder
    (the PricingEngine discipline); the platform takes the rest, so
    ``platform + Σ contributors == price`` to the micro, always.
    """
    price = int(price_micros)
    if price <= 0 or not shares:
        return price, {}
    pool = (price * int(round((1.0 - AD_COMMISSION_ALPHA) * 100))) // 100
    total_weight = sum(weight for _, weight in shares)
    if total_weight <= 0:
        return price, {}
    out: dict[str, int] = {}
    allocated = 0
    for i, (author, weight) in enumerate(shares):
        if i == len(shares) - 1:
            amount = pool - allocated
        else:
            amount = int(pool * (weight / total_weight))
        out[author] = out.get(author, 0) + amount
        allocated += amount
    return price - pool, out


def preview_earnings(
    placements: list[Placement],
    *,
    impressed: set[str],
    lineage_of: Callable[[Placement], list[tuple[str, float]]],
) -> dict:
    """The display-only forecast: what the standing impressions WOULD
    pay, computed with the same conserved split A5 will post — and
    labeled a forecast, because a preview that reads like a balance is
    a lie with a currency symbol."""
    platform = 0
    contributors: dict[str, int] = {}
    counted = 0
    for placement in placements:
        if placement.placement_id not in impressed:
            continue
        shares = lineage_of(placement)
        if not shares:
            continue
        p, per = forecast_split(placement.price_micros, shares)
        platform += p
        for author, micros in per.items():
            contributors[author] = contributors.get(author, 0) + micros
        counted += 1
    return {
        "forecast": True,  # a preview, never a payout
        "alpha": AD_COMMISSION_ALPHA,
        "impressions_counted": counted,
        "platform_micros": platform,
        "contributors": contributors,
    }
