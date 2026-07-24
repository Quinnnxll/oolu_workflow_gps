"""Model manifests — what each model can actually do, declared once.

Phase 2 of the context-harness plan. Until now capability knowledge
lived as scattered predicates (a regex in the adapter, an ``hasattr``
probe at the authoring door) and the real capability discovery endpoint
(``ApiKeyProviderAdapter.capabilities()``) fed nothing. This registry is
the one table both the router and the adapters read:

- **Routing decisions** ask the manifest, not the object shape: the
  authoring door routes a model to the agentic path because its
  manifest says ``tool_calling``, not because the router happens to
  expose a ``consult`` method (every router does — the probe never
  distinguished models at all).
- **Wire construction** asks the manifest which knobs the model speaks:
  extended thinking, the OpenAI reasoning-effort dial, prompt caching.
- **Unknown models are inferred, conservatively.** A cloud id with a
  recognized family gets its family's powers; an unrecognized id (local
  Ollama tags, fine-tunes) is assumed to speak the plain wire and NO
  reliable native tool calling — small local models emitting junk tool
  JSON is exactly the failure the one-shot fenced-code path exists to
  sidestep (``routing/gateway.py`` module docstring).

Operators overlay the table without a code change::

    OOLU_MODEL_MANIFESTS='{"qwen3:32b": {"tool_calling": true}}'

The overlay is read at resolve time, merged field-by-field onto the
declared or inferred base.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, replace

_ENV_OVERLAY = "OOLU_MODEL_MANIFESTS"


@dataclass(frozen=True)
class ModelManifest:
    """One model's declared powers, provider-independent in shape."""

    model_id: str
    provider: str = ""  # anthropic | openai | local | "" (unknown)
    tool_calling: bool = False
    structured_output: bool = False  # schema-forced delivery via tools
    thinking: bool = False  # Anthropic extended thinking
    reasoning_effort: bool = False  # OpenAI o-series / gpt-5 dial
    prompt_caching: bool = False  # honors cache_control breakpoints
    context_window: int = 128_000
    max_output_tokens: int = 4096


# The models the tiers actually name today, declared exactly. Every
# entry doubles as documentation of why a seat behaves as it does.
KNOWN_MODELS: dict[str, ModelManifest] = {
    manifest.model_id: manifest
    for manifest in (
        ModelManifest(
            model_id="claude-haiku-4-5-20251001",
            provider="anthropic",
            tool_calling=True,
            structured_output=True,
            thinking=True,
            prompt_caching=True,
            context_window=200_000,
            max_output_tokens=64_000,
        ),
        ModelManifest(
            model_id="claude-sonnet-5",
            provider="anthropic",
            tool_calling=True,
            structured_output=True,
            thinking=True,
            prompt_caching=True,
            context_window=200_000,
            max_output_tokens=64_000,
        ),
        ModelManifest(
            model_id="gpt-4o-mini",
            provider="openai",
            tool_calling=True,
            structured_output=True,
            context_window=128_000,
            max_output_tokens=16_384,
        ),
        ModelManifest(
            model_id="gpt-4o",
            provider="openai",
            tool_calling=True,
            structured_output=True,
            context_window=128_000,
            max_output_tokens=16_384,
        ),
    )
}


# ---------------------------------------------------------------------- #
# Family inference — the conservative fallback for ids not declared.      #
# ---------------------------------------------------------------------- #
# OpenAI's reasoning models (o-series, gpt-5 family) take
# max_completion_tokens + reasoning_effort and reject a temperature.
_OPENAI_REASONING_RE = re.compile(r"^(o\d|gpt-5)")

# Anthropic extended thinking arrived with Claude 3.7; these elders
# predate it and reject the block.
_ANTHROPIC_NO_THINKING_PREFIXES = (
    "claude-2",
    "claude-instant",
    "claude-3-haiku",
    "claude-3-opus",
    "claude-3-sonnet",
    "claude-3-5",
)


def openai_reasoning_model(model_id: str) -> bool:
    return bool(_OPENAI_REASONING_RE.match(str(model_id or "").strip()))


def anthropic_thinking_model(model_id: str) -> bool:
    name = str(model_id or "").strip().lower()
    if not name.startswith("claude"):
        return False
    return not any(name.startswith(p) for p in _ANTHROPIC_NO_THINKING_PREFIXES)


def _infer(model_id: str, provider: str) -> ModelManifest:
    name = str(model_id or "").strip().lower()
    if name.startswith("claude"):
        return ModelManifest(
            model_id=model_id,
            provider=provider or "anthropic",
            tool_calling=True,
            structured_output=True,
            thinking=anthropic_thinking_model(name),
            prompt_caching=True,
            context_window=200_000,
            max_output_tokens=32_000,
        )
    if openai_reasoning_model(name):
        return ModelManifest(
            model_id=model_id,
            provider=provider or "openai",
            tool_calling=True,
            structured_output=True,
            reasoning_effort=True,
            context_window=200_000,
            max_output_tokens=32_000,
        )
    if name.startswith(("gpt-", "chatgpt")):
        return ModelManifest(
            model_id=model_id,
            provider=provider or "openai",
            tool_calling=True,
            structured_output=True,
            context_window=128_000,
            max_output_tokens=16_384,
        )
    # An unrecognized id — a local tag (qwen3:4b, llama3.2) or a private
    # fine-tune. Plain wire only: no native tool calling assumed, which
    # routes it to the fenced-code path built for exactly these models.
    return ModelManifest(
        model_id=model_id,
        provider=provider or "local",
        context_window=32_000,
        max_output_tokens=8192,
    )


def _overlay(base: ModelManifest) -> ModelManifest:
    raw = os.environ.get(_ENV_OVERLAY, "").strip()
    if not raw:
        return base
    try:
        table = json.loads(raw)
    except ValueError:
        return base  # a broken overlay never breaks routing
    patch = table.get(base.model_id)
    if not isinstance(patch, dict):
        return base
    fields = {
        name: value
        for name, value in patch.items()
        if name in ModelManifest.__dataclass_fields__ and name != "model_id"
    }
    return replace(base, **fields) if fields else base


def manifest_for(model_id: str, *, provider: str = "") -> ModelManifest:
    """The manifest that governs one model id: declared if known,
    inferred from its family otherwise, operator overlay applied last.
    Never raises — capability resolution failing open (to the plain
    wire) is safer than failing loud mid-conversation."""
    base = KNOWN_MODELS.get(str(model_id or "").strip())
    if base is None:
        base = _infer(model_id, provider)
    elif provider and base.provider != provider:
        base = replace(base, provider=provider)
    return _overlay(base)
