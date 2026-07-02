from __future__ import annotations

import re
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .models import ActionEvent, ReusableSkill, SkillParameter, SkillSignature
from .registry import RegisteredSkill, SkillRegistry

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_STARTER_PACK = "starter.yaml"


def _slug(name: str) -> str:
    return _SLUG_RE.sub(".", name.strip().lower()).strip(".")


class SkillPackAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    operation: str
    adapter: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)


class SkillPackEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    skill_id: str | None = None
    name: str
    summary: str
    semver: str = "1.0.0"
    tags: list[str] = Field(default_factory=list)
    application: str = "web"
    adapter: str = "browser"
    parameters: list[SkillParameter] = Field(default_factory=list)
    actions: list[SkillPackAction]

    def resolved_id(self) -> str:
        return self.skill_id or _slug(self.name)

    def to_skill(self) -> ReusableSkill:
        skill_id = self.resolved_id()
        return ReusableSkill(
            id=skill_id,
            name=self.name,
            description=self.summary,
            signature=SkillSignature(
                application=self.application, adapter=self.adapter
            ),
            parameters=list(self.parameters),
            actions=[
                ActionEvent(
                    correlation_id=skill_id,
                    adapter=action.adapter or self.adapter,
                    operation=action.operation,
                    parameters=action.parameters,
                )
                for action in self.actions
            ],
        )


class SkillPack(BaseModel):
    model_config = ConfigDict(frozen=True)

    skills: list[SkillPackEntry]


def parse_skill_pack(data: dict[str, Any]) -> SkillPack:
    return SkillPack.model_validate(data)


def load_skill_pack(
    registry: SkillRegistry, source: str | Path | dict[str, Any]
) -> list[RegisteredSkill]:
    if isinstance(source, dict):
        data = source
    else:
        data = yaml.safe_load(Path(source).expanduser().read_text()) or {}
    pack = parse_skill_pack(data)
    registered: list[RegisteredSkill] = []
    for entry in pack.skills:
        registered.append(
            registry.register(
                entry.to_skill(),
                semver=entry.semver,
                summary=entry.summary,
                tags=entry.tags,
            )
        )
    return registered


def starter_pack_text() -> str:
    return (
        resources.files("workflow_gps.skills") / "packs" / _STARTER_PACK
    ).read_text()


def load_starter_pack(registry: SkillRegistry) -> list[RegisteredSkill]:
    return load_skill_pack(registry, yaml.safe_load(starter_pack_text()) or {})
