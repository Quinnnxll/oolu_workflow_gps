"""RFC 6238 TOTP — the second factor that releases a payment.

Placing an order or making a booking spends money on the user's behalf, so
the gate is deliberately strong: the final consent must carry a fresh code
from the user's authenticator app (Google Authenticator, Aegis, 1Password —
anything speaking the standard ``otpauth://`` URI). This module is the
primitive: generate an enrollment secret and its provisioning URI, and
verify a submitted code against the current 30-second window (with a
one-step drift each way for clock skew).

Pure standard library — no dependency for a defense-in-depth control.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
from urllib.parse import quote

# The interoperable defaults every authenticator app assumes.
_DIGITS = 6
_PERIOD = 30
_ALGORITHM = "SHA1"
# One step of clock skew each way — the standard tolerance.
_DRIFT = 1


def generate_secret(length: int = 20) -> str:
    """A fresh base32 secret (no padding) — what the QR code encodes."""
    return base64.b32encode(secrets.token_bytes(length)).decode("ascii").rstrip("=")


def provisioning_uri(secret: str, *, account: str, issuer: str = "OoLu") -> str:
    """The ``otpauth://`` URI an authenticator app imports (as a QR code).

    ``account`` labels the entry (the username); ``issuer`` names the app.
    """
    label = quote(f"{issuer}:{account}")
    query = (
        f"secret={secret}&issuer={quote(issuer)}"
        f"&algorithm={_ALGORITHM}&digits={_DIGITS}&period={_PERIOD}"
    )
    return f"otpauth://totp/{label}?{query}"


def _code_at(secret: str, counter: int) -> str:
    # base32 is case-insensitive and unpadded here; restore the padding.
    padding = "=" * ((8 - len(secret) % 8) % 8)
    key = base64.b32decode(secret.upper() + padding)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(truncated % (10**_DIGITS)).zfill(_DIGITS)


def code_at_time(secret: str, *, now: float) -> str:
    """The valid code at ``now`` (unix seconds) — used to drive tests and,
    on the client, never; the app the user holds computes this."""
    return _code_at(secret, int(now) // _PERIOD)


def verify(secret: str, code: str, *, now: float) -> bool:
    """True when ``code`` matches the current window (± one step of drift).
    Constant-time comparison; a malformed code is simply false, never a
    raise."""
    code = (code or "").strip()
    if len(code) != _DIGITS or not code.isdigit():
        return False
    counter = int(now) // _PERIOD
    for step in range(-_DRIFT, _DRIFT + 1):
        if hmac.compare_digest(code, _code_at(secret, counter + step)):
            return True
    return False
