from __future__ import annotations

from ..skills.models import ReusableSkill
from ..skills.requirements import RequirementBrief
from .state import Blueprint, ReservedAction, SemanticEdge, SemanticGrounding

_READ = frozenset(
    {"get", "list", "read", "search", "fetch", "describe", "show", "query", "view"}
)
_IRREVERSIBLE = frozenset(
    {"delete", "remove", "drop", "destroy", "purge", "revoke", "wipe"}
)


def classify_risk(operation: str) -> str:
    verb = operation.split(".")[-1].split("_")[0].split("-")[0].lower()
    if verb in _IRREVERSIBLE:
        return "irreversible"
    if verb in _READ:
        return "read"
    return "write"


def _reserved_action(action) -> ReservedAction:
    risk = classify_risk(action.operation)
    return ReservedAction(
        action=action,
        required_capabilities=frozenset({action.operation}),
        reserved=risk == "irreversible",
        risk=risk,
    )


class SkillRegistryPlanner:
    def __init__(self, skills: list[ReusableSkill]):
        self._skills = list(skills)

    def capabilities(self) -> frozenset[str]:
        caps: set[str] = set()
        for skill in self._skills:
            caps |= {action.operation for action in skill.actions}
        return frozenset(caps)

    def blueprints(self) -> list[Blueprint]:
        plans: list[Blueprint] = []
        for skill in self._skills:
            if not skill.actions:
                continue
            actions = [_reserved_action(action) for action in skill.actions]
            plans.append(
                Blueprint(
                    name=skill.name,
                    actions=actions,
                    estimated_cost=float(len(actions)),
                )
            )
        return plans


class RegistryGrounder:
    def __init__(self, capabilities: frozenset[str]):
        self._capabilities = frozenset(capabilities)

    def ground(self, brief: RequirementBrief) -> SemanticGrounding:
        edges = [
            SemanticEdge(source=param.name, target=param.name)
            for param in brief.parameters
        ]
        return SemanticGrounding(
            edges=edges,
            resolved_capabilities=self._capabilities,
            unresolved_terms=[],
        )
