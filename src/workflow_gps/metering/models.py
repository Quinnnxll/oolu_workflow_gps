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
    outcome: str
    audit_seq: int
    occurred_at: datetime
    recorded_at: datetime = Field(default_factory=_now)
