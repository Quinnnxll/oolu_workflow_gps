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


class SmtpMailSender:
    """The classic door: any SMTP mailbox sends the codes.

    Most self-hosting operators have SMTP credentials (a Gmail app
    password, an Office 365 mailbox, their registrar's mail) long
    before they have an HTTP mail API — this sender is what makes the
    reset-code and emailed-password doors REAL on those hosts. Pure
    stdlib (``smtplib`` + ``email.message``); a fresh connection per
    send, closed either way — code mail is rare and must not hold a
    socket open between sends.

    ``security``: "starttls" (587, the default), "ssl" (465), or
    "none" (a relay inside the operator's own network only).
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        sender: str,
        username: str = "",
        password: str = "",
        security: str = "starttls",
        smtp=None,  # test seam: a class with the smtplib.SMTP surface
        smtp_ssl=None,
        timeout: float = 15.0,
    ):
        self._host = host
        self._port = int(port)
        self._from = sender
        self._username = username
        self._password = password
        self._security = security
        self._smtp = smtp
        self._smtp_ssl = smtp_ssl
        self._timeout = float(timeout)

    def send(self, *, to: str, subject: str, body: str) -> None:
        import smtplib
        from email.message import EmailMessage

        message = EmailMessage()
        message["From"] = self._from
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        if self._security == "ssl":
            maker = self._smtp_ssl or smtplib.SMTP_SSL
        else:
            maker = self._smtp or smtplib.SMTP
        connection = maker(self._host, self._port, timeout=self._timeout)
        try:
            if self._security == "starttls":
                connection.starttls()
            if self._username:
                connection.login(self._username, self._password)
            connection.send_message(message)
        finally:
            try:
                connection.quit()
            except Exception:  # noqa: BLE001 — the send already happened
                pass


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

    def forget(self, email: str) -> int:
        """Data-subject erasure: every code row for the address, gone
        (verified marks included — a deleted account owes no history)."""
        with self._conn.transaction() as db:
            cursor = db.execute(
                "DELETE FROM mail_codes WHERE email = ?", (email,)
            )
        return int(getattr(cursor, "rowcount", 0) or 0)

    def is_verified(self, email: str, purpose: str) -> bool:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT verified_at FROM mail_codes"
                " WHERE email = ? AND purpose = ?",
                (email, purpose),
            ).fetchone()
        return bool(row and row["verified_at"])


_THROTTLE_SCHEMA = """CREATE TABLE IF NOT EXISTS send_throttle (
    identifier TEXT NOT NULL,
    purpose TEXT NOT NULL,
    last_at TEXT NOT NULL,
    day_start TEXT NOT NULL,
    day_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (identifier, purpose)
)"""


class SendThrottle:
    """Pacing for the outbound doors — mail and SMS alike.

    Every public route that SENDS something on request (a reset mail, a
    sign-in text) is a lever a stranger can pull with nothing but an
    address: mail-bombing a victim's inbox, or running up the host's SMS
    bill one code at a time. This ledger bounds the lever per identifier
    and purpose: a cooldown between sends, and a daily cap. Refusals are
    the CALLER's to keep silent — the route answers exactly as if it had
    sent, so the throttle never becomes an enumeration oracle.
    """

    def __init__(self, conn, *, clock=None) -> None:
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_THROTTLE_SCHEMA)

    def allow(
        self,
        identifier: str,
        purpose: str,
        *,
        cooldown_s: float,
        per_day: int,
        now: datetime | None = None,
    ) -> bool:
        """True (and the send is recorded) iff this send may go out now."""
        moment = now or self._clock()
        with self._conn.transaction() as db:
            row = db.execute(
                "SELECT last_at, day_start, day_count FROM send_throttle"
                " WHERE identifier = ? AND purpose = ?",
                (identifier, purpose),
            ).fetchone()
            if row is None:
                db.execute(
                    """INSERT INTO send_throttle
                         (identifier, purpose, last_at, day_start, day_count)
                       VALUES (?, ?, ?, ?, 1)""",
                    (identifier, purpose, moment.isoformat(), moment.isoformat()),
                )
                return True
            last_at = datetime.fromisoformat(row["last_at"])
            if (moment - last_at).total_seconds() < cooldown_s:
                return False
            day_start = datetime.fromisoformat(row["day_start"])
            day_count = int(row["day_count"])
            if (moment - day_start) >= timedelta(days=1):
                day_start, day_count = moment, 0
            if day_count >= per_day:
                return False
            db.execute(
                """UPDATE send_throttle
                   SET last_at = ?, day_start = ?, day_count = ?
                   WHERE identifier = ? AND purpose = ?""",
                (
                    moment.isoformat(),
                    day_start.isoformat(),
                    day_count + 1,
                    identifier,
                    purpose,
                ),
            )
            return True


def build_mail_sender(environ: dict[str, Any]) -> MailSender | None:
    """The host's outbound door from environment configuration, in order:

    - ``OOLU_MAIL=console`` → the development log sender.
    - SMTP (the classic mailbox most operators already have):
      ``OOLU_SMTP_HOST`` + ``OOLU_MAIL_FROM`` → :class:`SmtpMailSender`,
      with ``OOLU_SMTP_PORT`` (default 587), ``OOLU_SMTP_USER`` +
      ``OOLU_SMTP_PASSWORD`` (optional for an open relay), and
      ``OOLU_SMTP_SECURITY`` = starttls (default) | ssl | none.
    - HTTP JSON door: ``OOLU_MAIL_URL`` + ``OOLU_MAIL_KEY`` +
      ``OOLU_MAIL_FROM`` → :class:`HttpMailSender`.
    - nothing → None (the mail doors answer 404 and the app hides them).

    A half-configured SMTP raises with the missing names — it must
    never silently fall through to a door that cannot send.
    """
    if environ.get("OOLU_MAIL", "").strip().lower() == "console":
        return ConsoleMailSender()
    sender = environ.get("OOLU_MAIL_FROM", "").strip()
    smtp_host = environ.get("OOLU_SMTP_HOST", "").strip()
    if smtp_host:
        if not sender:
            raise ValueError(
                "OOLU_SMTP_HOST is set but OOLU_MAIL_FROM is missing — "
                "name the address the codes are sent from"
            )
        security = (
            environ.get("OOLU_SMTP_SECURITY", "").strip().lower() or "starttls"
        )
        if security not in ("starttls", "ssl", "none"):
            raise ValueError(
                "OOLU_SMTP_SECURITY must be starttls, ssl, or none"
            )
        try:
            port = int(
                environ.get("OOLU_SMTP_PORT", "").strip()
                or ("465" if security == "ssl" else "587")
            )
        except ValueError as exc:
            raise ValueError("OOLU_SMTP_PORT must be a number") from exc
        return SmtpMailSender(
            host=smtp_host,
            port=port,
            sender=sender,
            username=environ.get("OOLU_SMTP_USER", "").strip(),
            password=environ.get("OOLU_SMTP_PASSWORD", ""),
            security=security,
        )
    url = environ.get("OOLU_MAIL_URL", "").strip()
    key = environ.get("OOLU_MAIL_KEY", "").strip()
    if url and key and sender:
        return HttpMailSender(url=url, api_key=key, sender=sender)
    return None
