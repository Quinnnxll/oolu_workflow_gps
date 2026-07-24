"""Token accounting — one seam, honest about its precision.

Phase 2 of the context-harness plan. Nothing in the codebase counted
tokens before sending: context size was measured in characters
(``ModelCallRecord.context_chars``) and ceilings were enforced only by
the provider cutting the completion. This module is the counting seam
the Phase 3 budgeter compiles context packs against.

Two grades of answer, clearly labeled by function name:

- :func:`estimate_tokens` / :func:`count_request_tokens` — the DEFAULT:
  a deterministic character heuristic (≈4 chars/token for English-heavy
  prose and code) plus per-message wire overhead. Dependency-free,
  stable across environments, and deliberately a little conservative —
  a budgeter that slightly over-counts trims early, which is the safe
  side of a context window.
- When ``tiktoken`` is installed (the ``tokens`` extra), OpenAI-family
  ids are counted with the real tokenizer instead; every other model
  keeps the heuristic. Anthropic's exact counts live behind their
  count-tokens endpoint — a network call, deliberately NOT made here:
  a counting seam that phones home per estimate would be unusable in
  the compile path.

The numbers are estimates and say so. Callers that need the provider's
own truth read it AFTER the call, from ``usage`` — which the meter
already books per purpose.
"""

from __future__ import annotations

from typing import Any, Iterable

# ≈4 characters per token is the standard English/code rule of thumb;
# dividing by 3.6 instead over-counts ~10% on purpose (see module doc).
_CHARS_PER_TOKEN = 3.6

# Every chat message costs wire framing beyond its text (role tags,
# separators); four tokens is the classic per-message overhead figure.
_PER_MESSAGE_OVERHEAD = 4


def _tiktoken_encoding(model: str):
    """The real tokenizer for OpenAI-family ids when tiktoken is
    installed; None everywhere else — the caller falls back to the
    heuristic, never errors."""
    name = str(model or "").strip().lower()
    if not name.startswith(("gpt-", "o", "chatgpt", "text-")):
        return None
    try:
        import tiktoken
    except ImportError:
        return None
    try:
        return tiktoken.encoding_for_model(name)
    except KeyError:
        try:
            return tiktoken.get_encoding("o200k_base")
        except Exception:  # noqa: BLE001 - counting must never crash a call
            return None


def estimate_tokens(text: str, *, model: str = "") -> int:
    """Tokens in one string: the real tokenizer where one is available
    for the model, the conservative character heuristic otherwise.
    Empty text is 0; anything else is at least 1."""
    raw = str(text or "")
    if not raw:
        return 0
    encoding = _tiktoken_encoding(model)
    if encoding is not None:
        return max(1, len(encoding.encode(raw)))
    return max(1, round(len(raw) / _CHARS_PER_TOKEN))


def count_request_tokens(
    messages: Iterable[dict],
    *,
    tools: Iterable[Any] = (),
    model: str = "",
) -> int:
    """The estimated prompt size of one canonical request: every
    message's content (string or block list) plus per-message overhead,
    plus the serialized tool schemas — the parts of a request that
    actually occupy the window."""
    import json

    total = 0
    for message in messages:
        total += _PER_MESSAGE_OVERHEAD
        content = message.get("content")
        if isinstance(content, str):
            total += estimate_tokens(content, model=model)
        elif content is not None:
            total += estimate_tokens(
                json.dumps(content, ensure_ascii=False, default=str),
                model=model,
            )
        for call in message.get("tool_calls") or ():
            total += estimate_tokens(
                json.dumps(call, ensure_ascii=False, default=str), model=model
            )
    for tool in tools:
        spec = (
            tool.as_openai()
            if hasattr(tool, "as_openai")
            else tool
        )
        total += estimate_tokens(
            json.dumps(spec, ensure_ascii=False, default=str), model=model
        )
    return total
