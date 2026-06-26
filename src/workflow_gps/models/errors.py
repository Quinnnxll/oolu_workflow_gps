"""Error taxonomy and records — the load-bearing classification layer.

Not all stderr is recalculable. This module draws the single most important line
in the whole system: between failures the navigation engine can route *around*
(install a missing dependency, rewrite the generated code) and failures it must
*halt on and surface* (permissions, auth, resource exhaustion). Getting this
boundary right is what keeps "self-healing" from quietly becoming "self-harming"
— an infinite recalc loop burning compute on a genuinely impossible task.

`errors.py` is the lowest layer of the shared vocabulary. It imports nothing from
the rest of the package, so every other module can depend on it without risking a
circular import.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class ErrorClass(str, Enum):
    """How the runtime should *react* to a failure, not merely what it was."""

    # --- Recalculable: the engine can alter its route and keep moving ---
    MISSING_DEPENDENCY = "missing_dependency"   # ModuleNotFoundError -> Phase-A install
    SYNTAX_ERROR = "syntax_error"               # malformed generated code -> rewrite
    RUNTIME_EXCEPTION = "runtime_exception"     # generic raise at exec time -> rewrite
    CONTRACT_VIOLATION = "contract_violation"   # no / invalid sentinel JSON -> rewrite
    TIMEOUT = "timeout"                         # wall-clock exceeded -> rewrite (cautiously)
    SYNTHESIS_FAILED = "synthesis_failed"       # model returned no usable code -> re-prompt

    # --- Halt-and-surface: looping here is harmful, never helpful ---
    INSTALL_FAILED = "install_failed"           # Phase A: package won't install (no such dist)
    PERMISSION_DENIED = "permission_denied"     # PermissionError on fs / device
    AUTH_FAILURE = "auth_failure"               # 401 / 403 from a called API
    RESOURCE_EXHAUSTED = "resource_exhausted"   # OOM, disk full, fork bomb guard
    NETWORK_DENIED = "network_denied"           # egress blocked — expected in Phase B

    # --- Unknown: treat conservatively (baseline = do not loop blindly) ---
    UNKNOWN = "unknown"

    @property
    def is_recalculable(self) -> bool:
        """True when the engine has a legitimate route around this failure."""
        return self in _RECALCULABLE_CLASSES

    @property
    def requires_install(self) -> bool:
        """True only for the one class that should trigger a Phase-A network window."""
        return self is ErrorClass.MISSING_DEPENDENCY


# Defined as module-level frozensets so the membership test is O(1) and the
# taxonomy's two halves are auditable at a glance.
_RECALCULABLE_CLASSES: frozenset[ErrorClass] = frozenset(
    {
        ErrorClass.MISSING_DEPENDENCY,
        ErrorClass.SYNTAX_ERROR,
        ErrorClass.RUNTIME_EXCEPTION,
        ErrorClass.CONTRACT_VIOLATION,
        ErrorClass.TIMEOUT,
        ErrorClass.SYNTHESIS_FAILED,
    }
)

_HALTING_CLASSES: frozenset[ErrorClass] = frozenset(
    {
        ErrorClass.INSTALL_FAILED,
        ErrorClass.PERMISSION_DENIED,
        ErrorClass.AUTH_FAILURE,
        ErrorClass.RESOURCE_EXHAUSTED,
        ErrorClass.NETWORK_DENIED,
    }
)


# Signature normalisers: strip the volatile parts of an error string so that
# "the same failure" hashes to the same value across runs. This powers rut
# detection (trap #5: the recalc loop regenerating identical broken code) and
# the abstracted, scrub-safe error signatures we are allowed to centralise.
# Order matters — most specific patterns first, blanket digit-strip last.
_SIGNATURE_NORMALISERS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"0x[0-9a-fA-F]+"), "0xADDR"),         # memory addresses
    (re.compile(r"line\s+\d+"), "line N"),             # traceback line numbers
    (re.compile(r"'[^']*\.py'"), "'FILE.py'"),         # quoted file paths
    (re.compile(r"/[^\s'\"]+"), "PATH"),               # bare absolute paths
    (re.compile(r"\b\d+\b"), "N"),                      # residual integers
]


def normalise_message(message: str) -> str:
    """Reduce an error message to a stable, content-bearing skeleton.

    Used both for rut detection and for producing the abstracted error signature
    that is safe to share with the crowd-knowledge layer (no paths, no literals).
    """
    out = message.strip()
    for pattern, replacement in _SIGNATURE_NORMALISERS:
        out = pattern.sub(replacement, out)
    return out


def compute_signature(error_class: ErrorClass, exception_type: str | None, message: str) -> str:
    """Stable 16-char hash identifying 'the same failure' for dedup / escalation."""
    basis = f"{error_class.value}|{exception_type or ''}|{normalise_message(message)}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


class ErrorRecord(BaseModel):
    """One captured failure, classified and signed.

    Frozen on purpose: records are facts about the past and accumulate into the
    append-only ``error_history`` channel (trap #4). Nothing should mutate a record
    after capture.
    """

    model_config = ConfigDict(frozen=True)

    error_class: ErrorClass
    message: str = Field(..., description="Cleaned, human-readable summary of the failure.")
    exception_type: str | None = Field(
        default=None, description="Python exception name, e.g. 'ModuleNotFoundError'."
    )
    missing_module: str | None = Field(
        default=None,
        description="Import name extracted for dependency healing, e.g. 'cv2' (NOT the pip name).",
    )
    iteration: int = Field(default=0, description="Which recalc cycle produced this record.")
    signature: str = Field(..., description="Stable hash of (class, type, normalised message).")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Local-only forensic detail. Never leaves the machine: it routinely contains
    # absolute paths, tokens and internal endpoints. The scrubbing layer reads
    # `signature`/`message`, never this field, when deciding what may be shared.
    raw_stderr: str | None = Field(
        default=None, exclude=True, description="Full stderr — local diagnostics only, never uploaded."
    )

    @classmethod
    def create(
        cls,
        *,
        error_class: ErrorClass,
        message: str,
        exception_type: str | None = None,
        missing_module: str | None = None,
        raw_stderr: str | None = None,
        iteration: int = 0,
    ) -> "ErrorRecord":
        """Preferred constructor — computes the signature so call sites never forget."""
        return cls(
            error_class=error_class,
            message=message,
            exception_type=exception_type,
            missing_module=missing_module,
            raw_stderr=raw_stderr,
            iteration=iteration,
            signature=compute_signature(error_class, exception_type, message),
        )
