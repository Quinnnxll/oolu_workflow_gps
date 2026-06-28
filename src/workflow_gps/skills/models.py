"""Versioned, deployment-neutral records for demonstrations and reusable skills."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

SKILL_SCHEMA_VERSION = 1


def _id() -> str:
    return uuid4().hex


def _now() -> datetime:
    return datetime.now(UTC)


class ConstraintSeverity(str, Enum):
    HARD = "hard"
    SOFT = "soft"


class ConstraintStatus(str, Enum):
    UNRESOLVED = "unresolved"
    SATISFIED = "satisfied"
    VIOLATED = "violated"


class ExecutionStatus(str, Enum):
    PLANNED = "planned"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class ActionEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int = SKILL_SCHEMA_VERSION
    id: str = Field(default_factory=_id)
    correlation_id: str
    adapter: str
    operation: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    actor: str | None = None
    credential_ref: str | None = None
    observed_at: datetime = Field(default_factory=_now)


class StateSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int = SKILL_SCHEMA_VERSION
    fingerprint: str
    state: dict[str, Any] = Field(default_factory=dict)
    artifact_refs: list[str] = Field(default_factory=list)
    captured_at: datetime = Field(default_factory=_now)


class ConstraintSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    description: str
    validator: str
    severity: ConstraintSeverity = ConstraintSeverity.HARD
    status: ConstraintStatus = ConstraintStatus.UNRESOLVED
    evidence: dict[str, Any] = Field(default_factory=dict)


class Demonstration(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int = SKILL_SCHEMA_VERSION
    id: str = Field(default_factory=_id)
    intent: str
    actions: list[ActionEvent]
    before: StateSnapshot | None = None
    after: StateSnapshot | None = None
    constraints: list[ConstraintSpec] = Field(default_factory=list)
    outcome: ExecutionStatus
    evidence: dict[str, Any] = Field(default_factory=dict)
    application: str | None = None
    application_version: str | None = None
    created_at: datetime = Field(default_factory=_now)


class SkillSignature(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int = SKILL_SCHEMA_VERSION
    application: str
    application_version: str | None = None
    adapter: str
    environment_fingerprint: str | None = None
    artifact_fingerprints: dict[str, str] = Field(default_factory=dict)


class SkillParameter(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    value_type: str
    required: bool = True
    unit: str | None = None
    description: str | None = None
    domain: dict[str, Any] = Field(default_factory=dict)


class ReusableSkill(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int = SKILL_SCHEMA_VERSION
    id: str = Field(default_factory=_id)
    name: str
    description: str
    signature: SkillSignature
    parameters: list[SkillParameter] = Field(default_factory=list)
    preconditions: list[ConstraintSpec] = Field(default_factory=list)
    actions: list[ActionEvent]
    validators: list[ConstraintSpec] = Field(default_factory=list)
    recovery_actions: list[ActionEvent] = Field(default_factory=list)
    demonstration_ids: list[str] = Field(default_factory=list)
    credential_refs: list[str] = Field(default_factory=list)
    success_count: int = 0
    failure_count: int = 0
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class ExecutionOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int = SKILL_SCHEMA_VERSION
    id: str = Field(default_factory=_id)
    idempotency_key: str
    skill_id: str
    status: ExecutionStatus
    state_delta: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    recovery_status: ExecutionStatus | None = None
    started_at: datetime = Field(default_factory=_now)
    completed_at: datetime | None = None


class ApprovalRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int = SKILL_SCHEMA_VERSION
    id: str = Field(default_factory=_id)
    principal: str
    policy: str
    decision: Literal["approved", "denied"]
    scope: dict[str, Any] = Field(default_factory=dict)
    evidence_hash: str | None = None
    expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=_now)
