"""Two-tier routing matrix — which model, which sampling params, when to escalate.

Pure, state-driven decision logic (no network). Given a ``GraphState`` it returns a
``RoutingDecision``: the tier to use, the LiteLLM model string + endpoint, and the
sampling parameters. The gateway executes that decision; this module only decides.

THE ESCALATION LADDER
---------------------
The Fast Tier (Qwen3.6-35B-A3B MoE) is fast and cheap but is known to drift on long
multi-step loops and tight global rules. So we keep its loops short and promote to
the Reasoning Tier (Llama 3.3 70B) deliberately, via a staged response rather than a
single switch:

    failure 1 (same signature)         -> Fast, base temperature
    failure 2 (rut_bump_threshold)     -> Fast, temperature BUMPED (shake it loose cheaply)
    failure >=3 (rut_escalate_threshold) -> escalate to Reasoning (the bump didn't work)
    recalc_count >= max_fast_recalcs   -> escalate to Reasoning (genuinely multi-step now)

Escalation is one-way within a session: once on Reasoning we stay there. The
temperature bump (trap #5's other half) still applies on whichever tier is active,
capped per tier.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..models import GraphState, ModelTier

# Assumed default endpoint per the locked decision: a single OpenAI-compatible vLLM
# server on localhost:8000. In a real deployment the two tiers usually live on
# different endpoints; config/models.yaml overrides these.
_DEFAULT_API_BASE = "http://localhost:8000/v1"


class TierConfig(BaseModel):
    """Per-tier model + sampling configuration. Deserialized from config/models.yaml."""

    model_config = ConfigDict(frozen=True)

    tier: ModelTier
    model: str = Field(
        ..., description="LiteLLM model string, e.g. 'openai/Qwen/Qwen3.6-35B-A3B'."
    )
    api_base: str | None = _DEFAULT_API_BASE
    base_temperature: float = Field(default=0.1, ge=0.0)
    max_temperature: float = Field(default=0.7, ge=0.0)
    top_p: float = 0.8
    top_k: int | None = None
    max_tokens: int = 4096
    extra_params: dict = Field(
        default_factory=dict, description="Passed through as extra_body."
    )


class RoutingConfig(BaseModel):
    """Tier definitions plus the escalation/temperature thresholds."""

    model_config = ConfigDict(frozen=True)

    fast: TierConfig
    reasoning: TierConfig

    max_fast_recalcs: int = Field(
        default=3, description="Loop depth that forces escalation."
    )
    rut_bump_threshold: int = Field(
        default=2, description="Identical failures that bump temperature."
    )
    rut_escalate_threshold: int = Field(
        default=3, description="Identical failures that force escalation."
    )
    rut_temperature_step: float = Field(
        default=0.15, description="Temperature added per repeat past the first."
    )

    def tier_config(self, tier: ModelTier) -> TierConfig:
        return self.fast if tier is ModelTier.FAST else self.reasoning


def default_routing_config() -> RoutingConfig:
    """The locked v0 defaults: Qwen3.6-35B-A3B fast, Llama 3.3 70B reasoning."""
    return RoutingConfig(
        fast=TierConfig(
            tier=ModelTier.FAST,
            model="openai/Qwen/Qwen3.6-35B-A3B",
            base_temperature=0.1,
            max_temperature=0.7,
            top_p=0.8,
            top_k=20,  # Qwen model card recommendation
            max_tokens=4096,
        ),
        reasoning=TierConfig(
            tier=ModelTier.REASONING,
            model="openai/meta-llama/Llama-3.3-70B-Instruct",
            base_temperature=0.3,  # a touch more exploratory for re-planning
            max_temperature=0.7,
            top_p=0.9,
            top_k=None,
            max_tokens=4096,
        ),
    )


class RoutingDecision(BaseModel):
    """The chosen tier, model, and sampling parameters for one synthesis call."""

    model_config = ConfigDict(frozen=True)

    tier: ModelTier
    model: str
    api_base: str | None
    temperature: float
    top_p: float
    top_k: int | None
    max_tokens: int
    extra_params: dict = Field(default_factory=dict)
    escalated: bool = Field(
        default=False, description="True only on the step we cross Fast -> Reasoning."
    )
    reason: str = ""

    def to_completion_kwargs(self) -> dict:
        """Shape this decision into kwargs for ``litellm.completion(messages=..., **kwargs)``.

        Non-OpenAI-standard knobs (top_k, anything in extra_params) go under
        ``extra_body``, which LiteLLM forwards to the (vLLM) server. NOTE: ``extra_body``
        is forwarded verbatim and is NOT subject to ``litellm.drop_params``, so the real
        OpenAI API will reject ``top_k`` — when targeting api.openai.com, leave ``top_k``
        unset (see config/openai.yaml).
        """
        kwargs: dict = {
            "model": self.model,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
        }
        if self.api_base:
            kwargs["api_base"] = self.api_base
        extra_body = dict(self.extra_params)
        if self.top_k is not None:
            extra_body["top_k"] = self.top_k
        if extra_body:
            kwargs["extra_body"] = extra_body
        return kwargs


class RoutingMatrix:
    """Turns graph state into a routing decision via the escalation ladder."""

    def __init__(self, config: RoutingConfig | None = None):
        self._config = config or default_routing_config()

    @property
    def config(self) -> RoutingConfig:
        return self._config

    def decide(self, state: GraphState) -> RoutingDecision:
        cfg = self._config
        repeats = state.repeated_failure_count()
        target, escalated, reason = self._select_tier(state, repeats)
        tier_cfg = cfg.tier_config(target)
        temperature = self._temperature(tier_cfg, repeats)

        if escalated:
            reason = f"escalated Fast -> Reasoning: {reason}"
        elif repeats >= cfg.rut_bump_threshold:
            reason = f"{reason}; temperature bumped to {temperature:.2f} after {repeats} identical failures"

        return RoutingDecision(
            tier=target,
            model=tier_cfg.model,
            api_base=tier_cfg.api_base,
            temperature=temperature,
            top_p=tier_cfg.top_p,
            top_k=tier_cfg.top_k,
            max_tokens=tier_cfg.max_tokens,
            extra_params=dict(tier_cfg.extra_params),
            escalated=escalated,
            reason=reason,
        )

    # --- decision components ------------------------------------------ #
    def _select_tier(
        self, state: GraphState, repeats: int
    ) -> tuple[ModelTier, bool, str]:
        cfg = self._config

        # Already escalated: stay on Reasoning, never demote within a session.
        if state.current_tier is ModelTier.REASONING:
            return ModelTier.REASONING, False, "continuing on reasoning tier"

        # Loop depth: a genuinely multi-step task — exactly where the Fast MoE drifts.
        if state.recalc_count >= cfg.max_fast_recalcs:
            return (
                ModelTier.REASONING,
                True,
                f"{state.recalc_count} recalc cycles >= depth limit",
            )

        # Persistent rut: the temperature bump didn't shake it loose.
        if repeats >= cfg.rut_escalate_threshold:
            return ModelTier.REASONING, True, f"identical failure x{repeats}"

        return ModelTier.FAST, False, "fast tier"

    def _temperature(self, tier_cfg: TierConfig, repeats: int) -> float:
        cfg = self._config
        if repeats < cfg.rut_bump_threshold:
            return tier_cfg.base_temperature
        steps = repeats - (cfg.rut_bump_threshold - 1)  # first bump at the threshold
        bumped = tier_cfg.base_temperature + steps * cfg.rut_temperature_step
        return min(bumped, tier_cfg.max_temperature)
