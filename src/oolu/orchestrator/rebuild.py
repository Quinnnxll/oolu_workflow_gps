"""The LLM rebuild — called out when the user's retries are exhausted.

When a route keeps failing at the same wall — the user hit Retry twice and
the run still cannot get past the failing node — repeating the identical
plan a third time is not a strategy. This module is the escalation the
engine reaches for instead: ask a model to PLAN the steps fresh and WRITE
the code that executes them, then run that code through the script runtime
(which verifies by execution before trusting anything).

Consent first, always (the project rule: called and asked, never assumed):
the rebuild only fires when the tenant's ``account.autobuild_consent``
setting — "Auto-build nodes on my paths" — is on. With consent off the
rebuild refuses with the exact sentence the user needs to hear, and the
incident surfaces it instead of silently giving up.

The produced route is honest about its provenance: ``origin="llm_rebuild"``,
the model's numbered plan preserved in ``plan_notes``, and ``risk="write"``
so the human-control phase demands a confirmation before model-written code
runs — the user sees the new plan and approves it, every time.
"""

from __future__ import annotations

import logging
import re
from typing import Callable, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from ..routing.gateway import extract_script
from ..skills.models import ActionEvent
from .state import Blueprint, ExecutionRecord, ReservedAction, RoutePlan

logger = logging.getLogger(__name__)

# What the meter books a rebuild consultation under.
REBUILD_PURPOSE = "plan.rebuild"

# The settings switch the refusal points at — one string, used verbatim by
# the engine's incident payload and the gateway's chat/task hints so the
# user always reads the same instruction.
AUTOBUILD_SETTING_LABEL = "Auto-build nodes on my paths"
AUTOBUILD_CONSENT_KEY = "account.autobuild_consent"
AUTOBUILD_HINT = (
    f"Turn on '{AUTOBUILD_SETTING_LABEL}' in Settings to let OoLu plan and "
    "write the code itself when retries keep failing, then hit Retry again."
)

REBUILD_SYSTEM_PROMPT = """\
You are the route rebuilder of OoLu, a system that accomplishes user tasks
by planning routes through executable nodes. A planned route has failed
repeatedly and the user has exhausted their retries; your job is to plan the
task fresh and write the code that executes it.

Answer in EXACTLY this shape:
1. First a short numbered plan (one step per line, e.g. "1. ...") describing
   how you will accomplish the task.
2. Then ONE complete, self-contained Python script in a single fenced
   ```python code block that performs the whole plan in one run.

Script rules:
- Single result channel. The script MUST import and call emit_result exactly
  once with its final answer:
      from _oolu_runtime import emit_result
      emit_result(answer)   # answer may be any JSON-serializable value
  If the task genuinely cannot be completed, call emit_error(message) from
  the same module instead of guessing.
- The script runs in an isolated sandbox with the Python standard library
  available; missing third-party packages are installed automatically, so
  write the natural import.
- No runtime network or secrets: during execution the sandbox has NO network
  access and NO host credentials. Do everything with what the script itself
  computes.
- Do NOT repeat the failed approach verbatim — the failure context below
  tells you which node broke and why; route around it."""

_STEP_RE = re.compile(r"^\s*(\d+)[.)]\s+(.*\S)\s*$")


def parse_plan_steps(text: str | None, *, max_steps: int = 20) -> list[str]:
    """The model's numbered plan lines, in order, from before the code fence."""
    if not text:
        return []
    prose = text.split("```", 1)[0]
    steps: list[str] = []
    for line in prose.splitlines():
        match = _STEP_RE.match(line)
        if match:
            steps.append(match.group(2))
        if len(steps) >= max_steps:
            break
    return steps


class RebuildDecision(BaseModel):
    """What the rebuilder decided: a fresh route, or the reason there is none."""

    model_config = ConfigDict(frozen=True)

    route: RoutePlan | None = None
    reason: str = ""
    plan_notes: list[str] = Field(default_factory=list)

    @property
    def rebuilt(self) -> bool:
        return self.route is not None


