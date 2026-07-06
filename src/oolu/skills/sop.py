"""Human-defined Standard Operating Procedures as first-class typed objects.

An SOP is a small declarative document the user writes once. It is *compiled*
into structures the engine already enforces — never pasted into a prompt — so
SOP compliance is a property of the plan rather than of model behaviour:

- ``require_order``  -> hard blueprint edges (``provenance="sop"``); a route
  missing a required step is excluded, not silently reordered.
- ``forbid``         -> matching actions exclude the route, or become reserved
  (approval-gated) when ``unless_approved`` is set.
- ``approval``       -> matching actions become reserved actions, feeding the
  existing human-control -> approval-pause machinery.
- ``risk_budget``    -> an over-budget route forces approval on every
  non-read action.
- ``require_verify`` -> hard ``ConstraintSpec`` validators appended to skills.

The blueprint-side compilation lives in ``orchestrator.adaptive`` (blueprints
are an orchestrator concept); this module owns the SOP schema, parsing, and
the skill-side compilation, and keeps SOP structure distinguishable from
learned structure — evidence may prune learned edges, only the human may
change SOP edges.
"""

from __future__ import annotations

from fnmatch import fnmatchcase

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .models import ConstraintSeverity, ConstraintSpec, ReusableSkill


class ForbidRule(BaseModel):
    model_config = ConfigDict(frozen=True)

    operation: str  # fnmatch pattern against "operation" and "adapter/operation"
    unless_approved: bool = False


class ApprovalRule(BaseModel):
    model_config = ConfigDict(frozen=True)

    operations: list[str] = Field(default_factory=list)
    approvers: int = 1


class VerifyRule(BaseModel):
    model_config = ConfigDict(frozen=True)

    operation: str
    check: str  # validator name, e.g. "workspace.expected_artifacts"
    description: str = ""
    evidence: dict = Field(default_factory=dict)


class StandardOperatingProcedure(BaseModel):
    """A human-owned procedure. ``applies_to`` selects routes by tag or name."""

    model_config = ConfigDict(frozen=True)

    name: str
    applies_to: dict[str, list[str]] = Field(default_factory=dict)  # tags / names
    require_order: list[str] = Field(default_factory=list)  # operation patterns
    forbid: list[ForbidRule] = Field(default_factory=list)
    approval: ApprovalRule | None = None
    require_verify: list[VerifyRule] = Field(default_factory=list)
    risk_budget: float | None = None

    def matches(self, *, name: str, tags: list[str] | None = None) -> bool:
        wanted_tags = set(self.applies_to.get("tags", []))
        wanted_names = self.applies_to.get("names", [])
        if not wanted_tags and not wanted_names:
            return True  # an unscoped SOP applies to everything
        if wanted_tags & set(tags or []):
            return True
        return any(fnmatchcase(name, pattern) for pattern in wanted_names)


def parse_sop(text: str) -> StandardOperatingProcedure:
    """Parse one SOP from its YAML form.

    The document key ``sop`` names the procedure (matching the docs example);
    a top-level ``name`` key works too.
    """
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("an SOP document must be a mapping")
    if "sop" in data and "name" not in data:
        data = dict(data)
        data["name"] = data.pop("sop")
    return StandardOperatingProcedure.model_validate(data)


def operation_matches(pattern: str, *, adapter: str, operation: str) -> bool:
    """Match an SOP operation pattern against an action's labels."""
    return fnmatchcase(operation, pattern) or fnmatchcase(
        f"{adapter}/{operation}", pattern
    )


def apply_sop_to_skill(
    skill: ReusableSkill, sop: StandardOperatingProcedure
) -> ReusableSkill:
    """Append the SOP's mandatory verifications as hard validators."""
    if not sop.require_verify:
        return skill
    validators = list(skill.validators)
    known = {v.id for v in validators}
    for rule in sop.require_verify:
        if not any(
            operation_matches(
                rule.operation, adapter=action.adapter, operation=action.operation
            )
            for action in skill.actions
        ):
            continue
        spec_id = f"sop:{sop.name}:{rule.operation}:{rule.check}"
        if spec_id in known:
            continue
        validators.append(
            ConstraintSpec(
                id=spec_id,
                description=rule.description
                or f"SOP '{sop.name}' requires check '{rule.check}'",
                validator=rule.check,
                severity=ConstraintSeverity.HARD,
                evidence=dict(rule.evidence),
            )
        )
        known.add(spec_id)
    return skill.model_copy(update={"validators": validators})
