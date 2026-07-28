"""The explorer desk — compare, then the best buy (A6).

Side-by-side comparison over VERIFIED in-app data only: price and the
discount fact (a stated list price or nothing), verified-buyer feedback
(a review needs a finished order; self-reviews are refused), the
browsable trust score derived from the durable order book, and lab
evidence attached as contributor-attributed press contributions —
byline-labeled, provenance-resolvable, dividend-eligible.

Two laws hold structurally, both import-scan-proven:
- **Advertising can never buy a ranking** (plan invariant 5): nothing in
  this package imports `adhouse` — the scan in ``tests/test_adhouse.py``
  covers `explorer/` the moment it exists.
- **Verdicts are deterministic** (invariant 10): same candidates, same
  evidence, same mode → same ranking, every factor named in the brief's
  breakdown — the `ClearedPrice.notes` discipline.

Buying is not this package's job: a brief names its winner's listing and
hands off to the standing commerce spine unchanged — intent → digest →
policy ladder → approval → order.
"""

from .bestbuy import BRIEF_MODES, BestBuyBrief, best_buy
from .comparisons import ComparisonRow, ComparisonSet, build_rows, discount_fact
from .decisions import (
    DECISION_TTL_HOURS,
    ComparisonStore,
    infer_mode,
)
from .evidence import (
    EXPLORER_BRIEF_PREFIX,
    REVIEW_FINISHED_STATES,
    ExplorerError,
    LabReport,
    LabStore,
    ProductReview,
    ReviewDesk,
    ReviewStore,
    feedback_of,
    lab_of,
    lab_report,
    trust_from_book,
)
from .travel import (
    TRAVEL_CATEGORY,
    TravelBrief,
    TravelCandidate,
    TravelConstraints,
    plan_trip,
)

__all__ = [
    "DECISION_TTL_HOURS",
    "ComparisonStore",
    "infer_mode",
    "BRIEF_MODES",
    "BestBuyBrief",
    "ComparisonRow",
    "ComparisonSet",
    "EXPLORER_BRIEF_PREFIX",
    "ExplorerError",
    "LabReport",
    "LabStore",
    "ProductReview",
    "REVIEW_FINISHED_STATES",
    "ReviewDesk",
    "ReviewStore",
    "TRAVEL_CATEGORY",
    "TravelBrief",
    "TravelCandidate",
    "TravelConstraints",
    "best_buy",
    "plan_trip",
    "build_rows",
    "discount_fact",
    "feedback_of",
    "lab_of",
    "lab_report",
    "trust_from_book",
]
