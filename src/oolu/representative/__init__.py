"""Representative mode: a personal language model for every user.

See docs/representative-plan.md. Phase 0 is retrieval + persona few-shot
over the shared base model, drafts-only. Phase 1 adds the per-user QLoRA
pipeline: the dataset builder, the trainer worker on its own durable
queue, the adapter registry, and vLLM multi-LoRA serving. Preference
tuning and earned auto-send (Phase 2) build on these same seams. The
training stack itself stays behind ``pip install 'oolu[representative-train]'``
and a subprocess boundary — nothing here imports torch.
"""

from .dataset import COLD_START_FLOOR, DatasetStats, build_sft_dataset, to_jsonl
from .engine import RepresentativeEngine, RepresentativeFallback, pair_exchanges
from .gate import commitment_marker, judge
from .memory import ExchangeMemory, StoreExchangeMemory
from .models import Draft, GateVerdict, PersonaCard, RecallHit
from .serving import (
    AdapterServer,
    NoopAdapterServer,
    StoreAdapterServer,
    VllmAdapterServer,
    adapter_name,
    scope_digest,
)
from .store import RepresentativeStore

__all__ = [
    "AdapterServer",
    "COLD_START_FLOOR",
    "DatasetStats",
    "Draft",
    "ExchangeMemory",
    "GateVerdict",
    "NoopAdapterServer",
    "PersonaCard",
    "RecallHit",
    "RepresentativeEngine",
    "RepresentativeFallback",
    "RepresentativeStore",
    "StoreAdapterServer",
    "StoreExchangeMemory",
    "VllmAdapterServer",
    "adapter_name",
    "build_sft_dataset",
    "commitment_marker",
    "judge",
    "pair_exchanges",
    "scope_digest",
    "to_jsonl",
]
