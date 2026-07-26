"""Placements — merge at render, labeled by law (A4).

A placement is a SERVED OCCURRENCE: computed when a surface renders,
stored so delivery events have something true to bind to, and never
baked into content — deactivate the campaign and the next render simply
computes nothing. Every rendered view carries :data:`SPONSORED_LABEL`;
a view without the label is a defect, not a style choice. The
placement_id doubles as the provenance token the affiliate link and the
delivery doors carry.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Callable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from .campaigns import Campaign

SPONSORED_LABEL = "Sponsored"
# The magazine rule, quantified: ads live between the stories, and a
# member's day holds only so many. Working default; decision-log item.
MAX_PER_VIEWER_PER_DAY = 10


class Placement(BaseModel):
    model_config = ConfigDict(frozen=True)

    placement_id: str  # also the provenance token
    tenant_id: str
    campaign_id: str
    surface: str  # "edition" | "poll" — the only two legal surfaces
    content_ref: str  # story_id / pair_id the ad ran against
    viewer: str  # whose render this occurrence was
    price_micros: int
    breakdown: dict
    at: datetime


_SCHEMA = """CREATE TABLE IF NOT EXISTS ad_placements (
    placement_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    campaign_id TEXT NOT NULL,
    surface TEXT NOT NULL,
    content_ref TEXT NOT NULL,
    viewer TEXT NOT NULL,
    price_micros INTEGER NOT NULL,
    breakdown TEXT NOT NULL,
    at TEXT NOT NULL
)"""


class PlacementStore:
    def __init__(self, conn, *, clock: Callable[[], datetime] | None = None) -> None:
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def now(self) -> datetime:
        return self._clock()

    def create(
        self,
        *,
        tenant: str,
        campaign_id: str,
        surface: str,
        content_ref: str,
        viewer: str,
        price_micros: int,
        breakdown: dict,
    ) -> Placement:
        placement = Placement(
            placement_id=str(uuid4()),
            tenant_id=tenant,
            campaign_id=campaign_id,
            surface=surface,
            content_ref=content_ref,
            viewer=viewer,
            price_micros=int(price_micros),
            breakdown=breakdown,
            at=self._clock(),
        )
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO ad_placements
                     (placement_id, tenant_id, campaign_id, surface,
                      content_ref, viewer, price_micros, breakdown, at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    placement.placement_id,
                    tenant,
                    campaign_id,
                    surface,
                    content_ref,
                    viewer,
                    placement.price_micros,
                    json.dumps(breakdown),
                    placement.at.isoformat(),
                ),
            )
        return placement

    def get(self, placement_id: str, *, tenant: str) -> Placement | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT * FROM ad_placements"
                " WHERE placement_id = ? AND tenant_id = ?",
                (placement_id, tenant),
            ).fetchone()
        return self._record(row) if row is not None else None

    def list(self, *, tenant: str, limit: int = 1000) -> list[Placement]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                """SELECT * FROM ad_placements WHERE tenant_id = ?
                   ORDER BY at DESC, placement_id DESC LIMIT ?""",
                (tenant, int(limit)),
            ).fetchall()
        return [self._record(row) for row in rows]

    def viewer_count_since(
        self, *, tenant: str, viewer: str, since: datetime
    ) -> int:
        with self._conn.lock:
            row = self._conn.db.execute(
                """SELECT COUNT(*) AS n FROM ad_placements
                   WHERE tenant_id = ? AND viewer = ? AND at >= ?""",
                (tenant, viewer, since.isoformat()),
            ).fetchone()
        return int(row["n"])

    def campaigns_seen_since(
        self, *, tenant: str, viewer: str, since: datetime
    ) -> frozenset[str]:
        """Which campaigns this viewer already saw — the frequency cap's
        memory, so one campaign never wallpapers one member's day."""
        with self._conn.lock:
            rows = self._conn.db.execute(
                """SELECT DISTINCT campaign_id FROM ad_placements
                   WHERE tenant_id = ? AND viewer = ? AND at >= ?""",
                (tenant, viewer, since.isoformat()),
            ).fetchall()
        return frozenset(str(row["campaign_id"]) for row in rows)

    @staticmethod
    def day_window(now: datetime) -> datetime:
        return now - timedelta(hours=24)

    @staticmethod
    def _record(row) -> Placement:
        return Placement(
            placement_id=row["placement_id"],
            tenant_id=row["tenant_id"],
            campaign_id=row["campaign_id"],
            surface=row["surface"],
            content_ref=row["content_ref"],
            viewer=row["viewer"],
            price_micros=int(row["price_micros"]),
            breakdown=json.loads(row["breakdown"]),
            at=datetime.fromisoformat(row["at"]),
        )


def placement_view(placement: Placement, campaign: Campaign) -> dict:
    """What a surface renders — the label is structural, not optional."""
    return {
        "placement_id": placement.placement_id,  # the provenance token
        "label": SPONSORED_LABEL,
        "campaign_name": campaign.name,
        "creative": campaign.creative,
        "offer_ref": campaign.offer_ref,
        "advertiser": campaign.advertiser,
        "surface": placement.surface,
        "content_ref": placement.content_ref,
        "breakdown": placement.breakdown,  # the explainable record
    }
