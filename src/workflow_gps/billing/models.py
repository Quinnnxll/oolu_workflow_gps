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


class KycStatus(str, Enum):
    PENDING = "pending"
    VERIFIED = "verified"
    REJECTED = "rejected"


class ChargeStatus(str, Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class PayoutStatus(str, Enum):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"


class PayoutAccount(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int = BILLING_SCHEMA_VERSION
    noder_principal: str
    provider_account_id: str
    kyc_status: KycStatus = KycStatus.PENDING
    country: str = "US"
    currency: str = "usd"
    created_at: datetime = Field(default_factory=_now)


class PayoutBatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int = BILLING_SCHEMA_VERSION
    batch_id: str = Field(default_factory=_id)
    noder_principal: str
    amount_micros: int
    currency: str = "usd"
    status: PayoutStatus = PayoutStatus.PENDING
    provider_ref: str | None = None
    created_at: datetime = Field(default_factory=_now)


class ChargeReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider_ref: str
    amount_micros: int
    currency: str
    status: ChargeStatus


class PayoutReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider_ref: str
    amount_micros: int
    currency: str
    status: PayoutStatus
    fee_micros: int = 0
