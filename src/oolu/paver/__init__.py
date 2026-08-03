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

from .agent import PaveOutcome, PaveReport, PaverAgent, RehearsalResult
from .contracts import (
    NearMiss,
    RouteWeb,
    SurveyReport,
    WebEdge,
)
from .discovery import SurveyNode, WebSurveyor
from .routine import PaverScheduleStore
from .store import PaveStore

__all__ = [
    "NearMiss",
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
]
