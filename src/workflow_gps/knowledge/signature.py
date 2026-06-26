"""Stable signatures — cache keys for the knowledge layer.

Error-record signatures already exist (``models.errors.compute_signature`` /
``normalise_message``); this module adds the task-level signature and the composite
key the error-pattern store uses, and re-exports the error normalizer so the
knowledge layer has one import surface for "make this a stable, scrub-safe key".
"""

from __future__ import annotations

import hashlib
import re

from ..models import ErrorClass
from ..models.errors import normalise_message  # re-exported for convenience

__all__ = ["task_signature", "error_pattern_key", "normalise_message"]

_WS_RE = re.compile(r"\s+")


def task_signature(intent: str) -> str:
    """Stable 16-char hash of a normalized intent. Lowercased and whitespace-collapsed
    so trivially-different phrasings of the same task share a key."""
    normalized = _WS_RE.sub(" ", intent.strip().lower())
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


def error_pattern_key(error_class: ErrorClass, error_signature: str) -> str:
    """Composite key for the error-pattern store: class + normalized signature."""
    return f"{error_class.value}:{error_signature}"
