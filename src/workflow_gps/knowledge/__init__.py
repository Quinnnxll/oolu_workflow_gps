"""Abstract crowd-intelligence layer — dependency hints (#1) + error patterns (#2).

Route templates (#3) are intentionally excluded from the *crowd* layer by
design. The ``traces`` module is different: private, per-user execution-trace
statistics (success posteriors, precedence, costs) that never leave the
device — the personalization substrate, not shared knowledge.
"""

from __future__ import annotations

from .auth import (
    OAuth2PKCETokenProvider,
    StaticTokenProvider,
    TokenProvider,
    generate_pkce_pair,
)
from .client import (
    KnowledgeClient,
    LocalKnowledgeClient,
    NoopKnowledgeClient,
)
from .remote import (
    RemoteConfig,
    RemoteKnowledgeClient,
    Transport,
    TransportError,
    UrllibTransport,
)
from .scrubbing import is_safe_identifier, is_safe_to_store, scrub
from .signature import error_pattern_key, task_signature
from .traces import (
    TRACE_STORE_MIGRATIONS,
    NodeObservation,
    NodePosterior,
    TraceStore,
    route_node_key,
)

__all__ = [
    # protocol + implementations
    "KnowledgeClient",
    "NoopKnowledgeClient",
    "LocalKnowledgeClient",
    "RemoteKnowledgeClient",
    # remote plumbing
    "RemoteConfig",
    "Transport",
    "UrllibTransport",
    "TransportError",
    # auth
    "TokenProvider",
    "StaticTokenProvider",
    "OAuth2PKCETokenProvider",
    "generate_pkce_pair",
    # safety + keys
    "scrub",
    "is_safe_to_store",
    "is_safe_identifier",
    "task_signature",
    "error_pattern_key",
    # private execution-trace statistics (personalization, never shared)
    "TRACE_STORE_MIGRATIONS",
    "NodeObservation",
    "NodePosterior",
    "TraceStore",
    "route_node_key",
]
