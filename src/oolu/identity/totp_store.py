"""Durable, encrypted home for each account's TOTP secret.

The secret is shared material — anyone holding it can mint the user's
codes — so it is sealed with the install's machine key (the same primitive
the model keyring uses) and never leaves except to verify a submitted
code. Enrollment is two-step, mirroring every authenticator setup: begin
(hand out the secret + QR URI, provisionally) then confirm (prove the app
works by entering one code) — only a confirmed secret gates a payment.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from ..providers.keyring import _load_machine_key, _open, _seal
from . import totp

_SCHEMA = """CREATE TABLE IF NOT EXISTS totp_secrets (
    principal TEXT PRIMARY KEY,
    ciphertext TEXT NOT NULL,
    confirmed INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
)"""


class TotpStore:
    """One TOTP secret per principal, sealed at rest."""

    def __init__(
        self,
        conn,
        *,
        key_path: str | Path,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._machine_key = _load_machine_key(Path(key_path))
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def begin_enroll(self, principal: str, *, issuer: str = "OoLu") -> dict:
        """Mint a fresh secret (provisional) and return what a QR needs.

        Re-enrolling replaces any prior secret — and drops it back to
        unconfirmed, so a half-finished re-enroll can never leave a
        confirmed-but-wrong secret standing."""
        secret = totp.generate_secret()
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO totp_secrets (principal, ciphertext, confirmed, updated_at)
                   VALUES (?, ?, 0, ?)
                   ON CONFLICT(principal) DO UPDATE SET
                     ciphertext = excluded.ciphertext,
                     confirmed = 0,
                     updated_at = excluded.updated_at""",
                (principal, _seal(self._machine_key, secret.encode()), self._iso()),
            )
        return {
            "secret": secret,
            "uri": totp.provisioning_uri(secret, account=principal, issuer=issuer),
        }

    def confirm_enroll(self, principal: str, code: str, *, now: float) -> bool:
        """Prove the authenticator works: a valid code flips the secret to
        confirmed. Returns whether it took."""
        secret = self._secret(principal)
        if secret is None or not totp.verify(secret, code, now=now):
            return False
        with self._conn.transaction() as db:
            db.execute(
                "UPDATE totp_secrets SET confirmed = 1, updated_at = ? WHERE principal = ?",
                (self._iso(), principal),
            )
        return True

    def is_enrolled(self, principal: str) -> bool:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT confirmed FROM totp_secrets WHERE principal = ?", (principal,)
            ).fetchone()
        return bool(row and row["confirmed"])

    def verify(self, principal: str, code: str, *, now: float) -> bool:
        """True only when the principal has a CONFIRMED secret and the code
        matches. An unenrolled principal always fails — no code opens a
        door that was never set up."""
        if not self.is_enrolled(principal):
            return False
        secret = self._secret(principal)
        return secret is not None and totp.verify(secret, code, now=now)

    def disable(self, principal: str) -> bool:
        with self._conn.transaction() as db:
            cursor = db.execute(
                "DELETE FROM totp_secrets WHERE principal = ?", (principal,)
            )
        return int(getattr(cursor, "rowcount", 0) or 0) > 0

    def _secret(self, principal: str) -> str | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT ciphertext FROM totp_secrets WHERE principal = ?",
                (principal,),
            ).fetchone()
        if row is None:
            return None
        return _open(self._machine_key, row["ciphertext"]).decode()

    def _iso(self) -> str:
        moment = self._clock()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment.isoformat()
