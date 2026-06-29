"""The navigation state — LangGraph's state schema for the whole engine.

``GraphState`` is the single object that flows through the cyclical graph
(plan -> synthesize -> execute -> classify -> recalculate -> ...). It is a Pydantic
model used directly as LangGraph's ``state_schema``; the one channel that must
*accumulate* rather than *overwrite* — ``error_history`` — carries an explicit
reducer via ``Annotated[..., operator.add]`` so each node can append without
clobbering prior telemetry.

Two design rules are encoded structurally here:

  * **Cache-stable prefix vs volatile suffix (goal 3 / trap #4).** ``intent`` is the
    immutable destination and belongs in the cacheable prompt prefix. Everything
    volatile — ``session_id``, ``iteration``, growing ``error_history`` — is marked
    as suffix material; the prompting layer is responsible for rendering those at
    the very end of the message frame so RadixAttention keeps the prefix warm.

  * **Append-only history.** ``error_history`` only ever grows, in order. This both
    satisfies the reducer contract and prevents mid-frame cache poisoning.
"""

from __future__ import annotations

import operator
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from .errors import ErrorRecord
from .knowledge import DependencyHint
from .results import ExecutionResult


class ModelTier(str, Enum):
    """The two-tier routing matrix."""

    FAST = "fast"  # Qwen3.6-35B-A3B — snappy UI, short *stateless* loops
    REASONING = "reasoning"  # Llama 3.3 70B — re-planning when the route breaks


class GraphStatus(str, Enum):
    """Where the engine is in its navigation cycle."""

    PLANNING = "planning"
    SYNTHESIZING = "synthesizing"
    EXECUTING = "executing"
    RECALCULATING = "recalculating"
    ESCALATED = "escalated"
    COMPLETED = "completed"
    FAILED = "failed"


class ExecutionPlan(BaseModel):
    """A single proposed route: the script to run and what it needs to run."""

    intent: str
    script: str | None = None
    rationale: str | None = None
    required_dependencies: list[str] = Field(
        default_factory=list,
        description="Package names (already resolved from import names).",
    )
    phase_a_needed: bool = Field(
        default=False,
        description="Whether a network-enabled install window is required first.",
    )
    tier: ModelTier = ModelTier.FAST


class GraphState(BaseModel):
    """LangGraph state schema for Workflow-GPS."""

    # arbitrary_types_allowed keeps us flexible if later layers attach richer
    # objects (e.g. a backend handle) without fighting Pydantic validation.
    model_config = ConfigDict(arbitrary_types_allowed=True)

    # --- Immutable destination: belongs in the cache-stable PREFIX ---
    intent: str

    # --- Volatile identifiers: MUST render in the prompt SUFFIX, never the prefix ---
    session_id: str
    iteration: int = 0

    # --- Navigation status ---
    status: GraphStatus = GraphStatus.PLANNING
    current_tier: ModelTier = ModelTier.FAST
    plan: ExecutionPlan | None = None
    last_result: ExecutionResult | None = None

    # --- Append-only telemetry (trap #4). Reducer = list concatenation. ---
    error_history: Annotated[list[ErrorRecord], operator.add] = Field(
        default_factory=list
    )

    # --- Loop control: escalation + rut detection ---
    recalc_count: int = 0
    tier_escalations: int = 0
    synthesis_temperature: float = Field(
        default=0.1,
        description="Bumped on repeated identical failures to escape the rut (trap #5).",
    )

    # --- Knowledge consulted read-only at plan time (from KnowledgeClient) ---
    dependency_hints: list[DependencyHint] = Field(default_factory=list)

    # --- Optional script-cache telemetry (never enters the model prompt) ---
    cache_key: str | None = None
    cache_hit: bool = False
    cache_kind: str | None = None
    cache_status: str | None = None

    # --- Per-run telemetry. Sum-reduced so each node contributes its delta; volatile
    # (never enters the prompt, so the cacheable prefix is unaffected). ---
    gateway_calls: Annotated[int, operator.add] = 0
    prompt_tokens: Annotated[int, operator.add] = 0
    completion_tokens: Annotated[int, operator.add] = 0
    gateway_seconds: Annotated[float, operator.add] = 0.0
    backend_calls: Annotated[int, operator.add] = 0
    backend_seconds: Annotated[float, operator.add] = 0.0

    # --- Terminal outputs ---
    final_answer: dict | None = None
    failure_reason: str | None = None

    # ------------------------------------------------------------------ #
    # Derived helpers. These are plain properties/methods, NOT fields, so #
    # they never enter the serialized state channel or the LLM prompt.    #
    # ------------------------------------------------------------------ #

    @property
    def latest_error(self) -> ErrorRecord | None:
        return self.error_history[-1] if self.error_history else None

    @property
    def is_terminal(self) -> bool:
        return self.status in (GraphStatus.COMPLETED, GraphStatus.FAILED)

    def repeated_failure_count(self) -> int:
        """How many records share the most-recent error's signature.

        The core rut signal (trap #5): if the engine keeps regenerating the same
        broken code under a cached prefix at low temperature, this climbs and we
        change tactics (bump temperature, then escalate tier).
        """
        latest = self.latest_error
        if latest is None:
            return 0
        return sum(
            1 for record in self.error_history if record.signature == latest.signature
        )

    def should_escalate(self, max_fast_recalcs: int = 3) -> bool:
        """Promote FAST -> REASONING on loop depth OR a detected rut.

        Loop depth matters specifically because the Fast Tier (Qwen3.6 MoE) is known
        to drift on long multi-step loops — so once a task has become genuinely
        multi-step (~3 recalcs), it belongs to the Reasoning Tier.
        """
        if self.current_tier is ModelTier.REASONING:
            return False
        return (
            self.recalc_count >= max_fast_recalcs or self.repeated_failure_count() >= 2
        )
