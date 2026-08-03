"""The Paver — the backend that links related nodes into runnable webs.

"Sweep" is a taken noun three ways (CAS GC, hygiene, the representative's
drafting), so the road idiom names this instead: the Paver surveys
candidate roads, negotiates junctions, pours and load-tests the
connective code, and opens the route to traffic. W1 is the FIRST stretch
— the map and the heartbeat: a survey that discovers which of a tenant's
nodes could feed which (direct port matches and near-misses), grouped
into webs anchored at nodes with an external trigger door, refreshed on a
standing, consent-gated, fleet-safe Routine. No code is authored here —
W1 draws the map W2+ will pave.
"""

from __future__ import annotations

from .adapters import (
    AdapterResult,
    AdapterSpec,
    AdapterSynthesizer,
    render_mapping_adapter,
)
from .agent import (
    DEFER,
    AdapterBuild,
    PaveOutcome,
    PaveReport,
    PaverAgent,
    RehearsalResult,
)
from .contracts import (
    NearMiss,
    RouteWeb,
    SurveyReport,
    WebEdge,
)
from .discovery import SurveyNode, WebSurveyor
from .negotiator import ContractNegotiator, NegotiationResult
from .routine import PaverScheduleStore
from .store import PaveStore

__all__ = [
    "DEFER",
    "AdapterBuild",
    "AdapterResult",
    "AdapterSpec",
    "AdapterSynthesizer",
    "ContractNegotiator",
    "NearMiss",
    "NegotiationResult",
    "PaveOutcome",
    "PaveReport",
    "PaveStore",
    "PaverAgent",
    "PaverScheduleStore",
    "RehearsalResult",
    "RouteWeb",
    "SurveyNode",
    "SurveyReport",
    "WebEdge",
    "WebSurveyor",
    "render_mapping_adapter",
]
