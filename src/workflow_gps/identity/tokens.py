"""OIDC assertion validation with a pluggable signature verifier.

A JWT is only trusted after its signature verifies against a *configured* provider
and its registered claims (issuer, audience, expiry, not-before) check out. The
signature algorithm is pinned per provider, so ``alg: none`` and algorithm-confusion
attacks are rejected before any claim is read.

The ``SignatureVerifier`` port keeps the crypto pluggable: a stdlib HMAC verifier
ships for local/symmetric setups and tests, while asymmetric RS256/ES256 verifiers
backed by a provider JWKS are an adapter (an optional crypto dependency) — the
validation *logic* here does not change.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from .errors import AuthenticationError
from .models import Claims, PrincipalKind


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


@runtime_checkable
class SignatureVerifier(Protocol):
    algorithm: str

    def verify(self, signing_input: bytes, signature: bytes, header: dict) -> bool: ...


class Hs256Verifier:
    """HMAC-SHA256 verifier (symmetric). For local setups and offline tests.

    Production IdPs use asymmetric keys; that is a JWKS-backed verifier adapter
    implementing this same port.
    """

    algorithm = "HS256"

    def __init__(self, secret: bytes | str):
        self._secret = secret.encode("utf-8") if isinstance(secret, str) else secret

    def verify(self, signing_input: bytes, signature: bytes, header: dict) -> bool:
        if header.get("alg") != self.algorithm:
            return False
        expected = hmac.new(self._secret, signing_input, hashlib.sha256).digest()
        return hmac.compare_digest(expected, signature)


class ProviderConfig(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    issuer: str
    audiences: frozenset[str]
    verifier: SignatureVerifier
    leeway_seconds: int = 60
    tenant_claim: str = "tenant"
    principal_kind_claim: str = "pkind"


class OidcValidator:
    """Validates OIDC assertions against a set of configured providers."""

    def __init__(self, providers: list[ProviderConfig]):
        if not providers:
            raise ValueError("at least one provider must be configured")
        self._by_issuer = {provider.issuer: provider for provider in providers}

    def validate(self, token: str, *, now: datetime | None = None) -> Claims:
        moment = now or datetime.now(UTC)
        header, payload, signing_input, signature = self._split(token)

        alg = header.get("alg")
        if not alg or alg.lower() == "none":
            raise AuthenticationError("token algorithm 'none' is not permitted")

        issuer = payload.get("iss")
        if not isinstance(issuer, str) or issuer not in self._by_issuer:
            raise AuthenticationError("token issuer is not a configured provider")
        provider = self._by_issuer[issuer]

        if alg != provider.verifier.algorithm:
            raise AuthenticationError(
                f"token algorithm {alg!r} does not match provider {issuer!r}"
            )
        if not provider.verifier.verify(signing_input, signature, header):
            raise AuthenticationError("token signature verification failed")

        return self._claims(payload, provider, moment)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _split(token: str) -> tuple[dict, dict, bytes, bytes]:
        parts = token.split(".")
        if len(parts) != 3:
            raise AuthenticationError("malformed JWT: expected three segments")
        header_b64, payload_b64, signature_b64 = parts
        try:
            header = json.loads(_b64url_decode(header_b64))
            payload = json.loads(_b64url_decode(payload_b64))
            signature = _b64url_decode(signature_b64)
        except (ValueError, json.JSONDecodeError) as exc:
            raise AuthenticationError(f"malformed JWT: {exc}") from exc
        signing_input = f"{header_b64}.{payload_b64}".encode()
        return header, payload, signing_input, signature

    @staticmethod
    def _claims(payload: dict, provider: ProviderConfig, moment: datetime) -> Claims:
        leeway = timedelta(seconds=provider.leeway_seconds)

        subject = payload.get("sub")
        if not isinstance(subject, str) or not subject:
            raise AuthenticationError("token is missing a subject")

        raw_aud = payload.get("aud", [])
        audiences = [raw_aud] if isinstance(raw_aud, str) else list(raw_aud)
        if not provider.audiences.intersection(audiences):
            raise AuthenticationError("token audience is not accepted")

        exp = payload.get("exp")
        if exp is None:
            raise AuthenticationError("token is missing an expiry")
        expires_at = datetime.fromtimestamp(exp, UTC)
        if moment > expires_at + leeway:
            raise AuthenticationError("token has expired")

        not_before = None
        if (nbf := payload.get("nbf")) is not None:
            not_before = datetime.fromtimestamp(nbf, UTC)
            if moment < not_before - leeway:
                raise AuthenticationError("token is not yet valid")

        issued_at = None
        if (iat := payload.get("iat")) is not None:
            issued_at = datetime.fromtimestamp(iat, UTC)

        tenant_id = payload.get(provider.tenant_claim)
        if not isinstance(tenant_id, str) or not tenant_id:
            raise AuthenticationError("token is missing a tenant claim")

        kind_value = payload.get(
            provider.principal_kind_claim, PrincipalKind.USER.value
        )
        try:
            principal_kind = PrincipalKind(kind_value)
        except ValueError as exc:
            raise AuthenticationError(f"unknown principal kind: {kind_value}") from exc

        amr = payload.get("amr", [])
        return Claims(
            issuer=provider.issuer,
            subject=subject,
            audiences=audiences,
            tenant_id=tenant_id,
            principal_kind=principal_kind,
            expires_at=expires_at,
            not_before=not_before,
            issued_at=issued_at,
            amr=list(amr) if isinstance(amr, list) else [amr],
            acr=payload.get("acr"),
            raw=dict(payload),
        )


# --------------------------------------------------------------------------- #
# A tiny HS256 token minter — for tests and local symmetric setups only.       #
# --------------------------------------------------------------------------- #
def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class Hs256Signer(BaseModel):
    """Mints HS256 JWTs. NOT for production identity — IdPs sign asymmetrically."""

    model_config = ConfigDict(frozen=True)

    secret: str
    issuer: str
    audience: str
    tenant_claim: str = "tenant"

    def mint(
        self,
        *,
        subject: str,
        tenant_id: str,
        ttl_seconds: int = 3600,
        now: datetime | None = None,
        principal_kind: str = "user",
        amr: list[str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> str:
        moment = now or datetime.now(UTC)
        header = {"alg": "HS256", "typ": "JWT"}
        payload: dict[str, Any] = {
            "iss": self.issuer,
            "sub": subject,
            "aud": self.audience,
            "iat": int(moment.timestamp()),
            "exp": int((moment + timedelta(seconds=ttl_seconds)).timestamp()),
            self.tenant_claim: tenant_id,
            "pkind": principal_kind,
        }
        if amr:
            payload["amr"] = amr
        if extra:
            payload.update(extra)
        header_b64 = _b64url_encode(json.dumps(header).encode())
        payload_b64 = _b64url_encode(json.dumps(payload).encode())
        signing_input = f"{header_b64}.{payload_b64}".encode()
        signature = hmac.new(
            self.secret.encode("utf-8"), signing_input, hashlib.sha256
        ).digest()
        return f"{header_b64}.{payload_b64}.{_b64url_encode(signature)}"


__all__ = [
    "Hs256Signer",
    "Hs256Verifier",
    "OidcValidator",
    "ProviderConfig",
    "SignatureVerifier",
]
