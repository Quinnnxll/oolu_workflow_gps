from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from ..knowledge.scrubbing import scrub
from .compiler import DemonstrationCompiler
from .models import Demonstration, ExecutionStatus, ReusableSkill, SkillSignature
from .ports import ActionExecutor
from .registry import RegisteredSkill, SkillRegistry

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str) -> str:
    return _SLUG_RE.sub(".", text.strip().lower()).strip(".")


def _scrub_value(value: Any) -> Any:
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, list):
        return [_scrub_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _scrub_value(item) for key, item in value.items()}
    return value


def scrub_demonstration(demo: Demonstration) -> Demonstration:
    """Mask secrets/PII everywhere the model or store could see them, before any
    learning happens — the enforceable half of "learn the shape, not the values"."""
    return demo.model_copy(
        update={
            "intent": scrub(demo.intent),
            "actions": [
                action.model_copy(
                    update={"parameters": _scrub_value(action.parameters)}
                )
                for action in demo.actions
            ],
            "evidence": _scrub_value(demo.evidence),
        }
    )


class VerificationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    verified: bool | None = None
    outcomes: list[dict] = Field(default_factory=list)
    reason: str | None = None


class LearnedSkill(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str  # registered | unverified | compile_failed
    skill: ReusableSkill | None = None
    registered: RegisteredSkill | None = None
    verification: VerificationResult = Field(default_factory=VerificationResult)
    reason: str | None = None


class SkillLearner:
    """The learning spine: a consented demonstration becomes a verified, private
    skill — scrub -> compile -> sandbox-verify -> register. Registration is gated on
    verification (run the compiled actions through an injected sandbox executor and
    require success), so an unverified skill never enters the registry silently.
    """

    def __init__(
        self,
        registry: SkillRegistry,
        *,
        compiler: DemonstrationCompiler | None = None,
        executors: dict[str, ActionExecutor] | None = None,
        scrub_pii: bool = True,
        default_application: str = "learned",
    ):
        self._registry = registry
        self._compiler = compiler or DemonstrationCompiler()
        self._executors = dict(executors or {})
        self._scrub = scrub_pii
        self._application = default_application

    def learn(
        self,
        demonstration: Demonstration,
        *,
        name: str,
        description: str,
        semver: str = "0.1.0",
        tags: list[str] | None = None,
        adapter: str = "cli",
        skill_id: str | None = None,
        verify: bool = True,
        register_unverified: bool = False,
    ) -> LearnedSkill:
        demo = scrub_demonstration(demonstration) if self._scrub else demonstration
        signature = SkillSignature(
            application=demo.application or self._application, adapter=adapter
        )
        try:
            skill = self._compiler.compile_exact(
                demo, name=name, description=description, signature=signature
            )
        except ValueError as exc:
            return LearnedSkill(status="compile_failed", reason=str(exc))

        # Stable id so re-learning the same task versions it rather than duplicating.
        skill = skill.model_copy(update={"id": skill_id or f"learned.{_slug(name)}"})

        verification = (
            self._verify(skill)
            if verify
            else VerificationResult(verified=None, reason="verification skipped")
        )
        if not (verification.verified or not verify or register_unverified):
            return LearnedSkill(
                status="unverified",
                skill=skill,
                verification=verification,
                reason=verification.reason,
            )
        registered = self._registry.register(
            skill,
            semver=semver,
            summary=description,
            tags=list(tags or ["learned"]),
        )
        return LearnedSkill(
            status="registered",
            skill=skill,
            registered=registered,
            verification=verification,
        )

    def _verify(self, skill: ReusableSkill) -> VerificationResult:
        if not self._executors:
            return VerificationResult(
                verified=None, reason="no sandbox executor configured"
            )
        outcomes: list[dict] = []
        run_key = uuid4().hex
        for index, action in enumerate(skill.actions):
            executor = self._executors.get(action.adapter)
            if executor is None or action.operation not in executor.capabilities():
                return VerificationResult(
                    verified=False,
                    outcomes=outcomes,
                    reason=f"missing capability {action.adapter}/{action.operation}",
                )
            outcome = executor.execute(
                action, idempotency_key=f"learn:{skill.id}:{index}:{run_key}"
            )
            outcomes.append(outcome.model_dump(mode="json"))
            if outcome.status is not ExecutionStatus.SUCCEEDED:
                return VerificationResult(
                    verified=False,
                    outcomes=outcomes,
                    reason=outcome.error or "action failed in sandbox",
                )
        return VerificationResult(verified=True, outcomes=outcomes)
