"""HTTP gateway: a private, tenant-aware control-plane prototype.

A transport-agnostic application (over ``Request``/``Response``) with OIDC auth,
tenant-scoped RBAC, per-tenant quotas and rate limits, request idempotency,
pagination, an SSE event stream, verified replay-protected webhooks, security
headers/CORS, and a versioned OpenAPI contract — all on the durable runtime so
multiple gateway processes share one consistent view. The WSGI/ASGI binding and a
live event stream are the production seams. See ``docs/ADAPTER_MATURITY.md``.
"""

from .app import GatewayApp, GatewayConfig
from .asgi import GatewayASGI
from .errors import GatewayError, WebhookError
from .http import Request, Response, Router
from .openapi import API_VERSION, build_openapi
from .webhooks import StripeWebhookVerifier, WebhookSigner, WebhookVerifier

__all__ = [
    "API_VERSION",
    "GatewayApp",
    "GatewayASGI",
    "GatewayConfig",
    "GatewayError",
    "Request",
    "Response",
    "Router",
    "StripeWebhookVerifier",
    "WebhookError",
    "WebhookSigner",
    "WebhookVerifier",
    "build_openapi",
]
