"""Production ``HttpTransport``: a real HTTP client behind the provider boundary.

Provider adapters reach the network only through the injected ``HttpTransport``
port (``providers/base.py``); offline tests inject a fake, and this module is the
adapter that wraps a real ``httpx`` client for production. It maps the port's
``(method, url, headers, body)`` call onto an HTTP request and the reply back into
a :class:`ProviderResponse`. It is the seam previously listed "Not implemented" in
``docs/ADAPTER_MATURITY.md``.

Two behaviours matter for correctness:

* **Body encoding follows the declared content type.** A body sent with a
  ``Content-Type: application/x-www-form-urlencoded`` header (OAuth token
  endpoints, e.g. ``GoogleOAuthAdapter._token_request``) is form-encoded; every
  other body is sent as JSON. Posting an OAuth token request as JSON would be
  rejected by the IdP.
* **Transport failures become a retryable response, not a leaked exception.** A
  timeout or connection error is surfaced as ``ProviderResponse(status=503)`` so
  the shared retry pipeline in ``BaseProviderAdapter`` handles it uniformly with
  transient server-side 5xx, instead of an ``httpx`` exception escaping the port.

TLS and proxying come from the environment (``trust_env``): the managed CA bundle
and ``HTTPS_PROXY`` configured for the session are honoured without extra wiring.
A caller may override the CA bundle (``verify``) or inject a pre-built client.
"""

from __future__ import annotations

import os
import ssl
from typing import Any

from .base import ProviderResponse

try:
    import httpx
except ModuleNotFoundError as exc:  # pragma: no cover - exercised via the extra
    raise ModuleNotFoundError(
        "HttpxTransport requires the 'http' extra: pip install 'oolu[http]'"
    ) from exc


_FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
# Standard CA-bundle env vars, in precedence order. The managed proxy sets these
# to its re-termination CA; honouring them keeps TLS verification on.
_CA_ENV_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "GIT_SSL_CAINFO")


def _resolve_verify() -> ssl.SSLContext | bool:
    """Build an SSL context from the configured CA bundle, else default to True.

    Returning an explicit context (rather than relying on httpx's own env
    handling) keeps verification deterministic across httpx versions while still
    trusting the managed proxy CA.
    """
    for var in _CA_ENV_VARS:
        path = os.environ.get(var)
        if path and os.path.exists(path):
            return ssl.create_default_context(cafile=path)
    return True


def _is_form(headers: dict[str, str]) -> bool:
    for key, value in headers.items():
        if key.lower() == "content-type":
            return _FORM_CONTENT_TYPE in value.lower()
    return False


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    """Best-effort JSON decode; a non-JSON or empty body yields an empty dict.

    A non-dict JSON document (a bare list or scalar) is wrapped under ``data`` so
    the port's ``dict`` contract always holds.
    """
    if not response.content:
        return {}
    try:
        decoded = response.json()
    except ValueError:
        return {}
    return decoded if isinstance(decoded, dict) else {"data": decoded}


class HttpxTransport:
    """A production :class:`HttpTransport` backed by a pooled ``httpx.Client``."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        verify: ssl.SSLContext | str | bool | None = None,
        default_timeout: float = 30.0,
    ) -> None:
        self._default_timeout = default_timeout
        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            self._client = httpx.Client(
                trust_env=True,
                verify=_resolve_verify() if verify is None else verify,
                timeout=default_timeout,
            )
            self._owns_client = True

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> ProviderResponse:
        sent_headers = dict(headers or {})
        kwargs: dict[str, Any] = {"headers": sent_headers, "timeout": timeout}
        if body is not None:
            # Honour the caller's declared content type: form for OAuth token
            # endpoints, JSON for everything else.
            if _is_form(sent_headers):
                kwargs["data"] = body
            else:
                kwargs["json"] = body
        try:
            response = self._client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            # Surface transport failures as a retryable response so the shared
            # pipeline retries them like a transient 5xx instead of crashing.
            return ProviderResponse(status=503, json={"error": str(exc)})
        return ProviderResponse(
            status=response.status_code,
            json=_safe_json(response),
            headers=dict(response.headers),
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> HttpxTransport:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
