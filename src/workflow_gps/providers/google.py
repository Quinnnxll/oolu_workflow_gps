"""Google authorization-code / OIDC adapter.

Implements the authorization-code flow with PKCE: build the consent URL, validate
the callback (state + error), exchange the code for tokens, refresh, and revoke.
Provider-neutral capabilities are mapped to Google scopes via configuration. The
client secret and the issued tokens live only in the vault; this adapter holds
references and mints auth headers at call time.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from .base import BaseProviderAdapter, HttpTransport
from .errors import InvalidCallback, ProviderAuthError, classify_status
from .vault import CredentialRef, SecretVault


def pkce_challenge(verifier: str) -> str:
    """S256 PKCE challenge for a verifier."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class GoogleOAuthConfig:
    client_id: str
    client_secret_ref: CredentialRef
    redirect_uri: str
    scope_map: dict[str, str]
    auth_endpoint: str = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url: str = "https://oauth2.googleapis.com/token"
    revoke_url: str = "https://oauth2.googleapis.com/revoke"
    base_url: str = "https://www.googleapis.com"
    userinfo_path: str = "/oauth2/v3/userinfo"


@dataclass(frozen=True)
class OAuthCredential:
    access_token_ref: CredentialRef
    refresh_token_ref: CredentialRef | None
    scopes: tuple[str, ...] = ()
    token_type: str = "Bearer"
    expires_at: datetime | None = None


class GoogleOAuthAdapter(BaseProviderAdapter):
    name = "google"

    def __init__(
        self,
        config: GoogleOAuthConfig,
        *,
        vault: SecretVault,
        transport: HttpTransport,
        **kwargs,
    ):
        super().__init__(
            vault=vault, transport=transport, base_url=config.base_url, **kwargs
        )
        self._config = config
        self._credential: OAuthCredential | None = None
        self._ping_path = config.userinfo_path

    # --- capability discovery (configured scope mapping) ----------------- #
    def capabilities(self) -> frozenset[str]:
        return frozenset(self._config.scope_map)

    def map_scopes(self, capabilities: frozenset[str]) -> list[str]:
        return [
            self._config.scope_map[c]
            for c in capabilities
            if c in self._config.scope_map
        ]

    # --- authorization-code flow ----------------------------------------- #
    def authorization_url(
        self,
        *,
        capabilities: frozenset[str],
        state: str,
        code_verifier: str,
        nonce: str | None = None,
    ) -> str:
        scopes = self.map_scopes(capabilities)
        if not scopes:
            raise ProviderAuthError(
                "no Google scopes mapped for requested capabilities"
            )
        params = {
            "client_id": self._config.client_id,
            "redirect_uri": self._config.redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": pkce_challenge(code_verifier),
            "code_challenge_method": "S256",
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent",
        }
        if nonce is not None:
            params["nonce"] = nonce
        return f"{self._config.auth_endpoint}?{urlencode(params)}"

    def validate_callback(self, params: dict[str, str], *, expected_state: str) -> str:
        if "error" in params:
            raise InvalidCallback(f"authorization error: {params['error']}")
        if params.get("state") != expected_state:
            raise InvalidCallback("state mismatch in OAuth callback")
        code = params.get("code")
        if not code:
            raise InvalidCallback("authorization code missing from callback")
        return code

    def exchange_code(
        self, code: str, *, code_verifier: str, now: datetime | None = None
    ) -> OAuthCredential:
        moment = now or datetime.now(UTC)
        body = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self._config.client_id,
            "client_secret": self._vault.resolve(self._config.client_secret_ref),
            "redirect_uri": self._config.redirect_uri,
            "code_verifier": code_verifier,
        }
        data = self._token_request(self._config.token_url, body, moment)
        self._credential = self._credential_from_token_response(data, moment)
        return self._credential

    def refresh(self, *, now: datetime | None = None) -> OAuthCredential:
        moment = now or datetime.now(UTC)
        if self._credential is None or self._credential.refresh_token_ref is None:
            raise ProviderAuthError("no refresh token available")
        body = {
            "grant_type": "refresh_token",
            "client_id": self._config.client_id,
            "client_secret": self._vault.resolve(self._config.client_secret_ref),
            "refresh_token": self._vault.resolve(self._credential.refresh_token_ref),
        }
        data = self._token_request(self._config.token_url, body, moment)
        access_ref = self._vault.put(data["access_token"], kind="google_access_token")
        self._credential = OAuthCredential(
            access_token_ref=access_ref,
            refresh_token_ref=self._credential.refresh_token_ref,
            scopes=tuple(data.get("scope", " ".join(self._credential.scopes)).split()),
            token_type=data.get("token_type", "Bearer"),
            expires_at=moment + timedelta(seconds=int(data.get("expires_in", 3600))),
        )
        return self._credential

    def revoke(self) -> None:
        if self._credential is None:
            return
        token = self._vault.resolve(self._credential.access_token_ref)
        response = self._transport.request(
            "POST", self._config.revoke_url, headers={}, body={"token": token}
        )
        self.audit.append(
            {
                "method": "POST",
                "url": self._config.revoke_url,
                "status": response.status,
            }
        )
        self._vault.revoke(self._credential.access_token_ref)
        if self._credential.refresh_token_ref is not None:
            self._vault.revoke(self._credential.refresh_token_ref)

    # --- BaseProviderAdapter hooks --------------------------------------- #
    def ping(self, *, idempotency_key: str | None = None) -> dict:
        return self.authenticated_call(
            method="GET", path=self._ping_path, idempotency_key=idempotency_key
        )

    def revoke_credentials(self) -> None:
        if self._credential is not None:
            self._vault.revoke(self._credential.access_token_ref)

    def _primary_ref(self) -> CredentialRef:
        if self._credential is None:
            raise ProviderAuthError("no credential; complete the OAuth exchange first")
        return self._credential.access_token_ref

    def _auth_headers(self) -> dict[str, str]:
        return self._vault.authorize_header(self._primary_ref(), scheme="Bearer")

    # --- internals ------------------------------------------------------- #
    def _token_request(self, url: str, body: dict, moment: datetime) -> dict:
        response = self._transport.request(
            "POST",
            url,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=body,
        )
        self.audit.append({"method": "POST", "url": url, "status": response.status})
        error_cls = classify_status(response.status)
        if error_cls is not None:
            raise error_cls(
                f"{self.name}: token endpoint status {response.status}: "
                + self._vault.redact(str(response.json))
            )
        return response.json

    def _credential_from_token_response(
        self, data: dict, moment: datetime
    ) -> OAuthCredential:
        access_ref = self._vault.put(data["access_token"], kind="google_access_token")
        refresh_ref = (
            self._vault.put(data["refresh_token"], kind="google_refresh_token")
            if data.get("refresh_token")
            else None
        )
        return OAuthCredential(
            access_token_ref=access_ref,
            refresh_token_ref=refresh_ref,
            scopes=tuple(data.get("scope", "").split()),
            token_type=data.get("token_type", "Bearer"),
            expires_at=moment + timedelta(seconds=int(data.get("expires_in", 3600))),
        )


__all__ = [
    "GoogleOAuthAdapter",
    "GoogleOAuthConfig",
    "OAuthCredential",
    "pkce_challenge",
]
