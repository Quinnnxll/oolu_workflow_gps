"""Value patchers — the smart plugin that fills a contract's creative holes.

Where the ``ProposalModel`` advises *which* nodes to pick, a
``ValuePatcher`` fills *what values* the picked nodes run with: the
mechanical-design moment where the scaffolding (open app, open file,
select tool) is done and dimensions, positions, and text must come from
somewhere. Three fillers, one precedence:

    user-provided  >  patcher-filled  >  declared default

``GatewayValuePatcher`` is the LLM filler, and it is deliberately shaped
like every other model seam in this codebase:

- **batched**: ONE completion fills the entire manifest — the whole list
  of declared inputs with descriptions, defaults, and bounds rides one
  prompt (the "patch by patch" efficiency: a patch covers a node's
  inputs, one call covers all patches);
- **node-adapted**: the node steers the model through its own declared
  descriptions, defaults, and bounds — plus the goal text — never the
  other way around;
- **boxed**: whatever comes back is validated against each declaration
  (numbers clamp, hallucinated choices fall back to defaults, unknown
  names are dropped), so the model cannot leave the box the node drew;
- **metered**: the call is priced through ``billing.ModelCallMeter`` and
  the charge rides the returned patch into budgets;
- **advisory**: unreadable output means "no patch" (defaults apply), and
  ``patch_or_defaults`` turns even a dead endpoint into defaults — a
  creative model can improve a run, it can never block one.
"""

from __future__ import annotations

import json
from typing import Any, Protocol, Sequence, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from ..billing.model_calls import ModelCallMeter
from ..models import ModelTier
from ..routing.gateway import Gateway
from ..routing.matrix import RoutingConfig, RoutingDecision, default_routing_config
from ..routing.prompting import AssembledPrompt
from ..skills.inputs import BoundInput, validate_value
from .proposals import _json_candidates

PATCH_PURPOSE = "values.patch"

# Frozen — the cacheable prefix of every patch prompt.
PATCH_SYSTEM_PROMPT = """\
You fill in creative values for the workflow engine of OoLu. You \
are given a goal and a list of declared inputs — each with a description, \
a type, a default, and hard bounds or a closed choice set. Choose values \
that best serve the goal.

Rules:
- Stay inside each input's bounds or choice set; out-of-range values are \
clamped, unknown choices revert to the default.
- If you have no better idea than the default, repeat the default.
- Values only. You cannot add parameters, code, or instructions.

Output format:
- Reply with EXACTLY ONE fenced ```json block containing a single JSON \
object that maps input name (copied exactly) to its value. No prose \
outside the block."""


class ValuePatch(BaseModel):
    """Filled values (validated by the caller) and what filling them cost."""

    model_config = ConfigDict(frozen=True)

    values: dict[str, Any] = Field(default_factory=dict)
    cost: float = 0.0


@runtime_checkable
class ValuePatcher(Protocol):
    """Fills declared inputs. Advisory by contract: implementations may
    raise or return garbage and the run proceeds on defaults."""

    def patch(self, *, goal: str, manifest: Sequence[BoundInput]) -> ValuePatch: ...


class DefaultValuePatcher:
    """No opinions: every input takes its declared default. Free."""

    def patch(self, *, goal: str, manifest: Sequence[BoundInput]) -> ValuePatch:
        return ValuePatch()


class GatewayValuePatcher:
    """Fills the whole manifest with one model call over the routing Gateway."""

    def __init__(
        self,
        gateway: Gateway,
        *,
        config: RoutingConfig | None = None,
        tier: ModelTier = ModelTier.FAST,
        meter: ModelCallMeter | None = None,
        max_tokens: int = 768,
    ):
        self._gateway = gateway
        self._config = config or default_routing_config()
        self._tier = tier
        self._meter = meter or ModelCallMeter()
        self._max_tokens = max_tokens

    @property
    def meter(self) -> ModelCallMeter:
        return self._meter

    def patch(self, *, goal: str, manifest: Sequence[BoundInput]) -> ValuePatch:
        if not manifest:
            return ValuePatch()
        result = self._gateway.complete(self._decision(), self._prompt(goal, manifest))
        charge = self._meter.record(PATCH_PURPOSE, result)
        return ValuePatch(
            values=parse_patch(result.raw_text, manifest), cost=charge.cost
        )

    # ------------------------------------------------------------------ #
    def _decision(self) -> RoutingDecision:
        tier_cfg = self._config.tier_config(self._tier)
        return RoutingDecision(
            tier=tier_cfg.tier,
            model=tier_cfg.model,
            api_base=tier_cfg.api_base,
            temperature=tier_cfg.base_temperature,
            top_p=tier_cfg.top_p,
            top_k=tier_cfg.top_k,
            max_tokens=min(self._max_tokens, tier_cfg.max_tokens),
            extra_params=dict(tier_cfg.extra_params),
            reason="value patch",
        )

    def _prompt(self, goal: str, manifest: Sequence[BoundInput]) -> AssembledPrompt:
        lines = [f"Goal: {goal}", "Inputs to fill:"]
        for entry in manifest:
            spec = entry.spec
            lines.append(f"- name: {entry.qualified}")
            if spec.description:
                lines.append(f"  what: {spec.description}")
            lines.append(f"  type: {spec.value_type}")
            if spec.default is not None:
                lines.append(f"  default: {spec.default}")
            if spec.minimum is not None or spec.maximum is not None:
                lines.append(f"  bounds: [{spec.minimum}, {spec.maximum}]")
            if spec.choices:
                lines.append(f"  choices: {', '.join(spec.choices)}")
        return AssembledPrompt(
            messages=[
                {"role": "system", "content": PATCH_SYSTEM_PROMPT},
                {"role": "user", "content": "\n".join(lines)},
            ],
            prefix_len=1,
        )


def parse_patch(text: str | None, manifest: Sequence[BoundInput]) -> dict[str, Any]:
    """Extract and BOX the model's values: unknown names dropped, each
    survivor validated against its declaration (clamped, choice-checked).
    Unusable output — or an unusable individual value — simply vanishes,
    and the default fills that hole downstream."""
    if not text:
        return {}
    by_name = {entry.qualified: entry.spec for entry in manifest}
    for blob in _json_candidates(text):
        try:
            data = json.loads(blob)
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        values: dict[str, Any] = {}
        for key, offered in data.items():
            spec = by_name.get(key)
            if spec is None:
                continue
            try:
                values[key] = validate_value(spec, offered)
            except (TypeError, ValueError):
                continue
        if values:
            return values
    return {}


def patch_or_defaults(
    patcher: ValuePatcher | None, *, goal: str, manifest: Sequence[BoundInput]
) -> ValuePatch:
    """The run-path guard: no patcher, a raising patcher, or a dead model
    endpoint all mean the same thing — the declared defaults run."""
    if patcher is None or not manifest:
        return ValuePatch()
    try:
        return patcher.patch(goal=goal, manifest=manifest)
    except Exception:
        return ValuePatch()
