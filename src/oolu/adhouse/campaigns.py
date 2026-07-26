"""Campaigns — the demand side, typed and bounded (A4).

A campaign is an advertiser's standing offer to pay for attention:
budget in micros, a flight window, taxonomy targeting (the SAME genre
keys that classify contributions — matching is a join on one
vocabulary), the creative, and the affiliate offer it points at. WHO may
advertise is the gateway's gate (a seller-KYC-verified principal);
WHERE the funding lands is the gateway's posting (cash against the
`ad_budget_liability` account). This module holds the records and the
arithmetic — money never moves here.

`charged_micros` is display-only spend recognition: valid impressions
accrue it so budgets deplete and previews forecast, but no ledger moves
until A5's verified-event pipeline. Unspent budget stays a liability —
owed, not earned.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Callable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

MAX_CREATIVE_CHARS = 300  # an ad, not an article
MAX_NAME_CHARS = 80
MIN_BID_MICROS = 1_000  # a tenth of a cent — bids below this say nothing


class AdError(ValueError):
    """A refused ad-house move — the message is the direction."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class Campaign(BaseModel):
    model_config = ConfigDict(frozen=True)

    campaign_id: str
    tenant_id: str
    advertiser: str  # a seller-KYC-verified principal (gateway-gated)
    name: str
    creative: str  # the ad's words — scrub-gated at the door
    offer_ref: str  # the affiliate target: a marketplace listing/offer id
    genres: tuple[str, ...]  # taxonomy targeting — one vocabulary
    bid_micros: int  # per impression
    funded_micros: int  # what the ledger holds as liability
    charged_micros: int  # display-only recognition of served impressions
    status: str  # "active" | "paused"
    flight_start: datetime
    flight_end: datetime
    created_at: datetime

    @property
    def remaining_micros(self) -> int:
        return max(0, self.funded_micros - self.charged_micros)


_SCHEMA = """CREATE TABLE IF NOT EXISTS ad_campaigns (
    campaign_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    advertiser TEXT NOT NULL,
    name TEXT NOT NULL,
    creative TEXT NOT NULL,
    offer_ref TEXT NOT NULL,
    genres TEXT NOT NULL,
    bid_micros INTEGER NOT NULL,
    funded_micros INTEGER NOT NULL,
    charged_micros INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    flight_start TEXT NOT NULL,
    flight_end TEXT NOT NULL,
    created_at TEXT NOT NULL
)"""


class CampaignStore:
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
        advertiser: str,
        name: str,
        creative: str,
        offer_ref: str,
        genres: tuple[str, ...],
        bid_micros: int,
        funded_micros: int,
        flight_start: datetime,
        flight_end: datetime,
    ) -> Campaign:
        campaign = Campaign(
            campaign_id=str(uuid4()),
            tenant_id=tenant,
            advertiser=advertiser,
            name=name,
            creative=creative,
            offer_ref=offer_ref,
            genres=genres,
            bid_micros=int(bid_micros),
            funded_micros=int(funded_micros),
            charged_micros=0,
            status="active",
            flight_start=flight_start,
            flight_end=flight_end,
            created_at=self._clock(),
        )
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO ad_campaigns
                     (campaign_id, tenant_id, advertiser, name, creative,
                      offer_ref, genres, bid_micros, funded_micros,
                      charged_micros, status, flight_start, flight_end,
                      created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    campaign.campaign_id,
                    tenant,
                    advertiser,
                    name,
                    creative,
                    offer_ref,
                    json.dumps(list(genres)),
                    campaign.bid_micros,
                    campaign.funded_micros,
                    0,
                    campaign.status,
                    flight_start.isoformat(),
                    flight_end.isoformat(),
                    campaign.created_at.isoformat(),
                ),
            )
        return campaign

    def get(self, campaign_id: str, *, tenant: str) -> Campaign | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT * FROM ad_campaigns"
                " WHERE campaign_id = ? AND tenant_id = ?",
                (campaign_id, tenant),
            ).fetchone()
        return self._record(row) if row is not None else None

    def list(
        self, *, tenant: str, advertiser: str | None = None
    ) -> list[Campaign]:
        where = "tenant_id = ?"
        args: list = [tenant]
        if advertiser is not None:
            where += " AND advertiser = ?"
            args.append(advertiser)
        with self._conn.lock:
            rows = self._conn.db.execute(
                f"""SELECT * FROM ad_campaigns WHERE {where}
                    ORDER BY created_at DESC, campaign_id DESC LIMIT 200""",
                args,
            ).fetchall()
        return [self._record(row) for row in rows]

    def set_status(
        self, campaign_id: str, *, tenant: str, status: str
    ) -> Campaign | None:
        if status not in ("active", "paused"):
            raise AdError("status must be active or paused")
        with self._conn.transaction() as db:
            db.execute(
                "UPDATE ad_campaigns SET status = ?"
                " WHERE campaign_id = ? AND tenant_id = ?",
                (status, campaign_id, tenant),
            )
        return self.get(campaign_id, tenant=tenant)

    def add_charge(
        self, campaign_id: str, *, tenant: str, amount_micros: int
    ) -> None:
        """Display-only spend recognition — the budget depletes so
        matching stops over-serving; the ledger stays untouched."""
        with self._conn.transaction() as db:
            db.execute(
                """UPDATE ad_campaigns
                   SET charged_micros = charged_micros + ?
                   WHERE campaign_id = ? AND tenant_id = ?""",
                (int(amount_micros), campaign_id, tenant),
            )

    @staticmethod
    def _record(row) -> Campaign:
        return Campaign(
            campaign_id=row["campaign_id"],
            tenant_id=row["tenant_id"],
            advertiser=row["advertiser"],
            name=row["name"],
            creative=row["creative"],
            offer_ref=row["offer_ref"],
            genres=tuple(json.loads(row["genres"])),
            bid_micros=int(row["bid_micros"]),
            funded_micros=int(row["funded_micros"]),
            charged_micros=int(row["charged_micros"]),
            status=row["status"],
            flight_start=datetime.fromisoformat(row["flight_start"]),
            flight_end=datetime.fromisoformat(row["flight_end"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )
