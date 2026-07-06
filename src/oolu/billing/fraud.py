from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from ..metering.models import NoderShare


class FraudVerdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    allowed: bool
    reasons: list[str]
    shares: list[NoderShare]


@runtime_checkable
class FraudSignals(Protocol):
    def assess(
        self,
        *,
        idempotency_key: str,
        consumer_principal: str | None,
        shares: list[NoderShare],
    ) -> FraudVerdict: ...


class DefaultFraudSignals:
    """Excludes self-dealing shares, rejects replayed successes, and throttles
    abusive velocity. Commission accrues only on genuine, first-time, arms-length
    success. ``seen`` is an optional durable predicate (True if the execution key
    was already processed) for replay detection that survives restarts."""

    def __init__(
        self,
        *,
        velocity_limit: int | None = None,
        seen: Callable[[str], bool] | None = None,
    ) -> None:
        self._velocity_limit = velocity_limit
        self._seen = seen
        self._seen_keys: set[str] = set()
        self._counts: dict[str, int] = {}

    def assess(
        self,
        *,
        idempotency_key: str,
        consumer_principal: str | None,
        shares: list[NoderShare],
    ) -> FraudVerdict:
        if idempotency_key in self._seen_keys or (
            self._seen is not None and self._seen(idempotency_key)
        ):
            return FraudVerdict(allowed=False, reasons=["replayed_success"], shares=[])
        self._seen_keys.add(idempotency_key)

        if consumer_principal is not None and self._velocity_limit is not None:
            self._counts[consumer_principal] = (
                self._counts.get(consumer_principal, 0) + 1
            )
            if self._counts[consumer_principal] > self._velocity_limit:
                return FraudVerdict(
                    allowed=False, reasons=["velocity_exceeded"], shares=[]
                )

        reasons: list[str] = []
        kept: list[NoderShare] = []
        for share in shares:
            if (
                consumer_principal is not None
                and share.noder_principal == consumer_principal
            ):
                reasons.append(f"self_dealing:{share.noder_principal}")
            else:
                kept.append(share)
        return FraudVerdict(allowed=True, reasons=reasons, shares=kept)
