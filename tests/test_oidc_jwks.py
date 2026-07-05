from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from workflow_gps.identity import (
    IdentityConfigurationError,
    OidcValidator,
    ProviderConfig,
    assert_production_identity,
)
from workflow_gps.identity.errors import AuthenticationError
from workflow_gps.identity.jwks import JwksVerifier
from workflow_gps.identity.tokens import Hs256Verifier
from workflow_gps.providers.transport import HttpxTransport

ISSUER = "https://idp.test"
AUDIENCE = "workflow-gps"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_uint(value: int) -> str:
    length = (value.bit_length() + 7) // 8
    return _b64url(value.to_bytes(length, "big"))


def _payload() -> dict:
    now = datetime.now(UTC)
    return {
        "iss": ISSUER,
        "sub": "user-1",
        "aud": AUDIENCE,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
        "tenant": "tenant-1",
        "pkind": "user",
    }


def _signing_input(header: dict, payload: dict) -> tuple[str, bytes]:
    header_b64 = _b64url(json.dumps(header).encode())
    payload_b64 = _b64url(json.dumps(payload).encode())
    prefix = f"{header_b64}.{payload_b64}"
    return prefix, prefix.encode()


def _rsa_material(kid: str = "r1"):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = key.public_key().public_numbers()
    jwk = {
        "kty": "RSA",
        "kid": kid,
        "alg": "RS256",
        "use": "sig",
        "n": _b64url_uint(numbers.n),
        "e": _b64url_uint(numbers.e),
    }

    def mint(payload: dict | None = None) -> str:
        prefix, message = _signing_input(
            {"alg": "RS256", "typ": "JWT", "kid": kid}, payload or _payload()
        )
        signature = key.sign(message, padding.PKCS1v15(), hashes.SHA256())
        return f"{prefix}.{_b64url(signature)}"

    return key, jwk, mint


def _ec_material(kid: str = "e1"):
    key = ec.generate_private_key(ec.SECP256R1())
    numbers = key.public_key().public_numbers()
    jwk = {
        "kty": "EC",
        "kid": kid,
        "crv": "P-256",
        "x": _b64url(numbers.x.to_bytes(32, "big")),
        "y": _b64url(numbers.y.to_bytes(32, "big")),
    }

    def mint(payload: dict | None = None) -> str:
        prefix, message = _signing_input(
            {"alg": "ES256", "typ": "JWT", "kid": kid}, payload or _payload()
        )
        der = key.sign(message, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return f"{prefix}.{_b64url(raw)}"

    return key, jwk, mint


def _validator(verifier) -> OidcValidator:
    return OidcValidator(
        [
            ProviderConfig(
                issuer=ISSUER, audiences=frozenset({AUDIENCE}), verifier=verifier
            )
        ]
    )


def test_rs256_token_validates_via_jwks():
    _, jwk, mint = _rsa_material()
    verifier = JwksVerifier(fetch=lambda: {"keys": [jwk]}, algorithm="RS256")
    claims = _validator(verifier).validate(mint())
    assert claims.subject == "user-1"
    assert claims.tenant_id == "tenant-1"


def test_es256_token_validates_via_jwks():
    _, jwk, mint = _ec_material()
    verifier = JwksVerifier(fetch=lambda: {"keys": [jwk]}, algorithm="ES256")
    claims = _validator(verifier).validate(mint())
    assert claims.subject == "user-1"


def test_tampered_signature_is_rejected():
    _, jwk, mint = _rsa_material()
    verifier = JwksVerifier(fetch=lambda: {"keys": [jwk]}, algorithm="RS256")
    token = mint()
    tampered = token[:-4] + ("aaaa" if not token.endswith("aaaa") else "bbbb")
    with pytest.raises(AuthenticationError):
        _validator(verifier).validate(tampered)


def test_algorithm_mismatch_returns_false():
    _, jwk, _ = _rsa_material()
    verifier = JwksVerifier(fetch=lambda: {"keys": [jwk]}, algorithm="RS256")
    assert verifier.verify(b"x", b"y", {"alg": "ES256", "kid": "r1"}) is False


def test_unknown_kid_triggers_rotation_refresh():
    _, old_jwk, _ = _rsa_material("old")
    _, new_jwk, mint = _rsa_material("new")
    documents = [{"keys": [old_jwk]}, {"keys": [old_jwk, new_jwk]}]
    calls = {"n": 0}
    clock = {"t": 1000.0}

    def fetch():
        index = min(calls["n"], len(documents) - 1)
        calls["n"] += 1
        return documents[index]

    verifier = JwksVerifier(
        fetch=fetch,
        algorithm="RS256",
        min_refresh_interval=60.0,
        clock=lambda: clock["t"],
    )
    clock["t"] = 1100.0
    claims = _validator(verifier).validate(mint())
    assert claims.subject == "user-1"
    assert calls["n"] == 2


def test_rsa_verifier_ignores_ec_keys():
    _, ec_jwk, _ = _ec_material()
    verifier = JwksVerifier(fetch=lambda: {"keys": [ec_jwk]}, algorithm="RS256")
    assert verifier.verify(b"x", b"y", {"alg": "RS256", "kid": "e1"}) is False


def test_from_transport_fetches_jwks_over_http():
    _, jwk, mint = _rsa_material()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/.well-known/jwks.json"
        return httpx.Response(200, json={"keys": [jwk]})

    transport = HttpxTransport(
        client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    verifier = JwksVerifier.from_transport(
        transport, "https://idp.test/.well-known/jwks.json", algorithm="RS256"
    )
    claims = _validator(verifier).validate(mint())
    assert claims.subject == "user-1"


def test_unsupported_algorithm_rejected():
    with pytest.raises(ValueError):
        JwksVerifier(fetch=lambda: {"keys": []}, algorithm="HS256")


def test_production_guard_rejects_symmetric_provider():
    provider = ProviderConfig(
        issuer=ISSUER, audiences=frozenset({AUDIENCE}), verifier=Hs256Verifier("secret")
    )
    with pytest.raises(IdentityConfigurationError):
        assert_production_identity([provider])


def test_production_guard_accepts_asymmetric_provider():
    _, jwk, _ = _rsa_material()
    provider = ProviderConfig(
        issuer=ISSUER,
        audiences=frozenset({AUDIENCE}),
        verifier=JwksVerifier(fetch=lambda: {"keys": [jwk]}, algorithm="RS256"),
    )
    assert_production_identity([provider])
