"""Outbound SMS: the phone twin of the mail door.

"Continue with phone" needs exactly one outbound capability — send a
short text to a number — and this module is that seam. It mirrors
``mail.py`` deliberately: a Protocol, a console sender for development, a
recording double for tests, a generic HTTP sender for production, and an
environment builder the host calls once. One-time codes reuse
``MailCodeStore`` (the identifier column is any string; a phone number in
E.164 form is one), so codes hash, expire, and burn identically on both
doors.
"""

from __future__ import annotations

import re
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SmsSender(Protocol):
    def send(self, *, to: str, body: str) -> None: ...


class ConsoleSmsSender:
    """Development sender: the text lands in the server log."""

    def __init__(self, log=print):
        self._log = log

    def send(self, *, to: str, body: str) -> None:
        self._log(f"[sms] to={to}\n{body}")


class RecordingSmsSender:
    """Test double: every text is kept for assertions."""

    def __init__(self):
        self.sent: list[dict] = []

    def send(self, *, to: str, body: str) -> None:
        self.sent.append({"to": to, "body": body})


class HttpSmsSender:
    """A generic JSON door: POST {from, to, body} with a bearer key.

    Twilio-style providers need only a different URL shape later — the
    seam is this class, not its callers.
    """

    def __init__(self, *, url: str, api_key: str, sender: str, transport=None):
        self._url = url.rstrip("/")
        self._key = api_key
        self._sender = sender
        if transport is None:
            import httpx

            transport = httpx.Client(trust_env=True, timeout=15.0)
        self._transport = transport

    def send(self, *, to: str, body: str) -> None:
        response = self._transport.post(
            self._url,
            headers={
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
            },
            json={"from": self._sender, "to": to, "body": body},
        )
        status = getattr(response, "status_code", getattr(response, "status", 0))
        if not (200 <= int(status) < 300):
            raise RuntimeError(f"the SMS provider answered {status}")


# E.164-ish: an optional +, then 7-15 digits. Separators are forgiven on
# input and stripped in the canonical form.
_PHONE_DIGITS_RE = re.compile(r"\d")


def normalize_phone(raw: object) -> str:
    """A phone number in canonical form: ``+<digits>``.

    Spaces, dashes, dots, and parentheses are forgiven; anything else —
    letters, too few or too many digits — is refused in words. The
    canonical form is the identity codes and account links key on, so
    "+1 (555) 010-0000" and "+15550100000" are one number."""
    text = str(raw or "").strip()
    if not text:
        raise ValueError("a phone number is required")
    cleaned = text.replace(" ", "").replace("-", "").replace(".", "")
    cleaned = cleaned.replace("(", "").replace(")", "")
    plus = cleaned.startswith("+")
    digits = cleaned[1:] if plus else cleaned
    if not digits.isdigit():
        raise ValueError(f"'{text}' is not a phone number")
    if not 7 <= len(digits) <= 15:
        raise ValueError("a phone number has 7 to 15 digits")
    return "+" + digits


def build_sms_sender(environ: dict[str, Any]) -> SmsSender | None:
    """The host's outbound texts from environment configuration:
    OOLU_SMS_URL + OOLU_SMS_KEY + OOLU_SMS_FROM → HttpSmsSender;
    OOLU_SMS=console → the development log sender; nothing → None
    (the phone door answers 404 and the app hides the button)."""
    if environ.get("OOLU_SMS", "").strip().lower() == "console":
        return ConsoleSmsSender()
    url = environ.get("OOLU_SMS_URL", "").strip()
    key = environ.get("OOLU_SMS_KEY", "").strip()
    sender = environ.get("OOLU_SMS_FROM", "").strip()
    if url and key and sender:
        return HttpSmsSender(url=url, api_key=key, sender=sender)
    return None
