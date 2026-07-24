"""Model-call metering — planning has a price, and the books get to see it.

Every LLM call the platform makes on a consumer's behalf (ranking candidate
producers, synthesizing scripts) burns real compute, but until now none of
it entered the economics: budgets judged marketplace gross alone, as if
advice were free. This module closes that gap. Callers record each
completion's token telemetry under a *purpose* tag, a ``ModelPriceTable``
turns tokens into money, and the resulting charge rides the same estimated
cost that budgets and previews already judge.

The default prices model amortized local-deployment compute, not a provider
invoice — deliberately small, deliberately nonzero. The point is not the
absolute number; it is that model calls are never free, so a plan that took
many consultations to assemble is honestly dearer than one that took none.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field


class ModelCallRecord(BaseModel):
    """One metered completion: who asked why, what it used, what it cost."""

    model_config = ConfigDict(frozen=True)

    purpose: str  # e.g. "assembly.proposal" — what the call was for
    model: str
    tier: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0
    duration_s: float = 0.0
    at: datetime
    # Effort telemetry (context-harness plan, Phase 0). How the completion
    # ENDED and how much context rode in — so "did node.build truncate?"
    # and "how starved was the call?" are answerable from the books alone.
    # finish_reason keeps the provider's own word ("end_turn", "stop",
    # "max_tokens", "length", "tool_use", ...); empty = the caller didn't
    # know, never a guess.
    finish_reason: str = ""
    context_chars: int = 0  # total characters of the messages sent


class ModelPriceTable(BaseModel):
    """Cost per **million tokens**, keyed by tier value ("fast", ...).

    Unknown tiers fall back to the default rates, which deliberately match
    the dearest configured tier: a call whose price we cannot classify is
    priced conservatively, never quietly free.
    """

    model_config = ConfigDict(frozen=True)

    prompt_per_million: dict[str, float] = Field(default_factory=dict)
    completion_per_million: dict[str, float] = Field(default_factory=dict)
    default_prompt_per_million: float = 0.0
    default_completion_per_million: float = 0.0

    def cost(self, tier: str, prompt_tokens: int, completion_tokens: int) -> float:
        prompt_rate = self.prompt_per_million.get(tier, self.default_prompt_per_million)
        completion_rate = self.completion_per_million.get(
            tier, self.default_completion_per_million
        )
        return (
            max(0, prompt_tokens) * prompt_rate
            + max(0, completion_tokens) * completion_rate
        ) / 1_000_000


DEFAULT_MODEL_PRICES = ModelPriceTable(
    # "local" is a KNOWN free tier — the machine's own model costs the
    # user nothing per token (usage is still recorded as telemetry, and
    # it never eats the spending cap). Unknown tiers still price dearest.
    prompt_per_million={"fast": 0.10, "reasoning": 0.60, "local": 0.0},
    completion_per_million={"fast": 0.30, "reasoning": 1.80, "local": 0.0},
    default_prompt_per_million=0.60,
    default_completion_per_million=1.80,
)


class ModelCallMeter:
    """A thread-safe ledger of model calls. Gateways record; surfaces read.

    ``record`` duck-types on the routing gateway's ``SynthesisResult``
    telemetry fields (model, tier, token counts, duration) so the meter
    never imports the routing stack — anything with those fields meters.
    """

    def __init__(
        self,
        *,
        prices: ModelPriceTable | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self._prices = prices or DEFAULT_MODEL_PRICES
        self._clock = clock or (lambda: datetime.now(UTC))
        self._records: list[ModelCallRecord] = []
        self._lock = threading.Lock()

    @property
    def prices(self) -> ModelPriceTable:
        return self._prices

    def record(self, purpose: str, result) -> ModelCallRecord:
        tier = getattr(result.tier, "value", None) or str(result.tier)
        entry = ModelCallRecord(
            purpose=purpose,
            model=result.model,
            tier=tier,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            cost=self._prices.cost(
                tier, result.prompt_tokens, result.completion_tokens
            ),
            duration_s=result.duration_s,
            at=self._clock(),
            # getattr keeps the duck-typing promise: telemetry objects that
            # predate these fields (the routing gateway's SynthesisResult)
            # meter exactly as before.
            finish_reason=str(getattr(result, "finish_reason", "") or ""),
            context_chars=int(getattr(result, "context_chars", 0) or 0),
        )
        with self._lock:
            self._records.append(entry)
        return entry

    def charges(self, purpose: str | None = None) -> list[ModelCallRecord]:
        with self._lock:
            records = list(self._records)
        if purpose is None:
            return records
        return [record for record in records if record.purpose == purpose]

    def total_cost(self, purpose: str | None = None) -> float:
        return sum(record.cost for record in self.charges(purpose))
