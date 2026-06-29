"""Classification and dependency resolution — raw failures become routed decisions.

Two jobs that belong together because the first produces the input to the second:

  1. CLASSIFIER. ``classify(result)`` turns a raw ``ExecutionResult`` (exit code,
     stderr, contract state, phase) into a single ``ErrorRecord`` carrying an
     ``ErrorClass``. This is the one place the taxonomy is applied, so the
     recalculable-vs-halt decision is made consistently. The classifier never loops
     or retries; it only labels.

  2. RESOLVER. ``resolve(import_name)`` maps a Python import name to the pip/uv
     PACKAGE name (trap #6: ``cv2`` -> ``opencv-python``). It consults, in order, a
     curated builtin map, then learned/crowd ``DependencyHint``s (passed in by the
     graph from the knowledge layer), then an identity fallback.

``plan_dependency_fix`` bridges the two: given a classified MISSING_DEPENDENCY
record, it returns the resolution the recalc layer should install next cycle.

Depends only on ``models`` and ``contract`` — no Docker, no LLM — so it is fully
unit-testable in isolation.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

from ..models import (
    DependencyHint,
    ErrorClass,
    ErrorRecord,
    ExecutionResult,
    KnowledgeSource,
    Phase,
)
from .contract import ContractStatus, parse_stdout

# --------------------------------------------------------------------------- #
# CLASSIFIER                                                                   #
# --------------------------------------------------------------------------- #

# Final-line matcher for a Python traceback: ``ExceptionType: message``. We scan
# stderr bottom-up and take the first exception-shaped line.
_EXC_LINE = re.compile(
    r"^(?P<type>[A-Za-z_][\w.]*(?:Error|Exception|Interrupt|Exit|Warning|error))\b:?[ \t]?(?P<msg>.*)$"
)
_NO_MODULE = re.compile(r"No module named ['\"]?([\w.]+)['\"]?")

# Exception-type-name -> ErrorClass, for the names that map unambiguously.
_TYPE_TO_CLASS: dict[str, ErrorClass] = {
    "ModuleNotFoundError": ErrorClass.MISSING_DEPENDENCY,
    "ImportError": ErrorClass.MISSING_DEPENDENCY,
    "SyntaxError": ErrorClass.SYNTAX_ERROR,
    "IndentationError": ErrorClass.SYNTAX_ERROR,
    "TabError": ErrorClass.SYNTAX_ERROR,
    "PermissionError": ErrorClass.PERMISSION_DENIED,
    "MemoryError": ErrorClass.RESOURCE_EXHAUSTED,
}

# Message substrings that betray a network failure (often surfaced as a generic
# ConnectionError or a urllib3 wrapper, so type-name matching alone misses them).
_NETWORK_MARKERS = (
    "name or service not known",
    "temporary failure in name resolution",
    "network is unreachable",
    "no route to host",
    "connection refused",
    "failed to establish a new connection",
    "max retries exceeded",
    "nodename nor servname provided",
)
# Auth signals from called APIs.
_AUTH_MARKERS = (
    "401",
    "403",
    "unauthorized",
    "forbidden",
    "invalid api key",
    "invalid_api_key",
    "authentication failed",
    "authenticationerror",
)
# Install-failure signals from pip/uv.
_INSTALL_NODIST_MARKERS = (
    "no matching distribution",
    "could not find a version",
    "no versions found",
    "not found in the package registry",
)
_INSTALL_NETWORK_MARKERS = (
    "could not resolve host",
    "failed to establish",
    "temporary failure in name resolution",
    "network is unreachable",
)


def _stderr_tail(text: str, limit: int = 400) -> str:
    text = (text or "").strip()
    return text[-limit:]


def _extract_exception(stderr: str) -> tuple[str | None, str | None]:
    """Return (exception_type, message) from the last traceback line, if present."""
    for line in reversed((stderr or "").splitlines()):
        line = line.strip()
        if not line:
            continue
        m = _EXC_LINE.match(line)
        if m:
            return m.group("type"), (m.group("msg") or "").strip()
    return None, None


def _extract_module(text: str) -> str | None:
    m = _NO_MODULE.search(text or "")
    return m.group(1) if m else None


def _looks_network(text_lower: str) -> bool:
    return any(marker in text_lower for marker in _NETWORK_MARKERS)


def _looks_auth(text_lower: str) -> bool:
    return any(marker in text_lower for marker in _AUTH_MARKERS)


def _type_to_class(type_name: str | None, message: str) -> ErrorClass | None:
    """Map an exception type (+ its message) to a class, where recognizable."""
    if type_name and type_name in _TYPE_TO_CLASS:
        return _TYPE_TO_CLASS[type_name]
    low = f"{type_name or ''} {message}".lower()
    if type_name and "auth" in type_name.lower():
        return ErrorClass.AUTH_FAILURE
    if _looks_auth(low):
        return ErrorClass.AUTH_FAILURE
    if _looks_network(low):
        return ErrorClass.NETWORK_DENIED
    if "permission denied" in low:
        return ErrorClass.PERMISSION_DENIED
    if "cannot allocate memory" in low or "out of memory" in low:
        return ErrorClass.RESOURCE_EXHAUSTED
    return None


def _classify_install(result: ExecutionResult, iteration: int) -> ErrorRecord:
    low = (result.stderr or "").lower()
    if result.timed_out:
        return ErrorRecord.create(
            error_class=ErrorClass.TIMEOUT,
            message="dependency install exceeded its time budget",
            raw_stderr=result.stderr,
            iteration=iteration,
        )
    if any(m in low for m in _INSTALL_NETWORK_MARKERS):
        cls = ErrorClass.NETWORK_DENIED
        msg = "dependency index unreachable during install"
    else:
        cls = ErrorClass.INSTALL_FAILED
        msg = "package could not be installed"
        if any(m in low for m in _INSTALL_NODIST_MARKERS):
            msg = "no installable distribution for requested package"
    return ErrorRecord.create(
        error_class=cls, message=msg, raw_stderr=result.stderr, iteration=iteration
    )


def classify(result: ExecutionResult, *, iteration: int = 0) -> ErrorRecord | None:
    """Label a failed execution. Returns None if the result actually succeeded.

    Classification only — no retry logic, no taxonomy decisions duplicated
    elsewhere. The graph's edges decide what to *do* with the label.
    """
    if result.succeeded:
        return None

    if result.phase is Phase.INSTALL:
        return _classify_install(result, iteration)

    # --- Phase B (EXECUTE) ---
    if result.timed_out:
        return ErrorRecord.create(
            error_class=ErrorClass.TIMEOUT,
            message="execution exceeded the wall-clock limit",
            raw_stderr=result.stderr,
            iteration=iteration,
        )

    # A script can self-report a STRUCTURED error envelope on stdout even when it
    # exits non-zero: the Docker entrypoint converts an uncaught exception into an
    # envelope (carrying the exception type + message) and exits 1, leaving stderr
    # EMPTY. That envelope is the most authoritative signal, so consult it before any
    # stderr/exit-code heuristics — otherwise a Docker ModuleNotFoundError would be
    # mislabeled UNKNOWN ("non-zero exit with no diagnostics") and never healed.
    contract = parse_stdout(result.stdout)
    if contract.found and contract.status is ContractStatus.ERROR:
        kind = contract.error_kind
        message = contract.error_message or "script reported an error"
        cls = _type_to_class(kind, message) or ErrorClass.RUNTIME_EXCEPTION
        module = (
            _extract_module(message) if cls is ErrorClass.MISSING_DEPENDENCY else None
        )
        return ErrorRecord.create(
            error_class=cls,
            message=message,
            exception_type=kind,
            missing_module=module,
            raw_stderr=result.stderr,
            iteration=iteration,
        )

    exc_type, exc_msg = _extract_exception(result.stderr)
    stderr_low = (result.stderr or "").lower()

    # Missing dependency is the highest-value branch — extract the module so the
    # resolver can act on it.
    if (
        exc_type in ("ModuleNotFoundError", "ImportError")
        or "no module named" in stderr_low
    ):
        module = _extract_module(result.stderr)
        return ErrorRecord.create(
            error_class=ErrorClass.MISSING_DEPENDENCY,
            message=f"No module named '{module}'" if module else "missing dependency",
            exception_type=exc_type or "ModuleNotFoundError",
            missing_module=module,
            raw_stderr=result.stderr,
            iteration=iteration,
        )

    # Other recognizable exception types / message signals.
    mapped = _type_to_class(exc_type, exc_msg or "")
    if mapped is not None:
        return ErrorRecord.create(
            error_class=mapped,
            message=exc_msg or mapped.value.replace("_", " "),
            exception_type=exc_type,
            raw_stderr=result.stderr,
            iteration=iteration,
        )

    # A non-zero exit with a traceback we couldn't pin down => generic runtime fault.
    if result.exit_code != 0:
        if exc_type:
            return ErrorRecord.create(
                error_class=ErrorClass.RUNTIME_EXCEPTION,
                message=exc_msg or f"{exc_type} raised",
                exception_type=exc_type,
                raw_stderr=result.stderr,
                iteration=iteration,
            )
        if result.stderr.strip():
            return ErrorRecord.create(
                error_class=ErrorClass.RUNTIME_EXCEPTION,
                message=_stderr_tail(result.stderr),
                raw_stderr=result.stderr,
                iteration=iteration,
            )
        return ErrorRecord.create(
            error_class=ErrorClass.UNKNOWN,
            message=f"non-zero exit ({result.exit_code}) with no diagnostics",
            raw_stderr=result.stderr,
            iteration=iteration,
        )

    # Not a success, and no structured error envelope (handled above) and no usable
    # stderr/exit signal => a contract problem. The script emitted nothing parseable;
    # name the violation reason from the already-parsed contract.
    reason = contract.violation.value if contract.violation else "no result"
    return ErrorRecord.create(
        error_class=ErrorClass.CONTRACT_VIOLATION,
        message=f"no valid result emitted ({reason})",
        raw_stderr=result.stderr,
        iteration=iteration,
    )


# --------------------------------------------------------------------------- #
# RESOLVER                                                                     #
# --------------------------------------------------------------------------- #

# Curated import-name -> package-name mismatches. The painful-to-rediscover set;
# this is exactly the BUILTIN knowledge the crowd layer would otherwise re-derive
# per user (trap #6). Identity mappings (requests -> requests) are intentionally
# omitted — they are handled by the fallback.
_BUILTIN_IMPORT_TO_PACKAGE: dict[str, str] = {
    "cv2": "opencv-python",
    "PIL": "pillow",
    "bs4": "beautifulsoup4",
    "yaml": "pyyaml",
    "sklearn": "scikit-learn",
    "skimage": "scikit-image",
    "OpenSSL": "pyopenssl",
    "dateutil": "python-dateutil",
    "dotenv": "python-dotenv",
    "jwt": "pyjwt",
    "serial": "pyserial",
    "Crypto": "pycryptodome",
    "google.protobuf": "protobuf",
    "win32api": "pywin32",
    "win32com": "pywin32",
    "magic": "python-magic",
    "fitz": "pymupdf",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "slugify": "python-slugify",
    "attr": "attrs",
    "psycopg2": "psycopg2-binary",
    "MySQLdb": "mysqlclient",
    "zmq": "pyzmq",
    "nacl": "pynacl",
    "wx": "wxpython",
    "cairo": "pycairo",
    "gi": "pygobject",
    "usb": "pyusb",
}

# Below this trust score, a learned/crowd hint is not trusted over the identity
# fallback. Set just above the fresh-crowd prior (0.50) so a zero-evidence crowd
# suggestion is rejected, while a learned-local hint (0.60) or any hint with at
# least one corroborating success clears the bar.
_HINT_TRUST_FLOOR = 0.55


class DependencyResolution(BaseModel):
    """The chosen package to install for a missing import, with provenance."""

    model_config = ConfigDict(frozen=True)

    import_name: str
    package_name: str
    source: KnowledgeSource
    pinned_version: str | None = None
    confidence: float = Field(..., ge=0.0, le=1.0)
    is_fallback: bool = Field(
        default=False,
        description="True when we simply assumed package == import (no known mapping).",
    )


def resolve(
    import_name: str, hints: tuple[DependencyHint, ...] = ()
) -> DependencyResolution:
    """Map a Python import name to the package to install.

    Priority: curated builtin map (authoritative) > trusted learned/crowd hint >
    identity fallback (assume the package name equals the import name).
    """
    name = import_name.strip()
    top = name.split(".")[0]

    # 1. Builtin: try the full dotted name first (e.g. google.protobuf), then top-level.
    for key in (name, top):
        if key in _BUILTIN_IMPORT_TO_PACKAGE:
            return DependencyResolution(
                import_name=name,
                package_name=_BUILTIN_IMPORT_TO_PACKAGE[key],
                source=KnowledgeSource.BUILTIN,
                confidence=0.95,
            )

    # 2. Learned / crowd hints, best trust first, above the floor.
    matching = sorted(
        (h for h in hints if h.import_name in (name, top)),
        key=lambda h: h.trust_score,
        reverse=True,
    )
    for hint in matching:
        if hint.trust_score >= _HINT_TRUST_FLOOR:
            return DependencyResolution(
                import_name=name,
                package_name=hint.package_name,
                source=hint.source,
                pinned_version=hint.pinned_version,
                confidence=hint.trust_score,
            )

    # 3. Identity fallback — correct for the common case (requests, numpy, ...).
    return DependencyResolution(
        import_name=name,
        package_name=top,
        source=KnowledgeSource.LOCAL,
        confidence=0.4,
        is_fallback=True,
    )


def plan_dependency_fix(
    record: ErrorRecord, hints: tuple[DependencyHint, ...] = ()
) -> DependencyResolution | None:
    """Bridge classifier -> resolver: the package to install for a missing-dep record."""
    if (
        record.error_class is not ErrorClass.MISSING_DEPENDENCY
        or not record.missing_module
    ):
        return None
    return resolve(record.missing_module, hints)
