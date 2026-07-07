"""The HTTP ActionExecutor — the engine's first pair of hands.

Executes the ``http`` adapter's ``get`` operation (the starter pack's
"Fetch JSON from an API" and every skill like it) against the real network,
inside a policy that fails closed:

* **GET only.** Reading the web is the reversible floor; writes arrive as
  separate operations with their own risk class and confirmation gates.
* **SSRF guard, always on.** Every hostname is resolved and every resolved
  address must be public: loopback, RFC1918, link-local (cloud metadata),
  and reserved ranges are refused — a skill can never be pointed at the
  machine's own services or its network's insides. An optional allowlist
  narrows further; ``allow_private`` exists for offline tests and demos
  and is never the shipped default.
* **Redirects re-checked.** Redirects are followed manually so every hop
  passes the same guard — a public URL that bounces to 169.254.169.254
  dies at the bounce.
* **Bounded reads.** The download stops at ``max_bytes``; the evidence kept
  on the run is truncated to ``evidence_bytes`` so audit logs stay sane.

Outcome evidence carries what the user actually wants to see — status,
final URL, content type, and the (bounded) body — which the engine surfaces
on the run result.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

from .models import ActionEvent, ExecutionOutcome, ExecutionStatus

_MAX_REDIRECTS = 3


@dataclass(frozen=True)
class HttpExecutionPolicy:
    """What this machine permits a skill to fetch."""

    allow_hosts: frozenset[str] = frozenset()  # empty = any PUBLIC host
    timeout_s: float = 20.0
    max_bytes: int = 1_000_000
    evidence_bytes: int = 4_096
    allow_private: bool = False  # offline tests/demos only — never shipped on

    def host_allowed(self, host: str) -> bool:
        if not self.allow_hosts:
            return True
        host = host.lower()
        return any(
            host == allowed or host.endswith("." + allowed)
            for allowed in self.allow_hosts
        )


def _addresses_are_public(host: str, resolver: Callable[[str], list[str]]) -> bool:
    """True iff every address the name resolves to is globally routable."""
    try:
        addresses = resolver(host)
    except OSError:
        return False
    if not addresses:
        return False
    for raw in addresses:
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            return False
        if not address.is_global:
            return False
    return True


def _default_resolver(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, None)]


class HttpActionExecutor:
    """GET for the ``http`` adapter, inside the policy above."""

    name = "http"

    def __init__(
        self,
        policy: HttpExecutionPolicy | None = None,
        *,
        client: Any = None,  # httpx.Client; injectable for offline tests
        resolver: Callable[[str], list[str]] | None = None,
    ):
        self._policy = policy or HttpExecutionPolicy()
        self._client = client
        self._resolver = resolver or _default_resolver
        self._completed: dict[str, ExecutionOutcome] = {}
        self._lock = threading.RLock()

    def capabilities(self) -> frozenset[str]:
        return frozenset({"get"})

    def cancel(self, idempotency_key: str) -> None:
        # Requests are short-lived and bounded; there is nothing to reap.
        return None

    # ------------------------------------------------------------------ #
    def execute(self, action: ActionEvent, *, idempotency_key: str) -> ExecutionOutcome:
        with self._lock:
            if idempotency_key in self._completed:
                return self._completed[idempotency_key]

        if action.adapter != self.name or action.operation != "get":
            return self._done(
                action, idempotency_key, ExecutionStatus.BLOCKED,
                error="unsupported HTTP action",
            )
        url = action.parameters.get("url")
        if not isinstance(url, str) or not url.strip():
            return self._done(
                action, idempotency_key, ExecutionStatus.FAILED,
                error="no url provided — say which address to fetch",
            )
        headers = {
            str(k): str(v)
            for k, v in (action.parameters.get("headers") or {}).items()
        }

        try:
            outcome = self._fetch(action, idempotency_key, url.strip(), headers)
        except Exception as exc:  # noqa: BLE001 - a dead network is a failed
            # action with a reason, never a crashed run.
            outcome = self._done(
                action, idempotency_key, ExecutionStatus.FAILED,
                error=f"GET failed: {exc}",
            )
        return outcome

    # ------------------------------------------------------------------ #
    def _guard(self, url: str) -> str | None:
        """The policy verdict for one URL. None = allowed, else the reason."""
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return f"only http(s) URLs are allowed, not {parts.scheme!r}"
        host = parts.hostname
        if not host:
            return "the URL has no host"
        if not self._policy.host_allowed(host):
            return f"host {host!r} is not on this machine's allowlist"
        if self._policy.allow_private:
            return None
        if not _addresses_are_public(host, self._resolver):
            return (
                f"host {host!r} does not resolve to a public address — "
                "skills may not reach this machine's own network"
            )
        return None

    def _fetch(
        self, action: ActionEvent, key: str, url: str, headers: dict[str, str]
    ) -> ExecutionOutcome:
        import httpx

        client = self._client
        if client is None:
            client = self._client = httpx.Client(
                trust_env=True, timeout=self._policy.timeout_s
            )

        current = url
        for _hop in range(_MAX_REDIRECTS + 1):
            reason = self._guard(current)
            if reason is not None:
                return self._done(
                    action, key, ExecutionStatus.BLOCKED, error=reason
                )
            # Redirects are followed manually so EVERY hop re-passes the
            # guard; stream so the size cap cuts the read, not the memory.
            with client.stream(
                "GET", current, headers=headers, follow_redirects=False
            ) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        return self._done(
                            action, key, ExecutionStatus.FAILED,
                            error="redirect without a location",
                        )
                    current = urljoin(current, location)
                    continue
                body = bytearray()
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) >= self._policy.max_bytes:
                        break
                truncated = len(body) > self._policy.evidence_bytes
                text = bytes(body[: self._policy.evidence_bytes]).decode(
                    "utf-8", errors="replace"
                )
                ok = 200 <= response.status_code < 300
                return self._done(
                    action, key,
                    ExecutionStatus.SUCCEEDED if ok else ExecutionStatus.FAILED,
                    error=None if ok else f"GET answered {response.status_code}",
                    evidence={
                        "status": response.status_code,
                        "url": current,
                        "content_type": response.headers.get("content-type", ""),
                        "body": text,
                        "truncated": truncated,
                    },
                )
        return self._done(
            action, key, ExecutionStatus.FAILED, error="too many redirects"
        )

    def _done(
        self,
        action: ActionEvent,
        key: str,
        status: ExecutionStatus,
        *,
        error: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> ExecutionOutcome:
        outcome = ExecutionOutcome(
            idempotency_key=key,
            skill_id=str(action.parameters.get("skill_id", "http")),
            status=status,
            error=error,
            evidence=evidence or {},
            completed_at=datetime.now(UTC),
        )
        with self._lock:
            self._completed[key] = outcome
        return outcome
