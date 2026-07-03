from __future__ import annotations

from .discovery import DiscoveredTool
from .registry import RegisteredSkill, SkillRegistry, _tokens

_EMPTY = "No registered skills or local tools match this request."


def render_tool_manifest(skills: list[RegisteredSkill]) -> str:
    if not skills:
        return ""
    lines = ["Registered skills (invoke by id):"]
    for entry in skills:
        params = ", ".join(p.name for p in entry.skill.parameters)
        signature = f"({params})" if params else "()"
        lines.append(
            f"- {entry.name}{signature} [{entry.skill_id}@{entry.semver}]: {entry.summary}"
        )
    return "\n".join(lines)


def render_tool_env(tools: list[DiscoveredTool]) -> str:
    if not tools:
        return ""
    lines = ["Local tools installed on this machine (call by name):"]
    for tool in tools:
        tags = ", ".join(tool.tags)
        lines.append(f"- {tool.name} [{tool.category}] at {tool.path}: {tags}")
    return "\n".join(lines)


def select_tools(
    tools: list[DiscoveredTool], intent: str, *, limit: int = 8
) -> list[DiscoveredTool]:
    terms = set(_tokens(intent))
    if not terms:
        return list(tools)[:limit]
    scored: list[tuple[int, DiscoveredTool]] = []
    for tool in tools:
        fields = (
            set(_tokens(tool.name))
            | set(_tokens(" ".join(tool.tags)))
            | set(_tokens(tool.category))
        )
        score = len(terms & fields)
        if score:
            scored.append((score, tool))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [tool for _, tool in scored[:limit]]


class SkillContextBuilder:
    def __init__(self, registry: SkillRegistry, *, max_tools: int = 8):
        self._registry = registry
        self._max_tools = max_tools

    def select(self, intent: str) -> list[RegisteredSkill]:
        return [
            scored.skill
            for scored in self._registry.search(intent, limit=self._max_tools)
        ]

    def manifest(self, intent: str) -> str:
        return render_tool_manifest(self.select(intent)) or _EMPTY


class PlanningContextBuilder:
    """Renders the relevant registered skills *and* local tools for one intent, so
    the fast tier grounds a request onto what is actually available here."""

    def __init__(
        self,
        registry: SkillRegistry | None = None,
        *,
        tools: list[DiscoveredTool] | None = None,
        max_skills: int = 8,
        max_tools: int = 8,
    ):
        self._registry = registry
        self._tools = list(tools or [])
        self._max_skills = max_skills
        self._max_tools = max_tools

    def manifest(self, intent: str) -> str:
        parts: list[str] = []
        if self._registry is not None:
            skills = [
                scored.skill
                for scored in self._registry.search(intent, limit=self._max_skills)
            ]
            parts.append(render_tool_manifest(skills))
        if self._tools:
            parts.append(
                render_tool_env(
                    select_tools(self._tools, intent, limit=self._max_tools)
                )
            )
        rendered = "\n\n".join(part for part in parts if part)
        return rendered or _EMPTY
