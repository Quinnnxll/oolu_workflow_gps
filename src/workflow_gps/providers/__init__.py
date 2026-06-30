"""Provider adapters: contract-tested integrations behind a vault boundary.

Google authorization-code/OIDC, OpenAI (incl. org/project service identity), and
Anthropic (incl. managed gateway) adapters share one pipeline — capability
discovery, rate limits, budgets, request ids, idempotency, retries, and error
classification — and credentials live only in the :class:`SecretVault`. Every
adapter passes the same capability/revocation/idempotency/secret-leakage contract
suite. See ``docs/ADAPTER_MATURITY.md``.
"""

from .apikey import AnthropicAdapter, ApiKeyProviderAdapter, OpenAiAdapter
from .base import (
    BaseProviderAdapter,
    Budget,
    HttpTransport,
    ProviderResponse,
    RateLimiter,
    RequestContext,
    RetryPolicy,
)
from .errors import (
    RETRYABLE_STATUSES,
    BudgetExceeded,
    InvalidCallback,
    ProviderAuthError,
    ProviderError,
    ProviderRequestError,
    ProviderUnavailable,
    RateLimited,
    RevokedCredential,
    TokenExpired,
    classify_status,
)
from .google import (
    GoogleOAuthAdapter,
    GoogleOAuthConfig,
    OAuthCredential,
    pkce_challenge,
)
from .vault import CredentialRef, SecretVault

# NB: ``HttpxTransport`` lives in ``providers.transport`` and is intentionally not
# re-exported here — it requires the optional ``http`` extra (httpx), and importing
# the providers package must not force that dependency on the base install.

__all__ = [
    "RETRYABLE_STATUSES",
    "AnthropicAdapter",
    "ApiKeyProviderAdapter",
    "BaseProviderAdapter",
    "Budget",
    "BudgetExceeded",
    "CredentialRef",
    "GoogleOAuthAdapter",
    "GoogleOAuthConfig",
    "HttpTransport",
    "InvalidCallback",
    "OAuthCredential",
    "OpenAiAdapter",
    "ProviderAuthError",
    "ProviderError",
    "ProviderRequestError",
    "ProviderResponse",
    "ProviderUnavailable",
    "RateLimited",
    "RateLimiter",
    "RequestContext",
    "RetryPolicy",
    "RevokedCredential",
    "SecretVault",
    "TokenExpired",
    "classify_status",
    "pkce_challenge",
]
