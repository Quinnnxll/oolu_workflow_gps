"""The web hand — how a network-severed sandbox reaches the web honestly.

The sandbox NEVER gets a network. What it gets, when (and only when) the
node carries a web grant, is a bind-mounted **exchange directory** and the
shim's ``http_request`` function: the script writes a request as a JSON
file, and this module's :class:`WebBroker` — a host-side thread watching
the same directory — answers it *through the machine's guarded HTTP
executor*. The threat model's sentence stays literally true: "the sandbox
stays severed; when a node needs the web it goes through the host-side
HTTP executor, which is the honest enforcement point."

What that buys, compared to attaching a network:

* **One wall, not two.** Every brokered call passes the exact walls http
  actions pass — the machine allowlist, the node's egress grant (or the
  open-web-minus-blocks regime for verified orgs), and the always-on SSRF
  guard, re-checked on every redirect hop. There is no second, weaker
  network policy to keep in sync.
* **Severance stays verifiable.** ``_sever_network``'s proof ("no networks
  attached") remains exactly as strong; a directory of JSON files is not a
  network interface, cannot reach metadata services, and cannot speak to
  anything the broker refuses to speak to.
* **An honest record.** The broker keeps every answered call (method, URL,
  status, the refusal reason if refused) in ``calls`` — the run's own log
  of what its code actually asked the world.

The wire format is deliberately dumb — files, atomic renames, polling —
because the producer is untrusted code in a barebones container with only
the standard library. Protocol constants live here and are imported by the
shim's producer side at BUILD time only (the shim file itself stays
dependency-free; the constants are duplicated there and pinned by a test).
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .backend import WebGrant

logger = logging.getLogger(__name__)

# The env var that tells the script where its exchange is mounted. Absence
# means "this node has no web grant" — the shim refuses in those words.
EXCHANGE_ENV = "OOLU_WEB_EXCHANGE"
# Where the exchange is mounted inside the Docker sandbox.
CONTAINER_EXCHANGE = "/oolu-web"

REQUEST_SUFFIX = ".req.json"
RESPONSE_SUFFIX = ".rsp.json"

# How often the broker looks for new request files. The shim polls at the
# same cadence for its answer, so a call's floor latency is ~2 ticks.
_POLL_S = 0.05

# A single request file larger than this is not a request — refuse unread.
_MAX_REQUEST_FILE = 1_048_576


# The host-side fetch the broker answers through: the guarded ``request``
# method of skills.http_adapter.HttpActionExecutor (or anything matching
# its signature and its dict answer).
GuardedFetch = Callable[..., dict[str, Any]]


@dataclass
class WebBroker:
    """Answers a sandbox's request files through the guarded HTTP hand.

    One broker serves one run: start it around Phase B with the node's
    grant, stop it after. It never raises out of its thread — every
    malformed, oversized, or over-budget request becomes a *written
    response* carrying the reason, because the untrusted side deserves
    words, not silence.
    """

    fetch: GuardedFetch
    grant: WebGrant
    # The credential resolver (V2): anything with ``resolve(ref) -> str``
    # — the durable vault in production, None on hosts without secrets.
    # Secrets exist transiently HERE only: resolved per call, minted
    # into a header, never written to the exchange, the call log, or
    # a response.
    secrets: Any | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    # ------------------------------------------------------------------ #
    def start(self, exchange: Path) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._serve, args=(Path(exchange),), daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # ------------------------------------------------------------------ #
    def _serve(self, exchange: Path) -> None:
        answered: set[str] = set()
        while not self._stop.is_set():
            try:
                self.sweep(exchange, answered)
            except Exception:  # noqa: BLE001 - the broker outlives one bad file
                logger.exception("web broker sweep failed")
            self._stop.wait(_POLL_S)
        # One last sweep so a request written just before Phase B ended
        # still gets its answer (the script may already be gone; harmless).
        try:
            self.sweep(exchange, answered)
        except Exception:  # noqa: BLE001
            logger.exception("web broker final sweep failed")

    def sweep(self, exchange: Path, answered: set[str]) -> None:
        """Answer every unanswered request file once. Public and callable
        without the thread so tests drive the broker deterministically."""
        for req_path in sorted(exchange.glob(f"*{REQUEST_SUFFIX}")):
            call_id = req_path.name[: -len(REQUEST_SUFFIX)]
            if call_id in answered:
                continue
            answered.add(call_id)
            response = self._answer(req_path)
            self._write_response(exchange, call_id, response)

    # ------------------------------------------------------------------ #
    def _answer(self, req_path: Path) -> dict[str, Any]:
        if len(self.calls) >= self.grant.max_calls:
            refusal = (
                f"this run already made {self.grant.max_calls} web calls — "
                "the per-run cap; finish with what you have"
            )
            self.calls.append({"url": "", "method": "", "error": refusal})
            return _error_response(refusal)
        try:
            if req_path.stat().st_size > _MAX_REQUEST_FILE:
                raise ValueError(f"request file exceeds {_MAX_REQUEST_FILE} bytes")
            request = json.loads(req_path.read_text("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("a request is a JSON object")
        except Exception as exc:  # noqa: BLE001 - a broken request gets words
            self.calls.append({"url": "", "method": "", "error": str(exc)})
            return _error_response(f"unreadable request: {exc}")

        method = str(request.get("method") or "GET")
        url = str(request.get("url") or "")
        headers = request.get("headers")
        headers = dict(headers) if isinstance(headers, dict) else None
        # The credential seam (V2): the node's stored key becomes this
        # host's auth header HERE, host-side, for granted hosts only —
        # the script never holds it, and its own header of the same
        # name never overrides the vault's (the secret channel is the
        # host's, not the sandbox's).
        auth = self._auth_headers(url)
        if auth:
            headers = {**(headers or {}), **auth}
        body = request.get("body")
        result = self.fetch(
            method,
            url,
            headers=headers,
            body=str(body) if body is not None else None,
            grant=self._grant_hosts(),
            blocked=self._blocked_hosts(),
        )
        self.calls.append(
            {
                "method": method.upper(),
                "url": url,
                "status": result.get("status"),
                "error": result.get("error"),
            }
        )
        return result

    def _auth_headers(self, url: str) -> dict[str, str]:
        """The vault-minted headers for this request's host — empty when
        the node holds no credential for it, the host isn't granted, or
        no resolver is standing. A ref that fails to resolve (revoked,
        lost) is skipped silently: the request proceeds bare and the API
        answers honestly."""
        if self.secrets is None or not self.grant.auth:
            return {}
        from urllib.parse import urlparse

        try:
            host = (urlparse(url).hostname or "").lower()
        except ValueError:
            return {}
        if not host:
            return {}
        granted = self._grant_hosts()
        if granted is not None and not any(
            host == g or host.endswith("." + g) for g in granted
        ):
            return {}
        out: dict[str, str] = {}
        for entry in self.grant.auth:
            bound = entry.host.lower()
            if host != bound and not host.endswith("." + bound):
                continue
            try:
                secret = self.secrets.resolve(entry.ref)
            except Exception:  # noqa: BLE001 - a dead ref never breaks the call
                continue
            value = f"{entry.scheme} {secret}" if entry.scheme else secret
            out[entry.header] = value
        return out

    def _grant_hosts(self) -> frozenset[str] | None:
        # Mirrors the http-action stamp exactly: open web means no allow
        # list; otherwise the grant ALWAYS applies (empty fails closed).
        if self.grant.open_web:
            return None
        return frozenset(h.lower() for h in self.grant.hosts)

    def _blocked_hosts(self) -> frozenset[str] | None:
        if not self.grant.open_web:
            return None
        return frozenset(h.lower() for h in self.grant.blocked_hosts)

    @staticmethod
    def _write_response(exchange: Path, call_id: str, payload: dict) -> None:
        # Atomic: the shim must never read a half-written answer.
        final = exchange / f"{call_id}{RESPONSE_SUFFIX}"
        temp = exchange / f"{call_id}.rsp.tmp"
        temp.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
        temp.replace(final)


def _error_response(reason: str) -> dict[str, Any]:
    return {
        "status": 0,
        "url": "",
        "content_type": "",
        "body": "",
        "truncated": False,
        "error": reason,
    }


class _NullServing:
    """The no-grant case: nothing to start, nothing to stop."""

    calls: list[dict[str, Any]] = []

    def __enter__(self) -> "_NullServing":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@dataclass
class _Serving:
    broker: WebBroker
    exchange: Path

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.broker.calls

    def __enter__(self) -> "_Serving":
        self.broker.start(self.exchange)
        return self

    def __exit__(self, *exc: object) -> None:
        self.broker.stop()


def serve_web(
    exchange: Path | None,
    grant: WebGrant | None,
    fetch: GuardedFetch | None,
    secrets: Any | None = None,
) -> "_Serving | _NullServing":
    """The one-liner backends wrap Phase B in: a live broker when the run
    carries a grant AND the host wired a guarded fetch; a silent no-op
    otherwise (the shim then refuses calls with the honest words).
    ``secrets`` (V2) is the credential resolver the broker mints auth
    headers from — absent, requests go out bare exactly as before."""
    if exchange is None or grant is None or fetch is None:
        return _NullServing()
    return _Serving(
        WebBroker(fetch=fetch, grant=grant, secrets=secrets), Path(exchange)
    )
