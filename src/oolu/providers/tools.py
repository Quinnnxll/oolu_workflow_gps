"""Native tool-calling: one schema, both wires, validated before dispatch.

Until now every hand the model could use rode as prose conventions — a
fenced script here, an ``IO:`` line there — parsed back out of free text.
This module gives the providers the real thing: a :class:`ToolSpec` is
declared once with a JSON-schema parameter shape and rendered onto either
wire (OpenAI ``tools=[{"type":"function",...}]``, Anthropic
``tools=[{"name",...,"input_schema"}]``); the reply comes back as a
structured :class:`ToolReply` whose :class:`ToolCall`\\ s carry parsed
arguments, not regex captures.

Between the model and any handler stands the :class:`ToolRouter`: unknown
tool names, malformed argument JSON, and schema violations never reach a
handler — they become error :class:`ToolResult`\\ s the model reads and
retries on, so a bad emission costs a turn, not a crash. The bounded
:func:`run_tool_loop` is the agent loop over that contract: consult,
dispatch, feed results back, stop on a final text or at ``max_steps``.

Everything here is pure data-shaping over injected callables — no HTTP,
no model, no clock — so the whole contract is unit-testable offline, the
same posture as the rest of the provider pipeline.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

# --------------------------------------------------------------------------- #
# The canonical shapes.                                                       #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ToolSpec:
    """One tool, declared once: name, what it does, and the JSON schema of
    its arguments. The schema is the contract both wires carry and the
    router enforces — there is no second, looser description of the
    arguments anywhere."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema, object-typed at the root

    def as_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def as_anthropic(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }


