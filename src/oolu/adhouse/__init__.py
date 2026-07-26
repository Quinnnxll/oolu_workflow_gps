"""The ad house — buying, matching, merging; the money stays previews (A4).

Four concerns, deliberately separated (agents-expansion plan §4):

- **Buying** (`campaigns.py`) — a seller-KYC-verified advertiser funds a
  campaign: budget, flight, taxonomy targeting, creative, affiliate
  offer ref. The funding POSTING lives at the gateway on the standing
  double-entry ledger (cash in, liability standing) — this package never
  writes a ledger, which the import scan proves.
- **Matching** (`matching.py`) — a deterministic second-price pick over
  eligible campaigns: taxonomy fit, bounded consented-affinity pull,
  caps, bid. Same inputs, same placement, same factor breakdown —
  reproducibility is the audit.
- **Merging** (`placement.py`) — placements are computed at render and
  stored as served occurrences; every rendered view carries the label.
  Deactivating a campaign removes every placement on the next render,
  because placements are computed, never baked into content.
- **Delivery** (`delivery.py`) — typed impression/click events behind
  dedupe and velocity gates, and the conserved display-only forecast
  split (the A5 dry run: platform + Σ contributors == price, exactly).

The walls, both proven by import scans in ``tests/test_adhouse.py``:
``press/`` and ``explorer/`` import nothing from here (advertising can
never buy a ranking — invariant 5), and nothing here imports a money
path (no billing, no PSP — invariant "no ledger writes in adhouse").
"""

from .campaigns import (
    MAX_CREATIVE_CHARS,
    MIN_BID_MICROS,
    AdError,
    Campaign,
    CampaignStore,
)
from .delivery import (
    AD_COMMISSION_ALPHA,
    VELOCITY_CAP_PER_HOUR,
    AdEventStore,
    forecast_split,
    preview_earnings,
)
from .matching import MIN_PRICE_MICROS, MatchResult, match
from .placement import (
    MAX_PER_VIEWER_PER_DAY,
    SPONSORED_LABEL,
    Placement,
    PlacementStore,
    placement_view,
)

__all__ = [
    "AD_COMMISSION_ALPHA",
    "AdError",
    "AdEventStore",
    "Campaign",
    "CampaignStore",
    "MAX_CREATIVE_CHARS",
    "MAX_PER_VIEWER_PER_DAY",
    "MIN_BID_MICROS",
    "MIN_PRICE_MICROS",
    "MatchResult",
    "Placement",
    "PlacementStore",
    "SPONSORED_LABEL",
    "VELOCITY_CAP_PER_HOUR",
    "forecast_split",
    "match",
    "placement_view",
    "preview_earnings",
]
