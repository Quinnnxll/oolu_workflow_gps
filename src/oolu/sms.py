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

    For providers that speak this exact shape. Twilio — by far the most
    common — does NOT (form-encoded body, HTTP Basic auth); it has its
    own sender below, and ``build_sms_sender`` picks the right one.
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


# Twilio's REST shape, hard-won from real deployments where the generic
# JSON+Bearer door silently 401s: the message resource under the account,
# an application/x-www-form-urlencoded body with capitalized field names,
# and HTTP Basic auth (Account SID as the user, Auth Token as the pass).
TWILIO_API_ROOT = "https://api.twilio.com/2010-04-01"


class TwilioSmsSender:
    """The real Twilio door — the fix for 'phone registration doesn't work'.

    A deployer sets ``OOLU_TWILIO_ACCOUNT_SID`` + ``OOLU_TWILIO_AUTH_TOKEN``
    + ``OOLU_SMS_FROM`` (a Twilio number in E.164, or a Messaging Service
    SID) and the phone door lights up. The endpoint is derived from the
    account SID; override it only for a compatible mock via ``url``.
    """

    def __init__(
        self,
        *,
        account_sid: str,
        auth_token: str,
        sender: str,
        url: str | None = None,
        transport=None,
    ):
        self._sid = account_sid
        self._token = auth_token
        self._sender = sender
        self._url = (
            url.rstrip("/")
            if url
            else f"{TWILIO_API_ROOT}/Accounts/{account_sid}/Messages.json"
        )
        if transport is None:
            import httpx

            transport = httpx.Client(trust_env=True, timeout=15.0)
        self._transport = transport

    def send(self, *, to: str, body: str) -> None:
        # A Messaging Service SID (MG...) goes in MessagingServiceSid; a
        # plain number goes in From. Twilio accepts exactly one.
        form = {"To": to, "Body": body}
        if self._sender.startswith("MG"):
            form["MessagingServiceSid"] = self._sender
        else:
            form["From"] = self._sender
        response = self._transport.post(
            self._url,
            data=form,
            auth=(self._sid, self._token),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        status = getattr(response, "status_code", getattr(response, "status", 0))
        if not (200 <= int(status) < 300):
            detail = _twilio_error(response)
            raise RuntimeError(f"Twilio answered {status}{detail}")


def _twilio_error(response) -> str:
    """Twilio's own error message, when the response carries one — so a
    misconfigured door says *why* (bad number, unverified sender) instead
    of just a status code."""
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 - a body we can't read is no worse
        return ""
    message = payload.get("message") if isinstance(payload, dict) else None
    return f": {message}" if message else ""


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
    """The host's outbound texts from environment configuration. In order:

    - ``OOLU_SMS=console`` → the development log sender (the code prints to
      the server log so phone sign-up can be exercised with no provider).
    - Twilio (the common real provider): ``OOLU_TWILIO_ACCOUNT_SID`` +
      ``OOLU_TWILIO_AUTH_TOKEN`` + ``OOLU_SMS_FROM`` → ``TwilioSmsSender``.
      Also chosen when ``OOLU_SMS_PROVIDER=twilio`` or the generic URL is a
      twilio.com endpoint, so a half-configured Twilio never silently falls
      back to the JSON door that can't talk to it.
    - Generic JSON door: ``OOLU_SMS_URL`` + ``OOLU_SMS_KEY`` +
      ``OOLU_SMS_FROM`` → ``HttpSmsSender``.
    - nothing → None (the phone door answers 404 and the app hides the
      button).
    """
    if environ.get("OOLU_SMS", "").strip().lower() == "console":
        return ConsoleSmsSender()
    provider = environ.get("OOLU_SMS_PROVIDER", "").strip().lower()
    url = environ.get("OOLU_SMS_URL", "").strip()
    key = environ.get("OOLU_SMS_KEY", "").strip()
    sender = environ.get("OOLU_SMS_FROM", "").strip()
    account_sid = environ.get("OOLU_TWILIO_ACCOUNT_SID", "").strip()
    auth_token = environ.get("OOLU_TWILIO_AUTH_TOKEN", "").strip()

    wants_twilio = (
        provider == "twilio"
        or bool(account_sid and auth_token)
        or "twilio.com" in url
    )
    if wants_twilio:
        # Basic auth may come from the Twilio-specific vars or, for a
        # deployer who put the SID/token in the generic KEY slot as
        # "sid:token", from there — either way, be explicit if incomplete.
        if not account_sid and ":" in key:
            account_sid, _, auth_token = key.partition(":")
            account_sid, auth_token = account_sid.strip(), auth_token.strip()
        if account_sid and auth_token and sender:
            return TwilioSmsSender(
                account_sid=account_sid,
                auth_token=auth_token,
                sender=sender,
                url=url or None,
            )
        raise ValueError(
            "Twilio SMS is selected but incomplete: set OOLU_TWILIO_ACCOUNT_SID,"
            " OOLU_TWILIO_AUTH_TOKEN, and OOLU_SMS_FROM (a Twilio number or"
            " Messaging Service SID)."
        )
    if url and key and sender:
        return HttpSmsSender(url=url, api_key=key, sender=sender)
    return None
