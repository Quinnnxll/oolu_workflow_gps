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


# --------------------------------------------------------------------------- #
# Node-granular signatures.                                                    #
# --------------------------------------------------------------------------- #
def bindings_fingerprint(bindings: dict) -> str:
    """Canonical fingerprint of a node's slot bindings (order-insensitive)."""
    blob = json.dumps(
        bindings, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class NodeScriptSignature:
    """Cache identity for one node's synthesized script.

    Keyed by the node (not the parent intent) plus the slot-binding
    fingerprint and the environment: the same sub-task recurring inside
    *different* workflows hits the same entry — which is exactly where the
    intent-string cache never hits. A changed binding, backend, or
    environment produces a different key, so a stale script is never
    replayed against a world it was not synthesized for.
    """

    node_key: str
    bindings_fingerprint: str
    environment_fingerprint: str
    backend_kind: str
    backend_image: str | None = None
    pinned_index_url: str | None = None
    workflow_gps_version: str = __version__
    cache_schema_version: int = CACHE_SCHEMA_VERSION

    def canonical_payload(self) -> dict:
        payload = asdict(self)
        payload["node_key"] = normalize_intent(self.node_key)
        return payload


def make_node_script_cache_key(signature: NodeScriptSignature) -> str:
    blob = json.dumps(
        signature.canonical_payload(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "node:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()