@dataclass(frozen=True)
class ToolCall:
    """One call the model asked for. ``malformed`` carries the parse error
    when the wire's argument JSON would not load — the call still exists
    (it has an id the model expects an answer under), it just cannot be
    dispatched, and the router answers it with that error instead."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    malformed: str | None = None


@dataclass(frozen=True)
class ToolResult:
    """What a dispatched call came back as, shaped to travel back to the
    model. Success or not, it is always answered — a tool call the model
    never hears back about poisons every later turn of the loop."""

    tool_call_id: str
    name: str
    success: bool
    content: str

    def as_message(self) -> dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": self.tool_call_id,
            "name": self.name,
            "content": self.content,
        }


@dataclass(frozen=True)
class ToolReply:
    """A structured consultation result: the text (may be empty on a pure
    tool-call turn), the calls (may be empty on a final answer), and the
    usage the meter books.

    ``thinking_blocks`` is a provider annex: Anthropic extended-thinking
    blocks carried VERBATIM, because the API requires an assistant
    turn's thinking blocks re-sent unchanged when its tool calls are
    answered. The annex rides the neutral transcript as an extra key —
    the Anthropic renderer re-attaches it, every other dialect drops it
    (a provider switch mid-task simply sheds thoughts the new provider
    never had), so portability survives thinking."""

    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    prompt_tokens: int = 0
    completion_tokens: int = 0
    thinking_blocks: tuple[dict, ...] = ()

    def as_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.text}
        if self.tool_calls:
            message["tool_calls"] = [
                {"id": c.id, "name": c.name, "arguments": c.arguments}
                for c in self.tool_calls
            ]
        if self.thinking_blocks:
            message["thinking_blocks"] = [dict(b) for b in self.thinking_blocks]
        return message


class ToolArgumentError(ValueError):
    """A tool call whose arguments break the declared schema."""


class ToolLoopLimit(RuntimeError):
    """The bounded loop ran out of steps before the model finished."""


class StructuredOutputError(RuntimeError):
    """The model never delivered a schema-valid structured result within
    the correction budget — surfaced, never silently defaulted."""


# --------------------------------------------------------------------------- #
# Argument validation: the schema is enforced, not decorative.                #
# --------------------------------------------------------------------------- #

_TYPES: dict[str, type | tuple[type, ...]] = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def validate_arguments(schema: dict[str, Any], args: Any) -> list[str]:
    """Every way ``args`` breaks ``schema``, as human-readable problems.

    Deliberately a subset of JSON Schema — the constructs tool parameters
    actually use (type, properties, required, additionalProperties, enum,
    items, string/number bounds, pattern) — implemented on the stdlib so
    the base install validates without growing a dependency. An empty list
    means the arguments honor the contract."""
    problems: list[str] = []
    _validate(schema, args, "arguments", problems)
    return problems


def _validate(schema: dict, value: Any, where: str, problems: list[str]) -> None:
    declared = schema.get("type")
    if declared is not None:
        expected = _TYPES.get(declared)
        if expected is None:
            problems.append(f"{where}: unsupported schema type {declared!r}")
            return
        # bool is an int subclass in Python; a schema integer must not
        # quietly accept true/false.
        wrong_bool = declared in ("integer", "number") and isinstance(value, bool)
        if wrong_bool or not isinstance(value, expected):
            problems.append(
                f"{where}: expected {declared}, got {type(value).__name__}"
            )
            return
    if "enum" in schema and value not in schema["enum"]:
        allowed = ", ".join(repr(v) for v in schema["enum"])
        problems.append(f"{where}: {value!r} is not one of [{allowed}]")
        return
    if isinstance(value, str):
        low, high = schema.get("minLength"), schema.get("maxLength")
        if low is not None and len(value) < low:
            problems.append(f"{where}: shorter than minLength {low}")
        if high is not None and len(value) > high:
            problems.append(f"{where}: longer than maxLength {high}")
        pattern = schema.get("pattern")
        if pattern is not None and re.search(pattern, value) is None:
            problems.append(f"{where}: does not match pattern {pattern!r}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        low, high = schema.get("minimum"), schema.get("maximum")
        if low is not None and value < low:
            problems.append(f"{where}: below minimum {low}")
        if high is not None and value > high:
            problems.append(f"{where}: above maximum {high}")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for name in schema.get("required", ()):
            if name not in value:
                problems.append(f"{where}: missing required property {name!r}")
        extras_allowed = schema.get("additionalProperties", True)
        for name, item in value.items():
            subschema = properties.get(name)
            if subschema is not None:
                _validate(subschema, item, f"{where}.{name}", problems)
            elif extras_allowed is False:
                problems.append(f"{where}: unexpected property {name!r}")
    if isinstance(value, list):
        items = schema.get("items")
        if items is not None:
            for index, item in enumerate(value):
                _validate(items, item, f"{where}[{index}]", problems)


# --------------------------------------------------------------------------- #
# Wire conversion: one neutral transcript, two provider dialects.             #
# --------------------------------------------------------------------------- #
# The neutral transcript is the OpenAI-flavored role list the rest of the
# codebase already speaks, extended with structured entries:
#   assistant turn with calls:
#     {"role": "assistant", "content": str,
#      "tool_calls": [{"id", "name", "arguments": dict}]}
#   a tool's answer:
#     {"role": "tool", "tool_call_id": str, "name": str, "content": str}


def to_openai_messages(messages: list[dict]) -> list[dict]:
    """The neutral transcript in OpenAI dialect (arguments as JSON text)."""
    wire: list[dict] = []
    for message in messages:
        calls = message.get("tool_calls")
        if message.get("role") == "assistant" and calls:
            wire.append(
                {
                    "role": "assistant",
                    "content": message.get("content") or None,
                    "tool_calls": [
                        {
                            "id": call["id"],
                            "type": "function",
                            "function": {
                                "name": call["name"],
                                "arguments": json.dumps(
                                    call.get("arguments", {}), ensure_ascii=False
                                ),
                            },
                        }
                        for call in calls
                    ],
                }
            )
        elif message.get("role") == "tool":
            wire.append(
                {
                    "role": "tool",
                    "tool_call_id": message.get("tool_call_id", ""),
                    "content": message.get("content", ""),
                }
            )
        else:
            wire.append({"role": message["role"], "content": message.get("content", "")})
    return wire


def to_anthropic_messages(messages: list[dict]) -> list[dict]:
    """The neutral transcript in Anthropic dialect: tool calls as
    ``tool_use`` content blocks, tool answers as ``tool_result`` blocks in
    a user turn — consecutive answers merged into one turn, since they
    answer one assistant turn's batch of calls."""
    wire: list[dict] = []
    for message in messages:
        role = message.get("role")
        calls = message.get("tool_calls")
        thinking = message.get("thinking_blocks") or ()
        if role == "assistant" and (calls or thinking):
            blocks: list[dict] = []
            # Thinking blocks lead, verbatim — the API refuses a tool
            # transcript whose thoughts were dropped or reworded.
            blocks.extend(dict(b) for b in thinking)
            if message.get("content"):
                blocks.append({"type": "text", "text": message["content"]})
            for call in calls or ():
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call["id"],
                        "name": call["name"],
                        "input": call.get("arguments", {}),
                    }
                )
            wire.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": message.get("tool_call_id", ""),
                "content": message.get("content", ""),
            }
            if wire and wire[-1]["role"] == "user" and isinstance(
                wire[-1]["content"], list
            ):
                wire[-1]["content"].append(block)
            else:
                wire.append({"role": "user", "content": [block]})
        else:
            wire.append({"role": role, "content": message.get("content", "")})
    return wire


def parse_openai_tool_reply(data: dict) -> ToolReply:
    """A :class:`ToolReply` from a chat/completions response — the wire
    shape OpenAI and every local server speaks."""
    choices = data.get("choices") or []
    message = (choices[0] or {}).get("message", {}) if choices else {}
    calls: list[ToolCall] = []
    for entry in message.get("tool_calls") or []:
        function = entry.get("function", {}) or {}
        raw = function.get("arguments", "") or ""
        arguments: dict[str, Any] = {}
        malformed = None
        try:
            parsed = json.loads(raw) if raw else {}
            if isinstance(parsed, dict):
                arguments = parsed
            else:
                malformed = f"arguments must be a JSON object, got {raw!r}"
        except ValueError:
            malformed = f"arguments are not valid JSON: {raw!r}"
        calls.append(
            ToolCall(
                id=str(entry.get("id", "")),
                name=str(function.get("name", "")),
                arguments=arguments,
                malformed=malformed,
            )
        )
    usage = data.get("usage", {}) or {}
    return ToolReply(
        text=message.get("content") or "",
        tool_calls=tuple(calls),
        prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
        completion_tokens=int(usage.get("completion_tokens", 0) or 0),
    )


