"""Signed, expiring, audience-bound, single-use worker task leases.

A lease is the only thing that authorizes a worker to execute a task. The control
plane signs it; a worker verifies it before doing any work. Verification rejects:

- a malformed or wrongly-signed token (a "lost"/forged lease cannot execute);
- an expired lease;
- a lease whose audience is a different worker;
- a revoked lease (e.g. cancelled);
- a replayed lease — leases are single-use against a ledger, so a duplicated lease
  cannot execute twice.

Leases are signed with HMAC: the control plane and its workers share a symmetric
key (per-worker keys are a natural extension), which is appropriate here because
both ends are first-party — unlike user identity, which needs an asymmetric IdP.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .errors import (
    AudienceMismatch,
    ExpiredLease,
    InvalidLease,
    ReplayedLease,
    RevokedLease,
)
from .ledger import LeaseLedger


class TrustLevel(str, Enum):
    """Whether a task is untrusted synthesized code or a trusted local skill."""

    UNTRUSTED_SYNTHESIZED = "untrusted_synthesized"
    TRUSTED_LOCAL_SKILL = "trusted_local_skill"


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


class WorkerTaskLease(BaseModel):
    """The signed authorization to run one task on one worker for a bounded time."""

    model_config = ConfigDict(frozen=True)

    lease_id: str = Field(default_factory=lambda: uuid4().hex)
    task_id: str
    run_id: str | None = None
    tenant_id: str
    audience: str  # the worker id this lease is bound to
    capabilities: frozenset[str] = Field(default_factory=frozenset)
    trust_level: TrustLevel
    backend_required: str
    timeout_seconds: float = 30.0
    issuer: str = "control-plane"
    issued_at: datetime
    expires_at: datetime


class LeaseSigner:
    """Signs leases with HMAC-SHA256."""

    def __init__(self, secret: bytes | str):
        self._secret = secret.encode("utf-8") if isinstance(secret, str) else secret

    def sign(self, lease: WorkerTaskLease) -> str:
        payload = _b64url_encode(lease.model_dump_json().encode("utf-8"))
        signature = hmac.new(
            self._secret, payload.encode("ascii"), hashlib.sha256
        ).digest()
        return f"{payload}.{_b64url_encode(signature)}"


class LeaseVerifier:
    """Verifies a lease against this worker's audience, signature, and the ledger."""

    def __init__(
        self,
        secret: bytes | str,
        *,
        audience: str,
        ledger: LeaseLedger,
        leeway_seconds: float = 5.0,
    ):
        self._secret = secret.encode("utf-8") if isinstance(secret, str) else secret
        self._audience = audience
        self._ledger = ledger
        self._leeway = timedelta(seconds=leeway_seconds)

    def verify(self, token: str, *, now: datetime | None = None) -> WorkerTaskLease:
        moment = now or datetime.now(UTC)
        lease = self._parse_and_authenticate(token)

        if lease.audience != self._audience:
            raise AudienceMismatch(
                f"lease audience {lease.audience!r} != worker {self._audience!r}"
            )
        if moment > lease.expires_at + self._leeway:
            raise ExpiredLease(f"lease {lease.lease_id} expired at {lease.expires_at}")
        if self._ledger.is_revoked(lease.lease_id):
            raise RevokedLease(f"lease {lease.lease_id} was revoked")
        # Single-use: consuming an already-consumed lease is a replay.
        if not self._ledger.consume(lease.lease_id):
            raise ReplayedLease(f"lease {lease.lease_id} was already used")
        return lease

    def _parse_and_authenticate(self, token: str) -> WorkerTaskLease:
        parts = token.split(".")
        if len(parts) != 2:
            raise InvalidLease("malformed lease token")
        payload_b64, signature_b64 = parts
        expected = hmac.new(
            self._secret, payload_b64.encode("ascii"), hashlib.sha256
        ).digest()
        try:
            provided = _b64url_decode(signature_b64)
        except (ValueError, TypeError) as exc:
            raise InvalidLease("malformed lease signature") from exc
        if not hmac.compare_digest(expected, provided):
            raise InvalidLease("lease signature verification failed")
        try:
            payload = _b64url_decode(payload_b64).decode("utf-8")
            data: dict[str, Any] = json.loads(payload)
            return WorkerTaskLease.model_validate(data)
        except (ValueError, TypeError) as exc:
            raise InvalidLease(f"malformed lease payload: {exc}") from exc
