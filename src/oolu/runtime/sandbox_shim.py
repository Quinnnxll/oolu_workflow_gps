"""In-container result shim — the PRODUCER side of the runtime contract.

This file is copied verbatim into every sandbox container (as ``_oolu_runtime.py``)
and imported by the synthesized script:

    from _oolu_runtime import emit_result

It is the single channel by which a script returns its answer to the host. Free-form
stdout is unreliable — logging, library warnings and stray prints all interleave —
so the real answer is wrapped between collision-resistant sentinels as a JSON
envelope, and the host extracts *only* that block (trap #7).

HARD CONSTRAINT: pure standard library, no third-party imports, no package-relative
imports. The container may be a barebones python image with nothing installed (and
in Phase B it has no network to install anything), yet a script must ALWAYS be able
to report its result or its failure. If this file ever grows a non-stdlib import,
the whole "always able to phone home" guarantee breaks.

The host-side parser (``runtime/contract.py``) imports the sentinels and version
from THIS module, so the two sides are physically incapable of drifting apart.
"""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any

# Bump in lockstep with any change to the envelope shape. The version travels both
# inside the sentinel tag (so a mismatched harness is detectable from the raw text)
# and inside the JSON envelope (so a successful parse can still reject a bad shape).
PROTOCOL_VERSION = 1

# A fixed, namespaced, high-entropy tag. Fixed (not negotiated) so both sides agree
# with zero round-trips; high-entropy so it cannot plausibly collide with real
# program output. The leading/trailing newlines guarantee the sentinels sit on their
# own lines even if user code printed without a trailing newline just before.
_SENTINEL_TAG = "OOLU_RESULT_v1_b7c1f4e2a9"
RESULT_BEGIN = f"\n>>>>>{_SENTINEL_TAG}:BEGIN>>>>>\n"
RESULT_END = f"\n<<<<<{_SENTINEL_TAG}:END<<<<<\n"


def _write_envelope(envelope: dict[str, Any]) -> None:
    """Serialize and frame one envelope on stdout, flushing around it.

    ``default=str`` is a deliberate safety net: a script that returns a datetime, a
    Path, or a numpy scalar should degrade to a stringified value rather than crash
    the emit and turn a real answer into a contract violation. Flushing before and
    after keeps ordering sane relative to user prints and ensures the block survives
    even if the process is killed immediately afterward.
    """
    payload = json.dumps(envelope, ensure_ascii=False, default=str)
    sys.stdout.flush()
    sys.stdout.write(RESULT_BEGIN)
    sys.stdout.write(payload)
    sys.stdout.write(RESULT_END)
    sys.stdout.flush()


def emit_result(value: Any) -> None:
    """Report the script's final answer.

    Accepts any JSON-serializable value. Non-dict values are wrapped as
    ``{"result": value}`` so the host always receives an object — keeping the
    parsed ``contract_payload`` a predictable dict on the consumer side.
    """
    payload = value if isinstance(value, dict) else {"result": value}
    _write_envelope({"v": PROTOCOL_VERSION, "status": "ok", "payload": payload})


def emit_error(message: str, *, kind: str | None = None, details: Any = None) -> None:
    """Report a *structured* failure the script detected itself.

    This is NOT a contract violation — the contract worked; the script simply has
    bad news. The host distinguishes "script reported an error" (this) from "no
    parseable result at all" (a real violation) and routes them differently.
    """
    error: dict[str, Any] = {"message": message}
    if kind is not None:
        error["kind"] = kind
    if details is not None:
        error["details"] = details
    _write_envelope({"v": PROTOCOL_VERSION, "status": "error", "error": error})


def report_exception(exc: BaseException) -> None:
    """Convenience for the entrypoint: turn an uncaught exception into an error
    envelope carrying the exception type and a trimmed traceback tail."""
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    emit_error(
        str(exc) or exc.__class__.__name__,
        kind=exc.__class__.__name__,
        details={"traceback_tail": tb[-2000:]},
    )
