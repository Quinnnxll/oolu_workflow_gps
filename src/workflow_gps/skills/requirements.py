"""Requirement and Constraint Compiler for underspecified, parametric work."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import (
    SKILL_SCHEMA_VERSION,
    ConstraintSeverity,
    ConstraintSpec,
    ConstraintStatus,
)


class AuthorizationMode(str, Enum):
    GUIDED = "guided"
    EXPLORE = "explore"
    AUTONOMOUS_WITHIN_BOUNDS = "autonomous_within_bounds"
    FULLY_DELEGATED = "fully_delegated"


class ParameterSource(str, Enum):
    UNRESOLVED = "unresolved"
    USER = "user"
    DERIVED = "derived"
    AUTHORITY = "authority"
    DELEGATED = "delegated"


class BriefStatus(str, Enum):
    CLARIFYING = "clarifying"
    BLOCKED = "blocked"
    READY_FOR_SOLVER = "ready_for_solver"
    READY_FOR_VALIDATION = "ready_for_validation"
    READY_FOR_PRODUCTION = "ready_for_production"


class ParameterDomain(BaseModel):
    model_config = ConfigDict(frozen=True)

    value_type: str
    unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    options: list[Any] = Field(default_factory=list)


class RequirementParameter(BaseModel):
    """Suggested values are deliberately separate from the selected value."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    domain: ParameterDomain
    required: bool = True
    value: Any | None = None
    source: ParameterSource = ParameterSource.UNRESOLVED
    suggested_values: list[Any] = Field(default_factory=list)
    question: str | None = None
    question_priority: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def selected_values_require_provenance(self):
        if self.value is not None and self.source is ParameterSource.UNRESOLVED:
            raise ValueError("a selected parameter value must declare its source")
        if self.value is None and self.source in {
            ParameterSource.USER,
            ParameterSource.DERIVED,
            ParameterSource.AUTHORITY,
        }:
            raise ValueError("a resolved parameter source requires a selected value")
        return self


class AuthorizationGrant(BaseModel):
    model_config = ConfigDict(frozen=True)

    mode: AuthorizationMode = AuthorizationMode.GUIDED
    delegated_parameters: frozenset[str] = Field(default_factory=frozenset)
    allow_all_unspecified: bool = False
    objectives: list[str] = Field(default_factory=list)
    bounds: dict[str, ParameterDomain] = Field(default_factory=dict)
    reproducibility_seed: int | None = None

    def permits(self, parameter_name: str) -> bool:
        if self.mode is AuthorizationMode.FULLY_DELEGATED:
            return True
        if self.mode is AuthorizationMode.GUIDED:
            return False
        return self.allow_all_unspecified or parameter_name in self.delegated_parameters


class RequirementBrief(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int = SKILL_SCHEMA_VERSION
    intent: str
    parameters: list[RequirementParameter] = Field(default_factory=list)
    constraints: list[ConstraintSpec] = Field(default_factory=list)
    authorization: AuthorizationGrant = Field(default_factory=AuthorizationGrant)


class ClarificationQuestion(BaseModel):
    model_config = ConfigDict(frozen=True)

    parameter: str
    question: str
    suggested_values: list[Any] = Field(default_factory=list)
    domain: ParameterDomain
    priority: int


class CompilationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: BriefStatus
    unresolved_parameters: list[str] = Field(default_factory=list)
    delegated_parameters: list[str] = Field(default_factory=list)
    unresolved_hard_constraints: list[str] = Field(default_factory=list)
    violated_hard_constraints: list[str] = Field(default_factory=list)
    questions: list[ClarificationQuestion] = Field(default_factory=list)
    can_generate_candidates: bool = False
    can_produce: bool = False


class RequirementConstraintCompiler:
    """Assesses readiness without inventing or silently binding parameter values."""

    def compile(self, brief: RequirementBrief) -> CompilationResult:
        missing = [
            item for item in brief.parameters if item.required and item.value is None
        ]
        delegated = [item for item in missing if brief.authorization.permits(item.name)]
        unresolved = [item for item in missing if item not in delegated]
        violated = [
            item.id
            for item in brief.constraints
            if item.severity is ConstraintSeverity.HARD
            and item.status is ConstraintStatus.VIOLATED
        ]
        unvalidated = [
            item.id
            for item in brief.constraints
            if item.severity is ConstraintSeverity.HARD
            and item.status is ConstraintStatus.UNRESOLVED
        ]

        questions = [
            ClarificationQuestion(
                parameter=item.name,
                question=item.question or f"What should '{item.name}' be?",
                suggested_values=list(item.suggested_values),
                domain=item.domain,
                priority=item.question_priority,
            )
            for item in sorted(
                unresolved, key=lambda value: value.question_priority, reverse=True
            )
        ]

        if violated:
            status = BriefStatus.BLOCKED
        elif unresolved:
            status = BriefStatus.CLARIFYING
        elif missing:
            status = BriefStatus.READY_FOR_SOLVER
        elif unvalidated:
            status = BriefStatus.READY_FOR_VALIDATION
        else:
            status = BriefStatus.READY_FOR_PRODUCTION

        return CompilationResult(
            status=status,
            unresolved_parameters=[item.name for item in unresolved],
            delegated_parameters=[item.name for item in delegated],
            unresolved_hard_constraints=unvalidated,
            violated_hard_constraints=violated,
            questions=questions,
            can_generate_candidates=not violated and not unresolved,
            can_produce=status is BriefStatus.READY_FOR_PRODUCTION,
        )
