from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from ..skills.contract import Slot

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


class LineageRecord(BaseModel):
    """One ancestor in a version's derivation chain (level 1 = direct parent).

    Recorded at contribution time from ``derived_from`` and immutable
    thereafter — the provenance that fills royalty ancestors automatically
    when the version is bound to a paying run.
    """

    model_config = ConfigDict(frozen=True)

    ancestor_version_id: str
    ancestor_noder_principal: str
    level: int = Field(ge=1)


# Royalties reach at most this many ancestry levels; beyond it the geometric
# decay (0.35^level) makes shares economically negligible anyway.
MAX_LINEAGE_DEPTH = 5


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
    lineage: list[LineageRecord] = Field(default_factory=list)
    published_at: datetime = Field(default_factory=_now)


class Listing(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int = NODEPLACE_SCHEMA_VERSION
    listing_id: str = Field(default_factory=_id)
    version_id: str
    title: str
    summary: str
    tags: list[str] = Field(default_factory=list)
    # The slot vocabulary: what this node consumes and produces, in the same
    # typed terms the goal assembler plans over. Defaults are derived from
    # the contributed skill; noders may declare richer vocabularies.
    consumes: list[Slot] = Field(default_factory=list)
    produces: list[Slot] = Field(default_factory=list)
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


class Rating(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int = NODEPLACE_SCHEMA_VERSION
    rating_id: str = Field(default_factory=_id)
    subject_version_id: str
    rater_principal: str
    score: int
    text: str = ""
    verified_run: bool = True
    created_at: datetime = Field(default_factory=_now)
