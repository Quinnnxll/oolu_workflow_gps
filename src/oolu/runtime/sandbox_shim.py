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


# --------------------------------------------------------------------------- #
# The web hand — the PRODUCER side of the brokered exchange.                   #
# --------------------------------------------------------------------------- #
# The sandbox has no network, ever. When the node carries a web grant the
# host mounts an exchange directory (announced via $OOLU_WEB_EXCHANGE) and a
# host-side broker answers request files through the machine's guarded HTTP
# executor — the node's granted hosts, the SSRF guard, every redirect hop
# re-checked. These constants MUST mirror runtime/webhand.py (a test pins
# them); they are duplicated because this file may import nothing.
_WEB_EXCHANGE_ENV = "OOLU_WEB_EXCHANGE"
_WEB_REQUEST_SUFFIX = ".req.json"
_WEB_RESPONSE_SUFFIX = ".rsp.json"
_WEB_POLL_S = 0.05


class WebGrantError(RuntimeError):
    """The script asked for the web and this run carries no way to it."""


def http_request(
    url: str,
    *,
    method: str = "GET",
    headers: dict | None = None,
    body: Any = None,
    timeout_s: float = 60.0,
) -> dict:
    """One HTTP exchange through the host-side broker — the node's web hand.

    Returns ``{"status", "url", "content_type", "body", "truncated",
    "error"}``. A refused or failed call comes back with ``status`` 0 and
    the reason in ``error`` (e.g. the host is outside the node's granted
    hosts) — read it and either work around it or emit_result the honest
    partial answer. ``body`` may be a str, or any JSON-serializable value
    (sent as JSON with a content-type header unless one is already set).

    Raises :class:`WebGrantError` only when the run has no exchange at
    all — the node has no web grant, so no broker is listening.
    """
    import os
    import time
    import uuid

    exchange = os.environ.get(_WEB_EXCHANGE_ENV)
    if not exchange or not os.path.isdir(exchange):
        raise WebGrantError(
            "this node has no web grant — its responsible human can grant "
            "named hosts on the node's account (Work -> the node -> "
            "network), and the run will find the web hand mounted"
        )
    payload_body = body
    request_headers = dict(headers or {})
    if body is not None and not isinstance(body, str):
        payload_body = json.dumps(body, ensure_ascii=False, default=str)
        if not any(k.lower() == "content-type" for k in request_headers):
            request_headers["Content-Type"] = "application/json"
    call_id = uuid.uuid4().hex
    request = {
        "id": call_id,
        "method": str(method or "GET").upper(),
        "url": str(url),
        "headers": request_headers,
        "body": payload_body,
    }
    # Atomic hand-off: write whole, then rename onto the watched suffix.
    temp = os.path.join(exchange, call_id + ".req.tmp")
    final = os.path.join(exchange, call_id + _WEB_REQUEST_SUFFIX)
    with open(temp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(request, ensure_ascii=False))
    os.replace(temp, final)

    answer_path = os.path.join(exchange, call_id + _WEB_RESPONSE_SUFFIX)
    deadline = time.monotonic() + max(float(timeout_s), _WEB_POLL_S)
    while time.monotonic() < deadline:
        if os.path.exists(answer_path):
            with open(answer_path, encoding="utf-8") as fh:
                return json.load(fh)
        time.sleep(_WEB_POLL_S)
    return {
        "status": 0,
        "url": str(url),
        "content_type": "",
        "body": "",
        "truncated": False,
        "error": f"the web broker did not answer within {timeout_s:.0f}s",
    }
