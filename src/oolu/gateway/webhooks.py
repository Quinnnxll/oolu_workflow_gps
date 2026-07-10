"""Signed, replay-protected webhooks.

Outbound webhooks are signed with HMAC over the delivery id, a timestamp, and the
canonical payload. A receiver verifies the signature, rejects deliveries outside a
timestamp tolerance, and rejects any delivery id it has already seen — so a captured
webhook cannot be replayed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from .errors import WebhookError


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _sign(secret: bytes, delivery_id: str, timestamp: int, payload: dict) -> str:
    material = f"{delivery_id}.{timestamp}.{_canonical(payload)}".encode()
    return hmac.new(secret, material, hashlib.sha256).hexdigest()


class WebhookSigner:
    def __init__(self, secret: bytes | str):
        self._secret = secret.encode("utf-8") if isinstance(secret, str) else secret

    def sign(
        self,
        payload: dict,
        *,
        delivery_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, str]:
        delivery_id = delivery_id or uuid4().hex
        timestamp = int((now or datetime.now(UTC)).timestamp())
        signature = _sign(self._secret, delivery_id, timestamp, payload)
        return {
            "X-Webhook-Id": delivery_id,
            "X-Webhook-Timestamp": str(timestamp),
            "X-Webhook-Signature": signature,
        }


class WebhookVerifier:
    def __init__(
        self,
        secret: bytes | str,
        *,
        tolerance_seconds: int = 300,
        seen_ids: set[str] | None = None,
    ):
        self._secret = secret.encode("utf-8") if isinstance(secret, str) else secret
        self._tolerance = timedelta(seconds=tolerance_seconds)
        self._seen: set[str] = seen_ids if seen_ids is not None else set()

    def verify(
        self,
        payload: dict,
        headers: dict[str, str],
        *,
        now: datetime | None = None,
    ) -> None:
        moment = now or datetime.now(UTC)
        delivery_id = headers.get("X-Webhook-Id")
        raw_timestamp = headers.get("X-Webhook-Timestamp")
        signature = headers.get("X-Webhook-Signature")
        if not (delivery_id and raw_timestamp and signature):
            raise WebhookError("missing webhook signature headers")

        try:
            timestamp = int(raw_timestamp)
        except ValueError as exc:
            raise WebhookError("invalid webhook timestamp") from exc

        sent_at = datetime.fromtimestamp(timestamp, UTC)
        if abs(moment - sent_at) > self._tolerance:
            raise WebhookError("webhook timestamp outside tolerance (possible replay)")

        expected = _sign(self._secret, delivery_id, timestamp, payload)
        if not hmac.compare_digest(expected, signature):
            raise WebhookError("webhook signature verification failed")

        if delivery_id in self._seen:
            raise WebhookError("webhook delivery already processed (replay)")
        self._seen.add(delivery_id)


class StripeWebhookVerifier:
    """Stripe's ``Stripe-Signature`` scheme, as documented: the header
    carries ``t=<unix>,v1=<hex>[,v1=<hex>...]`` and each ``v1`` is
    HMAC-SHA256 over ``f"{t}.{raw_payload}"`` with the endpoint's
    ``whsec_...`` secret. Verified over the EXACT bytes on the wire —
    canonicalizing first would break real deliveries. Replay is bounded
    by the timestamp tolerance plus the handler's event-id idempotency.
    """

    def __init__(self, secret: str, *, tolerance_seconds: int = 300):
        self._secret = secret.encode("utf-8")
        self._tolerance = timedelta(seconds=tolerance_seconds)

    def sign_header(
        self, payload: bytes | str, *, now: datetime | None = None
    ) -> str:
        """The counterpart, for tests and simulations: a valid header."""
        timestamp = int((now or datetime.now(UTC)).timestamp())
        raw = payload if isinstance(payload, str) else payload.decode("utf-8")
        digest = hmac.new(
            self._secret, f"{timestamp}.{raw}".encode(), hashlib.sha256
        ).hexdigest()
        return f"t={timestamp},v1={digest}"

    def verify(
        self,
        payload: bytes | str,
        header: str | None,
        *,
        now: datetime | None = None,
    ) -> None:
        moment = now or datetime.now(UTC)
        if not header:
            raise WebhookError("missing Stripe-Signature header")
        timestamp: int | None = None
        signatures: list[str] = []
        for part in header.split(","):
            key, _, value = part.strip().partition("=")
            if key == "t":
                try:
                    timestamp = int(value)
                except ValueError as exc:
                    raise WebhookError("invalid Stripe-Signature timestamp") from exc
            elif key == "v1":
                signatures.append(value)
        if timestamp is None or not signatures:
            raise WebhookError("malformed Stripe-Signature header")
        sent_at = datetime.fromtimestamp(timestamp, UTC)
        if abs(moment - sent_at) > self._tolerance:
            raise WebhookError(
                "webhook timestamp outside tolerance (possible replay)"
            )
        raw = payload if isinstance(payload, str) else payload.decode("utf-8")
        expected = hmac.new(
            self._secret, f"{timestamp}.{raw}".encode(), hashlib.sha256
        ).hexdigest()
        if not any(hmac.compare_digest(expected, sig) for sig in signatures):
            raise WebhookError("webhook signature verification failed")
