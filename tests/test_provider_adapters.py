"""Provider-adapter tests (codex/provider-adapters).

The exit gate is a single contract suite — capability discovery, revocation,
idempotency, and secret-leakage — run against *every* adapter (Google OAuth,
OpenAI, Anthropic, Anthropic gateway). Provider-specific behaviour (OAuth flow,
error classification, retries, rate limits, budgets, service-identity headers) is
covered alongside. Everything runs offline through an injected transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from oolu.providers import (
    AnthropicAdapter,
    BudgetExceeded,
    GoogleOAuthAdapter,
    GoogleOAuthConfig,
    InvalidCallback,
    OpenAiAdapter,
    ProviderAuthError,
    ProviderRequestError,
    ProviderResponse,
    ProviderUnavailable,
    RateLimited,
    RateLimiter,
    RetryPolicy,
    RevokedCredential,
    SecretVault,
    classify_status,
)
from oolu.providers.base import Budget


# --------------------------------------------------------------------------- #
# Injected transports (the sandbox / remote-mock boundary).                   #
# --------------------------------------------------------------------------- #
class FakeTransport:
    """Routes requests by (method, url-suffix); records everything received."""

    def __init__(self, routes: dict[tuple[str, str], ProviderResponse]):
        self._routes = routes
        self.received: list[dict[str, Any]] = []

    def request(self, method, url, *, headers=None, body=None, timeout=30.0):
        self.received.append(
            {"method": method, "url": url, "headers": dict(headers or {}), "body": body}
        )
        for (m, suffix), response in self._routes.items():
            if method == m and url.endswith(suffix):
                return response
        return ProviderResponse(status=404, json={"error": "no route"})


class SequenceTransport:
    """Returns a fixed sequence of responses regardless of URL (for retry tests)."""

    def __init__(self, responses: list[ProviderResponse]):
        self._responses = list(responses)
        self.calls = 0
        self.received: list[dict[str, Any]] = []

    def request(self, method, url, *, headers=None, body=None, timeout=30.0):
        self.received.append(
            {"method": method, "url": url, "headers": dict(headers or {})}
        )
        index = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[index]


@dataclass
class AdapterCase:
    name: str
    adapter: Any
    vault: SecretVault
    secret: str
    transport: FakeTransport
    ping_suffix: str


# --------------------------------------------------------------------------- #
# Builders for each adapter, wired with an injected transport + vault.         #
# --------------------------------------------------------------------------- #
def _build_google() -> AdapterCase:
    vault = SecretVault()
    client_secret_ref = vault.put("google-client-secret", kind="oauth_client_secret")
    transport = FakeTransport(
        {
            ("POST", "/token"): ProviderResponse(
                200,
                {
                    "access_token": "ya29-access-token",
                    "refresh_token": "1//refresh-token",
                    "expires_in": 3600,
                    "scope": "https://www.googleapis.com/auth/userinfo.email",
                    "token_type": "Bearer",
                },
            ),
            ("GET", "/userinfo"): ProviderResponse(
                200, {"sub": "123", "email": "user@example.com"}
            ),
            ("POST", "/revoke"): ProviderResponse(200, {}),
        }
    )
    config = GoogleOAuthConfig(
        client_id="client-123.apps.googleusercontent.com",
        client_secret_ref=client_secret_ref,
        redirect_uri="https://app.example.com/callback",
        scope_map={
            "email": "https://www.googleapis.com/auth/userinfo.email",
            "calendar.read": "https://www.googleapis.com/auth/calendar.readonly",
        },
    )
    adapter = GoogleOAuthAdapter(config, vault=vault, transport=transport)
    adapter.exchange_code("auth-code", code_verifier="verifier-xyz")
    # The representative secret crossing the boundary is the access token.
    return AdapterCase(
        "google", adapter, vault, "ya29-access-token", transport, "/userinfo"
    )


def _build_openai() -> AdapterCase:
    vault = SecretVault()
    ref = vault.put("sk-openai-secret-123", kind="api_key")
    transport = FakeTransport(
        {
            ("GET", "/models"): ProviderResponse(
                200, {"data": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}]}
            ),
            ("POST", "/chat/completions"): ProviderResponse(
                200, {"id": "resp-1", "choices": [{"message": {"content": "hi"}}]}
            ),
        }
    )
    adapter = OpenAiAdapter(vault=vault, transport=transport, api_key_ref=ref)
    return AdapterCase(
        "openai", adapter, vault, "sk-openai-secret-123", transport, "/models"
    )


def _build_anthropic() -> AdapterCase:
    vault = SecretVault()
    ref = vault.put("sk-ant-secret-123", kind="api_key")
    transport = FakeTransport(
        {
            ("GET", "/models"): ProviderResponse(
                200, {"data": [{"id": "claude-opus-4-8"}]}
            ),
            ("POST", "/messages"): ProviderResponse(
                200, {"id": "msg-1", "content": [{"text": "hi"}]}
            ),
        }
    )
    adapter = AnthropicAdapter(vault=vault, transport=transport, api_key_ref=ref)
    return AdapterCase(
        "anthropic", adapter, vault, "sk-ant-secret-123", transport, "/models"
    )


def _build_anthropic_gateway() -> AdapterCase:
    vault = SecretVault()
    ref = vault.put("gw-token-123", kind="gateway_token")
    transport = FakeTransport(
        {
            ("GET", "/models"): ProviderResponse(
                200, {"data": [{"id": "claude-opus-4-8"}]}
            ),
            ("POST", "/messages"): ProviderResponse(200, {"id": "msg-1"}),
        }
    )
    adapter = AnthropicAdapter(
        vault=vault,
        transport=transport,
        api_key_ref=ref,
        base_url="https://gw.example.com/anthropic/v1",
        gateway=True,
    )
    return AdapterCase(
        "anthropic-gateway", adapter, vault, "gw-token-123", transport, "/models"
    )


BUILDERS = [
    pytest.param(_build_google, id="google"),
    pytest.param(_build_openai, id="openai"),
    pytest.param(_build_anthropic, id="anthropic"),
    pytest.param(_build_anthropic_gateway, id="anthropic-gateway"),
]


# --------------------------------------------------------------------------- #
# The shared contract suite — every adapter must pass all four.               #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("build", BUILDERS)
def test_contract_capability_discovery(build):
    case = build()
    capabilities = case.adapter.capabilities()
    assert capabilities, f"{case.name} reported no capabilities"


@pytest.mark.parametrize("build", BUILDERS)
def test_contract_idempotency(build):
    case = build()
    case.transport.received.clear()
    first = case.adapter.ping(idempotency_key="idem-1")
    second = case.adapter.ping(idempotency_key="idem-1")
    assert first == second
    pings = [
        r
        for r in case.transport.received
        if r["method"] == "GET" and r["url"].endswith(case.ping_suffix)
    ]
    assert len(pings) == 1, "a replayed idempotency key must not re-hit the transport"
    assert pings[0]["headers"].get("Idempotency-Key") == "idem-1"


@pytest.mark.parametrize("build", BUILDERS)
def test_contract_revocation(build):
    case = build()
    case.transport.received.clear()
    case.adapter.revoke_credentials()
    with pytest.raises(RevokedCredential):
        case.adapter.ping(idempotency_key="after-revoke")
    assert case.transport.received == [], "a revoked credential must not reach the wire"


@pytest.mark.parametrize("build", BUILDERS)
def test_contract_secret_leakage(build):
    case = build()
    case.transport.received.clear()
    result = case.adapter.ping(idempotency_key="leak-check")
    secret = case.secret
    # The secret must not appear in any artifact we keep or return.
    assert secret not in repr(case.adapter)
    assert secret not in str(case.adapter.audit)
    assert secret not in str(result)
    # ...but it MUST cross the boundary to the provider (in the auth header).
    on_the_wire = " ".join(str(r["headers"]) for r in case.transport.received)
    assert secret in on_the_wire


# --------------------------------------------------------------------------- #
# Google authorization-code / OIDC flow.                                      #
# --------------------------------------------------------------------------- #
def test_google_authorization_url_has_pkce_state_and_scopes():
    case = _build_google()
    url = case.adapter.authorization_url(
        capabilities=frozenset({"email"}),
        state="st-123",
        code_verifier="verifier-xyz",
        nonce="nonce-1",
    )
    assert "state=st-123" in url
    assert "code_challenge_method=S256" in url
    assert "code_challenge=" in url
    assert "scope=" in url
    assert "nonce=nonce-1" in url


def test_google_callback_validation():
    case = _build_google()
    assert (
        case.adapter.validate_callback(
            {"state": "st", "code": "the-code"}, expected_state="st"
        )
        == "the-code"
    )
    with pytest.raises(InvalidCallback):
        case.adapter.validate_callback(
            {"state": "wrong", "code": "x"}, expected_state="st"
        )
    with pytest.raises(InvalidCallback):
        case.adapter.validate_callback({"error": "access_denied"}, expected_state="st")


def test_google_refresh_rotates_token_and_revoke_blocks():
    case = _build_google()
    original = case.adapter._credential.access_token_ref
    refreshed = case.adapter.refresh()
    assert refreshed.access_token_ref != original  # a new vaulted token
    case.adapter.revoke()
    with pytest.raises(RevokedCredential):
        case.adapter.ping(idempotency_key="post-revoke")


def test_google_requires_exchange_before_calls():
    vault = SecretVault()
    config = GoogleOAuthConfig(
        client_id="c",
        client_secret_ref=vault.put("s"),
        redirect_uri="https://app/cb",
        scope_map={"email": "scope"},
    )
    adapter = GoogleOAuthAdapter(config, vault=vault, transport=FakeTransport({}))
    with pytest.raises(ProviderAuthError):
        adapter.ping()


# --------------------------------------------------------------------------- #
# Error classification, retries, rate limits, budgets, service identity.       #
# --------------------------------------------------------------------------- #
def test_error_classification():
    assert classify_status(200) is None
    assert classify_status(401) is ProviderAuthError
    assert classify_status(403) is RevokedCredential
    assert classify_status(429) is RateLimited
    assert classify_status(503) is ProviderUnavailable
    assert classify_status(400) is ProviderRequestError


def test_retries_then_succeeds():
    vault = SecretVault()
    ref = vault.put("sk-x")
    transport = SequenceTransport(
        [
            ProviderResponse(503, {}),
            ProviderResponse(503, {}),
            ProviderResponse(200, {"ok": True}),
        ]
    )
    adapter = OpenAiAdapter(
        vault=vault,
        transport=transport,
        api_key_ref=ref,
        retry=RetryPolicy(max_attempts=3),
    )
    result = adapter.invoke("/chat/completions", {"x": 1}, idempotency_key="r")
    assert result == {"ok": True}
    assert transport.calls == 3


def test_retries_exhausted_raises():
    vault = SecretVault()
    ref = vault.put("sk-x")
    transport = SequenceTransport([ProviderResponse(503, {}) for _ in range(3)])
    adapter = OpenAiAdapter(
        vault=vault,
        transport=transport,
        api_key_ref=ref,
        retry=RetryPolicy(max_attempts=3),
    )
    with pytest.raises(ProviderUnavailable):
        adapter.invoke("/chat/completions", {}, idempotency_key="r2")


def test_rate_limiter_blocks_second_call():
    vault = SecretVault()
    ref = vault.put("sk-x")
    transport = FakeTransport({("GET", "/models"): ProviderResponse(200, {"data": []})})
    limiter = RateLimiter(capacity=1, refill_per_second=0, clock=lambda: 0.0)
    adapter = OpenAiAdapter(
        vault=vault, transport=transport, api_key_ref=ref, rate_limiter=limiter
    )
    adapter.ping(idempotency_key="a")
    with pytest.raises(RateLimited):
        adapter.ping(idempotency_key="b")


def test_budget_is_enforced():
    vault = SecretVault()
    ref = vault.put("sk-x")
    transport = FakeTransport(
        {("POST", "/chat/completions"): ProviderResponse(200, {"ok": True})}
    )
    adapter = OpenAiAdapter(
        vault=vault, transport=transport, api_key_ref=ref, budget=Budget(limit=1.0)
    )
    adapter.invoke("/chat/completions", {}, idempotency_key="a", cost=1.0)
    with pytest.raises(BudgetExceeded):
        adapter.invoke("/chat/completions", {}, idempotency_key="b", cost=0.5)


def test_openai_service_identity_headers():
    vault = SecretVault()
    ref = vault.put("sk-x")
    transport = FakeTransport({("GET", "/models"): ProviderResponse(200, {"data": []})})
    adapter = OpenAiAdapter(
        vault=vault,
        transport=transport,
        api_key_ref=ref,
        organization="org-1",
        project="proj-1",
    )
    adapter.ping(idempotency_key="x")
    headers = transport.received[-1]["headers"]
    assert headers["OpenAI-Organization"] == "org-1"
    assert headers["OpenAI-Project"] == "proj-1"
    assert headers["Authorization"].startswith("Bearer ")


def test_anthropic_direct_uses_x_api_key_and_gateway_uses_bearer():
    direct = _build_anthropic()
    direct.adapter.ping(idempotency_key="x")
    direct_headers = direct.transport.received[-1]["headers"]
    assert direct_headers.get("x-api-key") == direct.secret
    assert "anthropic-version" in direct_headers

    gateway = _build_anthropic_gateway()
    gateway.adapter.ping(idempotency_key="x")
    gateway_headers = gateway.transport.received[-1]["headers"]
    assert gateway_headers["Authorization"] == f"Bearer {gateway.secret}"
