"""Cache-safe prompt assembly — the structural enforcement of goal 3.

RadixAttention (and any prefix cache) reuses the longest *token prefix* shared
across requests. The way to exploit that is to make everything expensive and
reusable come first and never change, and confine everything volatile to the tail:

    [0] system  — GLOBAL CONSTANT: identity, code-as-interface contract, output
                  format. Identical for every request the engine ever makes, so it
                  is cached across all sessions and all tasks.
    [1] task    — CONSTANT PER SESSION: the user's intent (+ optional result shape).
                  Identical across every recalc cycle of one navigation, so the
                  system+task prefix stays warm for the whole self-healing loop.
    [2] action  — VOLATILE: attempt number and salient failure feedback. The only
                  part that busts the cache, and it is last, so it busts the minimum.

The real invariant we guarantee — and that ``tests/unit/test_prompt_caching.py``
asserts — is that messages [0] and [1] are BYTE-IDENTICAL across cycles that differ
only in volatile state (iteration, error history, session id). Substring-checking
for stray volatile tokens is fragile (the digit "1" appears everywhere); prefix
invariance under volatile change is the property that actually matters.

This module also carries the message-side mitigation for the identical-broken-code
rut (trap #5): when the same failure repeats, the action message says so loudly and
tells the model to change strategy. The temperature-side mitigation lives in the
routing matrix, which reads the same state signals.
"""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field

from ..models import GraphState

# The frozen prefix prize. No volatile content, no task content — pure, reusable
# instructions, so this entire block is cached across every request, forever.
DEFAULT_SYSTEM_PROMPT = """\
You are the synthesis engine of Workflow-GPS, a system that accomplishes user \
tasks by writing and running code.

Operating principles:
- Code as the interface. For each task you produce ONE complete, self-contained \
Python script that performs the whole task in a single run. Do not propose \
multi-step plans, partial snippets, or tool-call JSON.
- Single result channel. The script MUST import and call emit_result exactly once \
with its final answer:
      from _wfgps_runtime import emit_result
      emit_result(answer)   # answer may be any JSON-serializable value
  If the task genuinely cannot be completed, call emit_error(message) from the same \
module instead of guessing.
- Dependencies are automatic. The script runs in an isolated sandbox with the \
Python standard library available. You may import third-party packages freely: if \
one is missing it is installed automatically and the script is re-run, so write the \
natural import and assume the package is present.
- No runtime network or secrets. During execution the sandbox has NO network access \
and NO access to host credentials or files outside its working directory. Do not \
attempt to reach the network or read secrets at run time.
- Stay minimal and focused. Aim the script directly at the task. You may print \
diagnostics to stdout/stderr freely; only the emit_result payload is read as the \
answer.

Output format:
- Respond with EXACTLY ONE Python code block fenced as ```python ... ``` and nothing \
else — no prose before or after the block."""

# Constant final directive, placed AFTER the volatile feedback for recency. Tiny, so
# it costs nothing that it sits past the cache boundary.
_ACTION_DIRECTIVE = (
    "Produce a single self-contained Python script in one ```python code block. "
    "It must call emit_result(...) exactly once with the final answer. "
    "Output only the code block."
)


class AssembledPrompt(BaseModel):
    """A built message list plus the boundary between cacheable and volatile parts."""

    model_config = ConfigDict(frozen=True)

    messages: list[dict] = Field(..., description="OpenAI-style [{role, content}, ...].")
    prefix_len: int = Field(..., description="Count of leading messages that are volatile-free.")

    @property
    def cacheable_messages(self) -> list[dict]:
        """The prefix that must stay byte-identical across recalc cycles."""
        return self.messages[: self.prefix_len]

    @property
    def volatile_messages(self) -> list[dict]:
        return self.messages[self.prefix_len :]

    @property
    def prefix_fingerprint(self) -> str:
        """Stable hash of the cacheable prefix. Invariant across cycles => cache warm.
        Telemetry can log this; a change mid-session means the prefix moved (a bug)."""
        blob = json.dumps(self.cacheable_messages, sort_keys=True, ensure_ascii=False)
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


class PromptAssembler:
    """Builds cache-safe prompts from graph state.

    Stateless aside from the frozen system prompt it holds. ``verify_cache_safety``
    turns on a paranoid check that the cacheable prefix never embeds known volatile
    values — useful in dev, off by default.
    """

    def __init__(self, *, system_prompt: str | None = None, verify_cache_safety: bool = False):
        self._system = system_prompt if system_prompt is not None else DEFAULT_SYSTEM_PROMPT
        self._verify = verify_cache_safety

    @property
    def system_prompt_fingerprint(self) -> str:
        """Hash of synthesis policy; changing it invalidates cached scripts."""
        return hashlib.sha256(self._system.encode("utf-8")).hexdigest()

    # --- public API ---------------------------------------------------- #
    def build(self, state: GraphState, *, result_schema: dict | None = None) -> AssembledPrompt:
        messages = [
            {"role": "system", "content": self._system},
            {"role": "user", "content": self._render_task(state.intent, result_schema)},
            {"role": "user", "content": self._render_action(state)},
        ]
        prompt = AssembledPrompt(messages=messages, prefix_len=2)
        if self._verify:
            self._assert_cache_safe(prompt, state)
        return prompt

    # --- rendering ----------------------------------------------------- #
    @staticmethod
    def _render_task(intent: str, result_schema: dict | None) -> str:
        task = f"Task:\n{intent.strip()}"
        if result_schema:
            schema = json.dumps(result_schema, indent=2, sort_keys=True)
            task += (
                "\n\nThe emit_result payload should match this shape:\n"
                f"{schema}"
            )
        return task

    def _render_action(self, state: GraphState) -> str:
        lines: list[str] = [f"Attempt {state.iteration + 1}."]

        err = state.latest_error
        if err is not None:
            lines.append("")
            lines.append("The previous attempt failed:")
            lines.append(f"- error: {err.error_class.value}")
            lines.append(f"- detail: {err.message}")

            repeats = state.repeated_failure_count()
            if repeats >= 2:
                lines.append(
                    f"- This identical failure has now occurred {repeats} times. Do NOT "
                    "repeat the previous approach; change the implementation strategy "
                    "materially."
                )

            if state.plan and state.plan.required_dependencies:
                pkgs = ", ".join(state.plan.required_dependencies)
                lines.append(f"- These packages are now installed and importable: {pkgs}.")

        lines.append("")
        lines.append(_ACTION_DIRECTIVE)
        return "\n".join(lines)

    # --- dev-only safety net ------------------------------------------ #
    def _assert_cache_safe(self, prompt: AssembledPrompt, state: GraphState) -> None:
        """Raise if a high-cardinality volatile value leaked into the prefix.

        We only check values that are genuinely distinctive (session id, the latest
        error's signature/message) — never small integers, which collide with normal
        prose. The strong guarantee comes from prefix invariance (tested separately),
        not from this; this just catches gross mistakes during development.
        """
        prefix_blob = json.dumps(prompt.cacheable_messages, ensure_ascii=False)
        suspects: list[str] = [state.session_id]
        if state.latest_error is not None:
            suspects.append(state.latest_error.signature)
            if len(state.latest_error.message) >= 12:
                suspects.append(state.latest_error.message)
        leaked = [s for s in suspects if s and s in prefix_blob]
        if leaked:
            raise AssertionError(f"volatile value leaked into cacheable prefix: {leaked!r}")
