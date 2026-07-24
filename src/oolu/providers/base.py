"""Shared provider-adapter machinery.

Every adapter goes through the same pipeline for an authenticated request:
capability discovery, a local rate limiter and spend budget, a request id and
idempotency key, retries with classified errors, and credentials minted from the
vault only at call time. ``authenticated_call`` is the uniform entry point the
contract suite exercises against every provider.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from .errors import (
    RETRYABLE_STATUSES,
    BudgetExceeded,
    ProviderError,
    RateLimited,
    RevokedCredential,
    classify_status,
)
from .vault import CredentialRef, SecretVault


@dataclass(frozen=True)
class ProviderResponse:
    status: int
    json: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class HttpTransport(Protocol):
    """The injected HTTP boundary. A real transport wraps an HTTP client; tests
    inject a sandbox/remote-mock transport."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> ProviderResponse: ...


@dataclass(frozen=True)
class RequestContext:
    request_id: str = field(default_factory=lambda: uuid4().hex)
    idempotency_key: str | None = None


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    retryable_statuses: frozenset[int] = RETRYABLE_STATUSES


def _default_backoff(seconds: float) -> None:
    """The real wait between retries. A module-level seam (late-bound at
    the call site) so offline tests neutralize it in one place — the
    old default was a silent no-op, which meant every production retry
    fired back-to-back into the very 429 it was retrying."""
    time.sleep(seconds)


class RateLimiter:
    """A token-bucket limiter. ``now`` is injectable for deterministic tests."""

    def __init__(
        self,
        capacity: float,
        refill_per_second: float,
        *,
        clock: Callable[[], float] | None = None,
    ):
        import time

        self._capacity = capacity
        self._refill = refill_per_second
        self._tokens = capacity
        self._clock = clock or time.monotonic
        self._updated = self._clock()

    def acquire(self, amount: float = 1.0, *, now: float | None = None) -> None:
        moment = now if now is not None else self._clock()
        elapsed = max(0.0, moment - self._updated)
        self._tokens = min(self._capacity, self._tokens + elapsed * self._refill)
        self._updated = moment
        if self._tokens < amount:
            raise RateLimited("local rate limit exceeded")
        self._tokens -= amount


class Budget:
    def __init__(self, limit: float):
        self._limit = limit
        self.spent = 0.0

    def charge(self, amount: float) -> None:
        if self.spent + amount > self._limit:
            raise BudgetExceeded(
                f"budget {self._limit} would be exceeded (spent {self.spent}, +{amount})"
            )
        self.spent += amount


class BaseProviderAdapter:
    """Common authenticated-request pipeline shared by every provider adapter."""

    name: str = "provider"

    def __init__(
        self,
        *,
        vault: SecretVault,
        transport: HttpTransport,
        base_url: str,
        retry: RetryPolicy | None = None,
        rate_limiter: RateLimiter | None = None,
        budget: Budget | None = None,
        sleep: Callable[[float], None] | None = None,
    ):
        self._vault = vault
        self._transport = transport
        self._base_url = base_url.rstrip("/")
        self._retry = retry or RetryPolicy()
        self._rate_limiter = rate_limiter
        self._budget = budget
        # None = the module's real backoff, resolved at call time so a
        # test can neutralize _default_backoff without touching callers.
        self._sleep = sleep
        # Audit log holds request metadata only — never headers or secrets.
        self.audit: list[dict[str, Any]] = []
        self._idempotency_cache: dict[str, dict[str, Any]] = {}

    # Subclasses provide these.
    def _primary_ref(self) -> CredentialRef:  # pragma: no cover - abstract
        raise NotImplementedError

    def _auth_headers(self) -> dict[str, str]:  # pragma: no cover - abstract
        raise NotImplementedError

    def capabilities(self) -> frozenset[str]:  # pragma: no cover - abstract
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    def authenticated_call(
        self,
        *,
        method: str = "GET",
        path: str = "/",
        body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        cost: float = 1.0,
    ) -> dict[str, Any]:
        # A revoked credential never reaches the transport.
        if self._vault.is_revoked(self._primary_ref()):
            raise RevokedCredential(f"{self.name}: credential is revoked")

        # Idempotent replays return the stored result without re-calling.
        if idempotency_key is not None and idempotency_key in self._idempotency_cache:
            return self._idempotency_cache[idempotency_key]

        if self._rate_limiter is not None:
            self._rate_limiter.acquire()
        if self._budget is not None:
            self._budget.charge(cost)

        context = RequestContext(idempotency_key=idempotency_key)
        url = f"{self._base_url}{path}"
        headers = {
            **self._auth_headers(),
            "X-Request-Id": context.request_id,
        }
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key

        result = self._send_with_retries(method, url, headers, body, context)
        if idempotency_key is not None:
            self._idempotency_cache[idempotency_key] = result
        return result

    def stream_call(  # pragma: no cover - needs a live streaming server
        self, *, method: str = "POST", path: str = "/", body: dict[str, Any] | None = None
    ):
        """Yield the raw SSE lines of a streaming provider call.

        Reuses this adapter's auth headers and base URL, but bypasses the
        retry/idempotency pipeline (a partially-streamed response cannot be
        replayed). Raises if the injected transport has no ``stream`` method."""
        if self._vault.is_revoked(self._primary_ref()):
            raise RevokedCredential(f"{self.name}: credential is revoked")
        stream = getattr(self._transport, "stream", None)
        if not callable(stream):
            raise ProviderError(f"{self.name}: transport does not stream")
        url = f"{self._base_url}{path}"
        headers = {**self._auth_headers(), "Accept": "text/event-stream"}
        return stream(method, url, headers=headers, body=body)

    def _send_with_retries(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any] | None,
        context: RequestContext,
    ) -> dict[str, Any]:
        last_error: ProviderError | None = None
        for attempt in range(1, self._retry.max_attempts + 1):
            response = self._transport.request(method, url, headers=headers, body=body)
            # Record only non-sensitive metadata.
            self.audit.append(
                {
                    "request_id": context.request_id,
                    "idempotency_key": context.idempotency_key,
                    "method": method,
                    "url": url,
                    "status": response.status,
                    "attempt": attempt,
                }
            )
            error_cls = classify_status(response.status)
            if error_cls is None:
                return response.json
            if (
                response.status in self._retry.retryable_statuses
                and attempt < self._retry.max_attempts
            ):
                last_error = error_cls(f"{self.name}: status {response.status}")
                (self._sleep or _default_backoff)(float(attempt))
                continue
            message = self._vault.redact(str(response.json) or "")
            raise error_cls(f"{self.name}: status {response.status}: {message}")
        assert last_error is not None
        raise last_error
