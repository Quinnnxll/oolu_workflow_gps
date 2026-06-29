"""Gateway error type carrying an HTTP status and a stable machine code."""

from __future__ import annotations


class GatewayError(Exception):
    """An error that maps directly to an HTTP response."""

    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(f"{status} {code}: {message}")


class WebhookError(Exception):
    """A webhook delivery failed verification (bad signature, replay, or skew)."""