def parse_anthropic_tool_reply(data: dict) -> ToolReply:
    """A :class:`ToolReply` from an Anthropic messages response: text
    blocks joined, ``tool_use`` blocks as calls (their input is already
    parsed JSON on this wire — malformation is an OpenAI-dialect problem)."""
    text_parts: list[str] = []
    calls: list[ToolCall] = []
    thinking: list[dict] = []
    for block in data.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") in ("thinking", "redacted_thinking"):
            # Carried verbatim — signatures and all — so a tool turn can
            # send them back exactly as issued.
            thinking.append(dict(block))
        elif block.get("type") == "tool_use":
            raw = block.get("input")
            arguments = raw if isinstance(raw, dict) else {}
            malformed = (
                None
                if isinstance(raw, dict)
                else f"tool input must be an object, got {raw!r}"
            )
            calls.append(
                ToolCall(
                    id=str(block.get("id", "")),
                    name=str(block.get("name", "")),
                    arguments=arguments,
                    malformed=malformed,
                )
            )
    usage = data.get("usage", {}) or {}
    return ToolReply(
        text="".join(text_parts),
        tool_calls=tuple(calls),
        prompt_tokens=int(usage.get("input_tokens", 0) or 0),
        completion_tokens=int(usage.get("output_tokens", 0) or 0),
        thinking_blocks=tuple(thinking),
    )


# --------------------------------------------------------------------------- #
# The router: nothing undeclared, nothing unvalidated, reaches a handler.     #
# --------------------------------------------------------------------------- #


class ToolRouter:
    """Name → (spec, handler), with the schema standing guard in between.

    ``dispatch`` never raises for the model's mistakes — an unknown name,
    malformed JSON, or a schema violation comes back as an error
    :class:`ToolResult` the model can read and correct on its next turn.
    A handler that itself blows up is likewise answered, not fatal: the
    loop's job is to keep the conversation alive; the audit of what
    actually failed is the handler's own concern."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[ToolSpec, Callable[[dict], Any]]] = {}

    def register(self, spec: ToolSpec, handler: Callable[[dict], Any]) -> None:
        if spec.name in self._entries:
            raise ValueError(f"tool {spec.name!r} is already registered")
        self._entries[spec.name] = (spec, handler)

    def specs(self) -> list[ToolSpec]:
        return [spec for spec, _handler in self._entries.values()]

    def dispatch(self, call: ToolCall) -> ToolResult:
        def refuse(reason: str) -> ToolResult:
            return ToolResult(
                tool_call_id=call.id, name=call.name, success=False, content=reason
            )

        if call.malformed:
            return refuse(call.malformed)
        entry = self._entries.get(call.name)
        if entry is None:
            known = ", ".join(sorted(self._entries)) or "none"
            return refuse(f"unknown tool {call.name!r}; available tools: {known}")
        spec, handler = entry
        problems = validate_arguments(spec.parameters, call.arguments)
        if problems:
            return refuse("invalid arguments: " + "; ".join(problems))
        try:
            outcome = handler(call.arguments)
        except Exception as exc:  # answered, not fatal — see class docstring
            return refuse(f"tool failed: {type(exc).__name__}: {exc}")
        content = (
            outcome
            if isinstance(outcome, str)
            else json.dumps(outcome, ensure_ascii=False, default=str)
        )
        return ToolResult(
            tool_call_id=call.id, name=call.name, success=True, content=content
        )


def run_tool_loop(
    consult: Callable[[list[dict]], ToolReply],
    messages: list[dict],
    router: ToolRouter,
    *,
    max_steps: int = 8,
) -> tuple[str, list[dict]]:
    """The bounded agent loop: consult, dispatch every call through the
    router, feed the answers back, until the model speaks a final text.

    Returns ``(final_text, transcript)`` — the transcript is the full
    neutral message list including every tool exchange, ready to audit or
    to hand to a follow-up consultation. Raises :class:`ToolLoopLimit`
    when ``max_steps`` consultations pass without a final answer; the
    ceiling is the caller's budget, never the model's to negotiate."""
    transcript = list(messages)
    for _step in range(max_steps):
        reply = consult(transcript)
        transcript.append(reply.as_message())
        if not reply.tool_calls:
            return reply.text, transcript
        for call in reply.tool_calls:
            transcript.append(router.dispatch(call).as_message())
    raise ToolLoopLimit(f"no final answer within {max_steps} steps")