@runtime_checkable
class RouteRebuilder(Protocol):
    """Plan the task fresh (typically via a model) after retries ran out."""

    def rebuild(
        self,
        *,
        intent: str,
        tenant_id: str,
        route: RoutePlan | None,
        execution: ExecutionRecord | None,
    ) -> RebuildDecision: ...


def _failure_context(
    route: RoutePlan | None, execution: ExecutionRecord | None
) -> str:
    lines: list[str] = []
    if route is not None:
        lines.append(f"Failed route: '{route.chosen.name}' with steps:")
        for item in route.chosen.actions:
            lines.append(f"- {item.action.adapter}/{item.action.operation}")
    if execution is not None:
        if execution.failed_action_label:
            lines.append(f"Failing node: {execution.failed_action_label}")
        if execution.error:
            lines.append(f"Failure: {execution.error}")
        lines.append(f"Attempts made: {execution.attempt}")
    return "\n".join(lines) if lines else "No route could be planned at all."


class LLMRouteRebuilder:
    """Ask the tenant's model to plan the steps and write the execution code.

    ``model`` is any chat model (``reply(messages) -> str``) — in production
    the tenant's ``ChatModelRouter`` metered under ``plan.rebuild``.
    ``consent`` answers whether a tenant allowed auto-building; without
    consent the rebuild refuses with the settings hint rather than acting.
    Every failure mode returns a refusal ``RebuildDecision`` — a rebuild
    that cannot happen must never kill the run it was trying to save.
    """

    def __init__(
        self,
        model,  # chat.ChatModel: reply(messages) -> str
        *,
        consent: Callable[[str], bool] | None = None,
    ):
        self._model = model
        self._consent = consent

    def rebuild(
        self,
        *,
        intent: str,
        tenant_id: str,
        route: RoutePlan | None,
        execution: ExecutionRecord | None,
    ) -> RebuildDecision:
        if self._consent is not None and not self._consent(tenant_id):
            return RebuildDecision(reason=f"auto-build is off — {AUTOBUILD_HINT}")
        if self._model is None:
            return RebuildDecision(
                reason="no model is configured to plan a rebuild — add a model "
                "key (or a local model) in Settings"
            )
        prompt = (
            f"Task: {intent}\n\n{_failure_context(route, execution)}\n\n"
            "Plan the steps and write the script."
        )
        try:
            raw = self._model.reply(
                [
                    {"role": "system", "content": REBUILD_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ]
            )
        except Exception as exc:  # noqa: BLE001 - a dead model is a refusal,
            # never a crash in the middle of recovery.
            logger.warning("rebuild model consultation failed: %s", exc)
            return RebuildDecision(reason=f"the model could not be reached: {exc}")

        script = extract_script(raw)
        if not script:
            return RebuildDecision(
                reason="the model answered but produced no runnable code"
            )
        notes = parse_plan_steps(raw)
        action = ActionEvent(
            correlation_id="llm-rebuild",
            adapter="script",
            operation="run",
            parameters={
                "goal": intent,
                "script": script,
                "node_key": f"rebuild:{intent}",
                "skill_id": "llm-rebuild",
            },
        )
        blueprint = Blueprint(
            name="AI rebuild",
            actions=[
                ReservedAction(
                    action=action,
                    required_capabilities=frozenset({"run"}),
                    reserved=False,
                    # Model-written code always re-earns the human's
                    # confirmation before it runs.
                    risk="write",
                )
            ],
            estimated_cost=0.0,
            origin="llm_rebuild",
            plan_notes=notes,
        )
        return RebuildDecision(
            route=RoutePlan(chosen=blueprint, alternatives=[], total_cost=0.0),
            reason="the model planned the steps and wrote the execution code",
            plan_notes=notes,
        )
