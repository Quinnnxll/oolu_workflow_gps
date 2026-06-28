"""Deterministic, conservative cache signatures for synthesized scripts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass

from workflow_gps import __version__

CACHE_SCHEMA_VERSION = 1


def normalize_intent(intent: str) -> str:
    """Remove insignificant whitespace and casing differences from an intent."""
    return re.sub(r"\s+", " ", intent).strip().casefold()


@dataclass(frozen=True, slots=True)
class ScriptCacheSignature:
    intent: str
    prompt_fingerprint: str
    routing_models: tuple[str, ...]
    backend_kind: str
    backend_image: str | None = None
    pinned_index_url: str | None = None
    workflow_gps_version: str = __version__
    cache_schema_version: int = CACHE_SCHEMA_VERSION

    def canonical_payload(self) -> dict:
        payload = asdict(self)
        payload["intent"] = normalize_intent(self.intent)
        payload["routing_models"] = list(self.routing_models)
        return payload


def make_script_cache_key(signature: ScriptCacheSignature) -> str:
    blob = json.dumps(
        signature.canonical_payload(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
