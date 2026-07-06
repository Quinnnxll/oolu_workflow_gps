from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

METERING_SCHEMA_VERSION = 1


def _id() -> str:
    return uuid4().hex


def _now() -> datetime:
    return datetime.now(UTC)


class MeteringEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int = METERING_SCHEMA_VERSION
    event_id: str = Field(default_factory=_id)
    idempotency_key: str
    run_id: str
    version_id: str | None = None
    consumer_tenant: str | None = None
    consumer_principal: str | None = None
    outcome: str
    gross: float | None = None
    provider_cost: float | None = None
    audit_seq: int
    occurred_at: datetime
    recorded_at: datetime = Field(default_factory=_now)


class NoderShare(BaseModel):
    model_config = ConfigDict(frozen=True)

    noder_principal: str
    weight: float
    multiplier: float = 1.0


class RunBinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int = METERING_SCHEMA_VERSION
    run_id: str
    version_id: str
    consumer_tenant: str
    consumer_principal: str | None = None
    gross: float = 0.0
    provider_cost: float = 0.0
    shares: list[NoderShare] = Field(default_factory=list)
    # What kind of job this run mostly was (a market class key). Lets the
    # budget layer learn spending behavior per class of goal — lavish in
    # one class never loosens (or tightens) another.
    goal_class: str | None = None


class AttributionRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int = METERING_SCHEMA_VERSION
    event_id: str
    noder_principal: str
    weight: float
    multiplier: float = 1.0
