from __future__ import annotations

from .registry import RegisteredSkill, SkillRegistry


def render_tool_manifest(skills: list[RegisteredSkill]) -> str:
    if not skills:
        return "No registered tools match this request."
    lines = ["Available tools (use only these):"]
    for entry in skills:
        params = ", ".join(p.name for p in entry.skill.parameters)
        signature = f"({params})" if params else "()"
        lines.append(
            f"- {entry.name}{signature} [{entry.skill_id}@{entry.semver}]: {entry.summary}"
        )
    return "\n".join(lines)


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
        return render_tool_manifest(self.select(intent))
