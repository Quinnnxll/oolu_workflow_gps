"""Provider integration error hierarchy and HTTP status classification.

Provider failures are classified into a small, stable vocabulary so callers (and
the shared contract suite) can react to *kinds* of failure — auth, revocation,
rate limiting, budget, transient unavailability, bad request — rather than raw
status codes, and so retry behaviour is decided uniformly across providers.
"""

from __future__ import annotations


class ProviderError(RuntimeError):
    """Base class for provider integration failures."""


class ProviderAuthError(ProviderError):
    """Authentication failed (e.g. 401) — the credential is invalid or expired."""


class TokenExpired(ProviderAuthError):
    """An access token is past its expiry and must be refreshed."""


class RevokedCredential(ProviderAuthError):
    """The credential has been revoked and must not be used."""


class InvalidCallback(ProviderError):
    """An OAuth callback failed validation (state mismatch, error param, etc.)."""


class RateLimited(ProviderError):
    """The provider (or the local limiter) rejected the request for rate reasons."""


class BudgetExceeded(ProviderError):
    """The configured spend budget would be exceeded by this request."""


class ProviderUnavailable(ProviderError):
    """A transient server-side failure (5xx) — typically retryable."""


class ProviderRequestError(ProviderError):
    """A non-retryable client error (4xx other than 401/403/429)."""


# Statuses worth retrying: rate limiting and transient server errors.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


def classify_status(status: int) -> type[ProviderError] | None:
    """Map an HTTP status to a provider error class, or ``None`` if it succeeded."""
    if 200 <= status < 300:
        return None
    if status == 401:
        return ProviderAuthError
    if status == 403:
        return RevokedCredential
    if status == 429:
        return RateLimited
    if status >= 500:
        return ProviderUnavailable
    return ProviderRequestError
