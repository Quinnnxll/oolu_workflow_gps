from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

NODEPLACE_SCHEMA_VERSION = 1


def _id() -> str:
    return uuid4().hex


def _now() -> datetime:
    return datetime.now(UTC)


class Visibility(str, Enum):
    PRIVATE = "private"
    UNLISTED = "unlisted"
    PUBLIC = "public"


class ListingStatus(str, Enum):
    DRAFT = "draft"
    IN_REVIEW = "in_review"
    ACTIVE = "active"
    SUSPENDED = "suspended"


class PricingModel(str, Enum):
    PER_SUCCESS = "per_success"
    SUBSCRIPTION = "subscription"
    FREE = "free"


class CostRecovery(str, Enum):
    ABSORB = "absorb"
    PASSTHROUGH = "passthrough"


class Node(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int = NODEPLACE_SCHEMA_VERSION
    node_id: str = Field(default_factory=_id)
    noder_principal: str
    tenant_id: str
    skill_id: str
    visibility: Visibility = Visibility.PRIVATE
    created_at: datetime = Field(default_factory=_now)
    revoked_at: datetime | None = None


class NodeVersion(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int = NODEPLACE_SCHEMA_VERSION
    version_id: str = Field(default_factory=_id)
    node_id: str
    semver: str
    content_hash: str
    sanitized_skill_json: str
    license: str = "proprietary"
    backend: str = "docker"
    requires_approval: bool = True
    published_at: datetime = Field(default_factory=_now)


class Listing(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int = NODEPLACE_SCHEMA_VERSION
    listing_id: str = Field(default_factory=_id)
    version_id: str
    title: str
    summary: str
    tags: list[str] = Field(default_factory=list)
    maturity_label: str = "experimental"
    status: ListingStatus = ListingStatus.DRAFT
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class PricingPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int = NODEPLACE_SCHEMA_VERSION
    policy_id: str = Field(default_factory=_id)
    version_id: str
    model: PricingModel = PricingModel.FREE
    unit_price: float = 0.0
    currency: str = "USD"
    cost_recovery: CostRecovery = CostRecovery.ABSORB
