"""Representative mode: a personal language model for every user (Phase 0).

See docs/representative-plan.md. Phase 0 is retrieval + persona few-shot
over the shared base model, drafts-only; the adapter pipeline (Phase 1)
and preference tuning / auto-send (Phase 2) build on these same seams.
"""

from .engine import RepresentativeEngine, RepresentativeFallback, pair_exchanges
from .gate import commitment_marker, judge
from .memory import ExchangeMemory, StoreExchangeMemory
from .models import Draft, GateVerdict, PersonaCard, RecallHit
from .serving import AdapterServer, NoopAdapterServer
from .store import RepresentativeStore

__all__ = [
    "AdapterServer",
    "Draft",
    "ExchangeMemory",
    "GateVerdict",
    "NoopAdapterServer",
    "PersonaCard",
    "RecallHit",
    "RepresentativeEngine",
    "RepresentativeFallback",
    "RepresentativeStore",
    "StoreExchangeMemory",
    "commitment_marker",
    "judge",
    "pair_exchanges",
]
