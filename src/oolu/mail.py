"""Outbound mail + one-time codes: the verification backbone.

A public host must prove e-mail ownership (registration) and offer a way
back in (password reset). Both ride the same two pieces:

- ``MailSender`` — the outbound door. ``HttpMailSender`` speaks the
  Resend-style JSON API any modern sender offers; ``ConsoleMailSender``
  prints to the log for development; ``RecordingMailSender`` is the test
  double.
- ``MailCodeStore`` — hashed, expiring, attempt-limited one-time codes,
  and the durable "this address was verified" mark. Codes are stored only
  as salted hashes; redeeming is single-use.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

CODE_TTL_MINUTES = 30
MAX_ATTEMPTS = 5


@runtime_checkable
class MailSender(Protocol):
    def send(self, *, to: str, subject: str, body: str) -> None: ...


class ConsoleMailSender:
    """Development sender: the mail lands in the server log."""

    def __init__(self, log=print):
        self._log = log

    def send(self, *, to: str, subject: str, body: str) -> None:
        self._log(f"[mail] to={to} subject={subject}\n{body}")


class RecordingMailSender:
    """Test double: every mail is kept for assertions."""

    def __init__(self):
        self.sent: list[dict] = []

    def send(self, *, to: str, subject: str, body: str) -> None:
        self.sent.append({"to": to, "subject": subject, "body": body})


class HttpMailSender:
    """The Resend-style JSON door: POST {from, to, subject, text}.

    Works unchanged with Resend; Postmark/SES need only a different URL
    shape later — the seam is this class, not its callers.
    """

    def __init__(self, *, url: str, api_key: str, sender: str, transport=None):
        self._url = url.rstrip("/")
        self._api_key = api_key
        self._from = sender
        self._transport = transport

    def _transport_or_real(self):
        if self._transport is None:
            from .providers.transport import HttpxTransport

            self._transport = HttpxTransport()
        return self._transport

    def send(self, *, to: str, subject: str, body: str) -> None:
        response = self._transport_or_real().request(
            "POST",
            self._url,
            headers={"Authorization": f"Bearer {self._api_key}"},
            body={"from": self._from, "to": [to], "subject": subject, "text": body},
        )
        if response.status >= 300:
            raise RuntimeError(f"mail send failed: status {response.status}")


def _hash(email: str, purpose: str, code: str) -> str:
    return hashlib.sha256(f"{email}|{purpose}|{code}".encode()).hexdigest()


_SCHEMA = """CREATE TABLE IF NOT EXISTS mail_codes (
    email TEXT NOT NULL,
    purpose TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    verified_at TEXT,
    PRIMARY KEY (email, purpose)
)"""


class MailCodeStore:
    """One-time codes per (email, purpose), plus the verified mark."""

    def __init__(self, conn, *, clock=None) -> None:
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def issue(self, email: str, purpose: str) -> str:
        """Mint a fresh 6-digit code (replacing any prior one)."""
        code = f"{secrets.randbelow(1_000_000):06d}"
        expires = self._clock() + timedelta(minutes=CODE_TTL_MINUTES)
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO mail_codes
                     (email, purpose, code_hash, expires_at, attempts, verified_at)
                   VALUES (?, ?, ?, ?, 0, NULL)
                   ON CONFLICT(email, purpose) DO UPDATE SET
                     code_hash = excluded.code_hash,
                     expires_at = excluded.expires_at,
                     attempts = 0""",
                (email, purpose, _hash(email, purpose, code), expires.isoformat()),
            )
        return code

    def redeem(self, email: str, purpose: str, code: str) -> bool:
        """Single-use: a correct, unexpired code marks (email, purpose)
        verified and can never be replayed; wrong guesses are counted and
        the code dies after MAX_ATTEMPTS."""
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT code_hash, expires_at, attempts FROM mail_codes"
                " WHERE email = ? AND purpose = ?",
                (email, purpose),
            ).fetchone()
        if row is None or row["attempts"] >= MAX_ATTEMPTS:
            return False
        if self._clock() > datetime.fromisoformat(row["expires_at"]):
            return False
        if not hmac.compare_digest(row["code_hash"], _hash(email, purpose, code)):
            with self._conn.transaction() as db:
                db.execute(
                    "UPDATE mail_codes SET attempts = attempts + 1"
                    " WHERE email = ? AND purpose = ?",
                    (email, purpose),
                )
            return False
        with self._conn.transaction() as db:
            db.execute(
                # The hash is burned so the code is strictly single-use.
                "UPDATE mail_codes SET verified_at = ?, code_hash = ''"
                " WHERE email = ? AND purpose = ?",
                (self._clock().isoformat(), email, purpose),
            )
        return True

    def mark_verified(self, email: str, purpose: str) -> None:
        """Mark without a code — for flows that proved inbox control some
        other way (a redeemed password-reset code)."""
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO mail_codes
                     (email, purpose, code_hash, expires_at, attempts, verified_at)
                   VALUES (?, ?, '', ?, 0, ?)
                   ON CONFLICT(email, purpose) DO UPDATE SET
                     verified_at = excluded.verified_at, code_hash = ''""",
                (
                    email,
                    purpose,
                    self._clock().isoformat(),
                    self._clock().isoformat(),
                ),
            )

    def is_verified(self, email: str, purpose: str) -> bool:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT verified_at FROM mail_codes"
                " WHERE email = ? AND purpose = ?",
                (email, purpose),
            ).fetchone()
        return bool(row and row["verified_at"])


def build_mail_sender(environ: dict[str, Any]) -> MailSender | None:
    """The host's outbound door from environment configuration:
    OOLU_MAIL_URL + OOLU_MAIL_KEY + OOLU_MAIL_FROM → HttpMailSender;
    OOLU_MAIL=console → the development log sender; nothing → None."""
    if environ.get("OOLU_MAIL", "").strip().lower() == "console":
        return ConsoleMailSender()
    url = environ.get("OOLU_MAIL_URL", "").strip()
    key = environ.get("OOLU_MAIL_KEY", "").strip()
    sender = environ.get("OOLU_MAIL_FROM", "").strip()
    if url and key and sender:
        return HttpMailSender(url=url, api_key=key, sender=sender)
    return None
