from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

BILLING_SCHEMA_VERSION = 1

MICROS_PER_UNIT = 1_000_000


def _id() -> str:
    return uuid4().hex


def _now() -> datetime:
    return datetime.now(UTC)


def to_micros(amount: float) -> int:
    return round(amount * MICROS_PER_UNIT)


def to_units(micros: int) -> float:
    return micros / MICROS_PER_UNIT


class EarningsKind(str, Enum):
    ACCRUAL = "accrual"
    RESERVE = "reserve"
    CLAWBACK = "clawback"
    PAYOUT = "payout"


class EarningsEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int = BILLING_SCHEMA_VERSION
    entry_id: str = Field(default_factory=_id)
    noder_principal: str
    event_id: str | None = None
    amount_micros: int
    kind: EarningsKind = EarningsKind.ACCRUAL
    available_at: datetime = Field(default_factory=_now)
    created_at: datetime = Field(default_factory=_now)


class NoderBalance(BaseModel):
    model_config = ConfigDict(frozen=True)

    noder_principal: str
    available_micros: int = 0
    pending_micros: int = 0
    reserved_micros: int = 0
    lifetime_paid_micros: int = 0
