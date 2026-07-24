"""Model gateway — the I/O boundary between the engine and the LLM backend.

Takes a ``RoutingDecision`` (which model + sampling) and an ``AssembledPrompt``
(cache-safe messages), calls the model through LiteLLM, and extracts the synthesized
Python script from the completion.

Two things make this more than a thin wrapper:

  1. CODE-BLOCK EXTRACTION. The model answers in prose-plus-fenced-code, not tool-call
     JSON. Parsing a ```python block is the entire reason small local models are
     viable here — it sidesteps their weakest capability (reliable structured tool
     calls). ``extract_script`` is pure and exhaustively tested.

  2. THE ERROR DISTINCTION. Failing to *get* a completion (endpoint down, timeout,
     provider error) is a ``GatewayError`` — infrastructure, surface it, never recalc.
     Getting a completion that simply has no usable code in it is NOT an error; it is
     a ``SynthesisResult`` with ``script=None``, which the graph can legitimately
     recalc (re-prompt, bump temperature) before giving up. Conflating the two would
     either loop on a dead endpoint or abort on a fixable empty answer.

LiteLLM is imported lazily, so this module imports fine without it. ``FakeGateway``
(scripted completions) lets the whole graph be tested with no live vLLM endpoint —
the same pattern as ``runtime.backend.StubBackend``.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Callable, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from ..models import ModelTier
from .matrix import RoutingDecision
from .prompting import AssembledPrompt

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Errors — transport failures only. (No-code-in-answer is NOT an error.)       #
# --------------------------------------------------------------------------- #
class GatewayError(Exception):
    """Could not obtain a completion. Surface it; do not feed it to recalc."""


class GatewayUnavailable(GatewayError):
    """The gateway cannot operate at all: LiteLLM missing, endpoint unreachable."""


# --------------------------------------------------------------------------- #
# Code extraction — pure and well-tested.                                      #
# --------------------------------------------------------------------------- #
_FENCE_RE = re.compile(r"```[ \t]*([A-Za-z0-9_+\-]*)[ \t]*\r?\n(.*?)```", re.DOTALL)
_TRUNCATED_FENCE_RE = re.compile(
    r"```[ \t]*(?:python|py|python3)?[ \t]*\r?\n(.*)$", re.DOTALL
)
_PYTHON_SMELL_RE = re.compile(r"(?:^|\n)\s*(?:import |from |def |class |emit_result\()")
_PYTHON_LANGS = {"python", "py", "python3"}


def extract_script(text: str | None) -> str | None:
    """Pull the synthesized Python out of a completion. Returns None if there is none.

    Strategy, in order:
      1. Complete fenced blocks: prefer the LAST non-empty ```python block (models
         sometimes show a wrong-then-corrected version; the last is the final word);
         fall back to the last non-empty fenced block of any language.
      2. A truncated opening fence with no close (a ``finish_reason=length`` cutoff):
         take everything after the opening fence.
      3. No fence at all: accept the raw text only if it smells like Python.
    """
    if not text:
        return None

    blocks = _FENCE_RE.findall(text)
    if blocks:
        python_blocks = [
            body.strip()
            for lang, body in blocks
            if lang.lower() in _PYTHON_LANGS and body.strip()
        ]
        if python_blocks:
            return python_blocks[-1]
        any_blocks = [body.strip() for _, body in blocks if body.strip()]
        if any_blocks:
            return any_blocks[-1]
        return None

    truncated = _TRUNCATED_FENCE_RE.search(text)
    if truncated:
        body = truncated.group(1).strip()
        return body or None

    if _PYTHON_SMELL_RE.search(text):
        return text.strip()
    return None


# --------------------------------------------------------------------------- #
# Result.                                                                      #
# --------------------------------------------------------------------------- #
class SynthesisResult(BaseModel):
    """One model turn: the raw answer, the extracted script, and usage telemetry."""

    model_config = ConfigDict(frozen=True)

    raw_text: str
    script: str | None
    model: str
    tier: ModelTier
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    finish_reason: str | None = None
    duration_s: float = 0.0

    @property
    def has_script(self) -> bool:
        return bool(self.script and self.script.strip())


# --------------------------------------------------------------------------- #
# Protocol + implementations.                                                  #
# --------------------------------------------------------------------------- #
@runtime_checkable
class Gateway(Protocol):
    @property
    def name(self) -> str: ...
    def complete(
        self, decision: RoutingDecision, prompt: AssembledPrompt
    ) -> SynthesisResult: ...
    def close(self) -> None: ...


# Exception class names that mean "the same request may well succeed in a
# moment" — rate limits, timeouts, connection blips, provider 5xx. Matched
# by name so no LiteLLM exception taxonomy is imported or version-pinned.
_TRANSIENT_ERROR_MARKS = (
    "ratelimit",
    "timeout",
    "connection",
    "internalserver",
    "serviceunavailable",
    "overloaded",
)


def _transient(exc: Exception) -> bool:
    return any(mark in type(exc).__name__.lower() for mark in _TRANSIENT_ERROR_MARKS)


def _default_backoff(seconds: float) -> None:
    """Real wait between retries; a module seam tests neutralize."""
    time.sleep(seconds)


class LiteLLMGateway:
    """Real gateway over LiteLLM -> vLLM (OpenAI-compatible).

    Transient transport failures (rate limit, timeout, connection, 5xx)
    are retried with backoff before becoming a ``GatewayError`` — the
    chat stack has always retried those; the synthesis stack now keeps
    the same promise. ``completion_fn`` is the injectable seam: tests
    exercise the retry ladder without litellm installed."""

    def __init__(
        self,
        *,
        request_timeout: float = 120.0,
        drop_unsupported_params: bool = True,
        max_attempts: int = 3,
        sleep: Callable[[float], None] | None = None,
        completion_fn: Callable[..., object] | None = None,
    ):
        if completion_fn is None:
            try:
                import litellm
            except ImportError as exc:
                raise GatewayUnavailable(
                    "litellm not installed (`pip install 'oolu[engine]'`)"
                ) from exc
            # vLLM rejects unknown params; dropping them keeps tier configs
            # portable.
            if drop_unsupported_params:
                litellm.drop_params = True
            completion_fn = litellm.completion
        self._completion = completion_fn
        self._timeout = request_timeout
        self._max_attempts = max(1, int(max_attempts))
        self._sleep = sleep

    @property
    def name(self) -> str:
        return "litellm"

    def complete(
        self, decision: RoutingDecision, prompt: AssembledPrompt
    ) -> SynthesisResult:
        kwargs = decision.to_completion_kwargs()
        kwargs.setdefault("timeout", self._timeout)
        start = time.monotonic()
        response = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._completion(messages=prompt.messages, **kwargs)
                break
            except Exception as exc:  # noqa: BLE001 - any failure to GET a completion is a GatewayError
                if _transient(exc) and attempt < self._max_attempts:
                    (self._sleep or _default_backoff)(float(attempt))
                    continue
                raise GatewayError(
                    f"completion failed via {kwargs.get('model')}: {exc}"
                ) from exc
        duration = time.monotonic() - start

        choice = response.choices[0]
        text = getattr(choice.message, "content", None) or ""
        usage = getattr(response, "usage", None)
        return SynthesisResult(
            raw_text=text,
            script=extract_script(text),
            model=kwargs["model"],
            tier=decision.tier,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            total_tokens=getattr(usage, "total_tokens", 0) or 0,
            finish_reason=getattr(choice, "finish_reason", None),
            duration_s=duration,
        )

    def close(self) -> None:
        return None


# A scripted completion: a ready string, or a callable deriving one from the call.
CompletionFactory = str | Exception | Callable[[RoutingDecision, AssembledPrompt], str]


class FakeGateway:
    """Returns scripted completions — no network. Mirrors StubBackend.

    Each entry is a completion string (extraction runs on it normally), or an
    Exception instance to simulate a transport failure on that call. Every call is
    recorded so tests can assert what the engine asked for on each cycle.
    """

    def __init__(self, completions: list[CompletionFactory], *, name: str = "fake"):
        self._completions: list[CompletionFactory] = list(completions)
        self._name = name
        self.calls: list[tuple[RoutingDecision, AssembledPrompt]] = []

    @property
    def name(self) -> str:
        return self._name

    def complete(
        self, decision: RoutingDecision, prompt: AssembledPrompt
    ) -> SynthesisResult:
        self.calls.append((decision, prompt))
        if not self._completions:
            raise GatewayError(
                f"fake gateway '{self._name}' has no scripted completions left"
            )
        nxt = self._completions.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        text = nxt(decision, prompt) if callable(nxt) else nxt
        return SynthesisResult(
            raw_text=text,
            script=extract_script(text),
            model=decision.model,
            tier=decision.tier,
            finish_reason="stop",
        )

    def close(self) -> None:
        return None
