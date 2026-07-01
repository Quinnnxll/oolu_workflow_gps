from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from .tokens import _b64url_decode

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, padding, utils
    from cryptography.hazmat.primitives.asymmetric.ec import (
        SECP256R1,
        EllipticCurvePublicNumbers,
    )
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
except ModuleNotFoundError as exc:  # pragma: no cover
    raise ModuleNotFoundError(
        "JwksVerifier requires the 'oidc' extra: pip install 'workflow-gps[oidc]'"
    ) from exc

JwksDocument = dict[str, Any]
JwksFetcher = Callable[[], JwksDocument]

_SUPPORTED = {"RS256", "ES256"}


def _b64url_uint(value: str) -> int:
    return int.from_bytes(_b64url_decode(value), "big")


def _public_key_from_jwk(jwk: dict) -> Any | None:
    kty = jwk.get("kty")
    if kty == "RSA":
        return RSAPublicNumbers(_b64url_uint(jwk["e"]), _b64url_uint(jwk["n"])).public_key()
    if kty == "EC":
        if jwk.get("crv") != "P-256":
            return None
        return EllipticCurvePublicNumbers(
            _b64url_uint(jwk["x"]), _b64url_uint(jwk["y"]), SECP256R1()
        ).public_key()
    return None


class JwksVerifier:
    def __init__(
        self,
        *,
        fetch: JwksFetcher,
        algorithm: str = "RS256",
        cache_ttl: float = 3600.0,
        min_refresh_interval: float = 60.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if algorithm not in _SUPPORTED:
            raise ValueError(f"unsupported algorithm {algorithm!r}; use one of {_SUPPORTED}")
        self.algorithm = algorithm
        self._fetch = fetch
        self._cache_ttl = cache_ttl
        self._min_refresh = min_refresh_interval
        self._clock = clock or time.monotonic
        self._expected_kty = "RSA" if algorithm == "RS256" else "EC"
        self._keys: dict[str, Any] = {}
        self._fetched_at: float | None = None
        self._last_miss_at: float | None = None

    def verify(self, signing_input: bytes, signature: bytes, header: dict) -> bool:
        if header.get("alg") != self.algorithm:
            return False
        key = self._key_for(header.get("kid"))
        if key is None:
            return False
        return self._verify_signature(key, signing_input, signature)

    def _key_for(self, kid: str | None) -> Any | None:
        now = self._clock()
        if self._fetched_at is None or (now - self._fetched_at) >= self._cache_ttl:
            self._refresh(now)
        key = self._lookup(kid)
        if key is None and (
            self._last_miss_at is None or (now - self._last_miss_at) >= self._min_refresh
        ):
            self._last_miss_at = now
            self._refresh(now)
            key = self._lookup(kid)
        return key

    def _lookup(self, kid: str | None) -> Any | None:
        if kid is not None:
            return self._keys.get(kid)
        if len(self._keys) == 1:
            return next(iter(self._keys.values()))
        return None

    def _refresh(self, now: float) -> None:
        document = self._fetch()
        keys: dict[str, Any] = {}
        for jwk in document.get("keys", []):
            kid = jwk.get("kid")
            if kid is None or jwk.get("kty") != self._expected_kty:
                continue
            try:
                public_key = _public_key_from_jwk(jwk)
            except (KeyError, ValueError):
                continue
            if public_key is not None:
                keys[kid] = public_key
        self._keys = keys
        self._fetched_at = now

    def _verify_signature(self, key: Any, signing_input: bytes, signature: bytes) -> bool:
        try:
            if self.algorithm == "RS256":
                key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
            else:
                if len(signature) != 64:
                    return False
                r = int.from_bytes(signature[:32], "big")
                s = int.from_bytes(signature[32:], "big")
                der = utils.encode_dss_signature(r, s)
                key.verify(der, signing_input, ec.ECDSA(hashes.SHA256()))
        except InvalidSignature:
            return False
        return True

    @classmethod
    def from_transport(
        cls,
        transport: Any,
        jwks_uri: str,
        *,
        algorithm: str = "RS256",
        **kwargs: Any,
    ) -> JwksVerifier:
        def fetch() -> JwksDocument:
            return transport.request("GET", jwks_uri).json

        return cls(fetch=fetch, algorithm=algorithm, **kwargs)
