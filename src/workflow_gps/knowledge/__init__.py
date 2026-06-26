"""Abstract crowd-intelligence layer — dependency hints (#1) + error patterns (#2).

Route templates (#3) are intentionally excluded from this layer by design.
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
]
