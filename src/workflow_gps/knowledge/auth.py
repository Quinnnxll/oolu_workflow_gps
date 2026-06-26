"""Authentication for the remote knowledge server — OAuth 2.1 with PKCE.

The client sends ``Authorization: Bearer <token>``; this module supplies that token.
A ``TokenProvider`` abstracts where the token comes from so the rest of the code never
deals with refresh logic:

  * ``StaticTokenProvider`` — a fixed token (tests, simple deployments, or a token
    minted out-of-band).
  * ``OAuth2PKCETokenProvider`` — the real flow. PKCE (RFC 7636) means no client
    secret is stored: a random ``code_verifier`` is created, its SHA-256
    ``code_challenge`` goes in the authorization request, and the verifier is sent at
    token exchange to prove the same client. This module implements the mechanical
    parts — challenge generation, code exchange, refresh, and expiry-cached access
    tokens. The *interactive* leg (opening a browser, catching the loopback redirect)
    is deployment-specific and left to the host harness, which calls
    ``exchange_code`` once with the returned authorization code.

HTTP is injected (``token_transport``) so the exchange/refresh are testable without a
live IdP.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from typing import Protocol, runtime_checkable
from urllib.parse import urlencode


@runtime_checkable
class TokenProvider(Protocol):
    def get_token(self) -> str:
        """Return a currently-valid bearer token, refreshing if needed."""
        ...


class StaticTokenProvider:
    """A fixed bearer token."""

    def __init__(self, token: str):
        self._token = token

    def get_token(self) -> str:
        return self._token


# --- PKCE helpers ---------------------------------------------------------- #
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) per RFC 7636 (S256)."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


class _TokenTransport(Protocol):
    def post_form(self, url: str, form: dict, *, timeout: float) -> dict:
        """POST application/x-www-form-urlencoded, return parsed JSON."""
        ...


class OAuth2PKCETokenProvider:
    """OAuth 2.1 authorization-code-with-PKCE token provider.

    Lifecycle: build the authorization URL (host opens it), receive the auth code at
    the redirect, call ``exchange_code`` once to obtain access + refresh tokens.
    Thereafter ``get_token`` serves the cached access token and refreshes it
    transparently before expiry.
    """

    _EXPIRY_SKEW_S = 30.0  # refresh a little early to avoid edge-of-expiry failures

    def __init__(
        self,
        *,
        token_url: str,
        client_id: str,
        token_transport: "_TokenTransport",
        refresh_token: str | None = None,
        scope: str | None = None,
    ):
        self._token_url = token_url
        self._client_id = client_id
        self._transport = token_transport
        self._scope = scope
        self._refresh_token = refresh_token
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    @staticmethod
    def build_authorization_url(
        auth_url: str, *, client_id: str, redirect_uri: str, code_challenge: str,
        state: str, scope: str | None = None,
    ) -> str:
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        if scope:
            params["scope"] = scope
        return f"{auth_url}?{urlencode(params)}"

    def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> None:
        """One-time exchange of an authorization code for tokens."""
        form = {
            "grant_type": "authorization_code",
            "client_id": self._client_id,
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
        }
        self._apply_token_response(self._transport.post_form(self._token_url, form, timeout=15.0))

    def get_token(self) -> str:
        if self._access_token and time.monotonic() < self._expires_at - self._EXPIRY_SKEW_S:
            return self._access_token
        if not self._refresh_token:
            raise RuntimeError("no valid access token and no refresh token; run exchange_code first")
        self._refresh()
        assert self._access_token is not None
        return self._access_token

    def _refresh(self) -> None:
        form = {
            "grant_type": "refresh_token",
            "client_id": self._client_id,
            "refresh_token": self._refresh_token,
        }
        if self._scope:
            form["scope"] = self._scope
        self._apply_token_response(self._transport.post_form(self._token_url, form, timeout=15.0))

    def _apply_token_response(self, data: dict) -> None:
        self._access_token = data["access_token"]
        self._expires_at = time.monotonic() + float(data.get("expires_in", 3600))
        # Refresh-token rotation: keep the newest if the server rotates it.
        if data.get("refresh_token"):
            self._refresh_token = data["refresh_token"]
