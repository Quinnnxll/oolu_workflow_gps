"""The node author: a coding agent with the library in hand.

``author_node_function`` (chat.py) writes a node's function from one
thing — the goal sentence — in one shot. This module is the agentic
sibling for models that speak native tool-calling: the same creation
gates and the same script contract, but the author now WORKS for its
answer. It can read the desk's node contracts (the slot vocabulary in
circulation — reuse names, don't mint synonyms), read a named node's
recent run outputs (so code written downstream of a node parses the
shape that node actually produces, not an imagined one), and it must
finish through a schema-checked hand — ``finish_node`` with the script
and its declared interface — so the interface arrives as validated
arguments, never as an ``IO:`` line to regex out of prose.

Its own seat, its own books: the gateway seats this agent at
``node.build`` and routes its consultations under that purpose, so the
authoring spend and audit trail stand apart from the conversation's.

Every capability is an injected callable and the model is consulted
through the provider-neutral ``consult`` port, so the whole agent is
unit-testable offline — and a model without ``consult`` simply keeps
the one-shot path; nothing breaks where tool-calling hasn't arrived.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from .chat import NODE_FUNCTION_PROMPT
from .providers.tools import ToolRouter, ToolSpec

# The hands supersede the one-shot reply protocol: same judgement, same
# script contract, different delivery.
AUTHOR_HANDS_NOTE = """\

You are seated with real hands (tools) — use them INSTEAD of the reply
format above. The judgement and the script rules stand unchanged (one
self-contained script, emit_result exactly once, the web only through
http_request); only the protocol changes:

- Look before you name: call list_nodes first and REUSE slot names
  already in circulation on this desk — a route chains on exact names,
  and a synonym breaks the chain.
- Building downstream of another node? Call read_node_output with its
  node_id and write code that parses the shape it ACTUALLY produced —
  never an imagined one.
- Deliver by CALLING finish_node with the finished script and its
  interface (inputs/outputs). Do not paste the script as prose.
- If the request is conversation, not executable work, CALL decline
  with the reason instead of writing NO_TASK."""

# One entry of a finish_node interface declaration — the same trinity of
# value types the slot vocabulary knows (chat.parse_node_io's _IO_TYPES).
_IO_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "type": {"type": "string", "enum": ["str", "path", "number"]},
    },
    "required": ["name"],
    "additionalProperties": False,
}

_DEFAULT_OUTPUTS = [{"name": "result", "type": "str"}]

# What a text-only turn (no hands used, no script recoverable) is told —
# once per turn, so a chatty model is steered back, not abandoned.
_NUDGE = (
    "Deliver through the hands: call finish_node with the script and its "
    "interface, or call decline if this is conversation."
)


@dataclass(frozen=True)
class AuthoredFunction:
    """What authoring produced: the same ``(script, io, refusal)`` truth
    ``author_node_function`` speaks, plus the full tool transcript for
    the audit trail and the consultation count for the cost note."""

    script: str | None
    io: dict = field(default_factory=dict)
    refusal: str = ""
    transcript: tuple[dict, ...] = ()
    consultations: int = 0


class NodeAuthorAgent:
    """The bounded authoring loop over injected capabilities.

    ``model`` must speak ``consult(messages, tools=...)`` (the
    ``ChatModelRouter`` port). ``catalog`` lists the desk's node
    contracts; ``outputs`` returns a named node's recent run outputs;
    ``read_file`` reads THIS node's drawer (rebuild contexts); ``verify``
    runs a candidate script and reports — when present it is a HARD gate:
    ``finish_node`` refuses a script that fails it, and the refusal goes
    back to the model as a correctable answer, not an exception."""

    def __init__(
        self,
        model,
        *,
        catalog: Callable[[], list[dict]] | None = None,
        outputs: Callable[[str], list[dict]] | None = None,
        read_file: Callable[[str], str | None] | None = None,
        verify: Callable[[str], dict] | None = None,
        max_steps: int = 6,
    ) -> None:
        self._model = model
        self._catalog = catalog
        self._outputs = outputs
        self._read_file = read_file
        self._verify = verify
        self._max_steps = max_steps

    # ------------------------------------------------------------------ #
    def author(
        self, goal: str, *, demonstrated: list[str] | None = None
    ) -> AuthoredFunction:
        content = goal
        if demonstrated:
            numbered = "\n".join(
                f"{i}. {step}" for i, step in enumerate(demonstrated, start=1)
            )
            content = (
                f"{goal}\n\n"
                "The user DEMONSTRATED this procedure step by step — imitate "
                "it exactly. The numbered steps below ARE the plan: write the "
                "function that performs them in this order, never a different "
                "approach. Lines marked (observed: …) are execution logs "
                "recorded while they demonstrated.\n"
                f"{numbered}"
            )
        outcome: dict[str, Any] = {}
        router = self._hands(outcome)
        transcript: list[dict] = [
            {
                "role": "system",
                "content": NODE_FUNCTION_PROMPT + AUTHOR_HANDS_NOTE,
            },
            {"role": "user", "content": content},
        ]
        consultations = 0
        for _step in range(self._max_steps):
            try:
                reply = self._model.consult(transcript, tools=router.specs())
            except Exception as exc:  # noqa: BLE001 - a dead model builds nothing
                return AuthoredFunction(
                    None,
                    refusal=(
                        "the model could not be reached to write the "
                        f"function: {exc}"
                    ),
                    transcript=tuple(transcript),
                    consultations=consultations,
                )
            consultations += 1
            transcript.append(reply.as_message())
            if reply.tool_calls:
                for call in reply.tool_calls:
                    transcript.append(router.dispatch(call).as_message())
                finished = outcome.get("finished")
                if finished is not None:
                    return AuthoredFunction(
                        finished["script"],
                        io=finished["io"],
                        transcript=tuple(transcript),
                        consultations=consultations,
                    )
                if "declined" in outcome:
                    return AuthoredFunction(
                        None,
                        refusal=outcome["declined"],
                        transcript=tuple(transcript),
                        consultations=consultations,
                    )
                continue
            # A text-only turn: the one-shot protocol may leak through —
            # honor it (same gates as author_node_function) before nudging.
            text = reply.text
            if "NO_TASK" in text.strip().upper()[:40]:
                return AuthoredFunction(
                    None,
                    refusal=(
                        "that reads as conversation, not an executable task "
                        "— a node is its function, so there is nothing to "
                        "build"
                    ),
                    transcript=tuple(transcript),
                    consultations=consultations,
                )
            from .routing.gateway import extract_script

            script = extract_script(text)
            if script:
                from .chat import parse_node_io

                problem = self._script_problem(script)
                if problem is None:
                    return AuthoredFunction(
                        script,
                        io=parse_node_io(text),
                        transcript=tuple(transcript),
                        consultations=consultations,
                    )
            transcript.append({"role": "user", "content": _NUDGE})
        return AuthoredFunction(
            None,
            refusal=(
                "the model ran out of authoring steps without finishing "
                "the function, so nothing was built"
            ),
            transcript=tuple(transcript),
            consultations=consultations,
        )

    # ------------------------------------------------------------------ #
    def _hands(self, outcome: dict[str, Any]) -> ToolRouter:
        router = ToolRouter()
        router.register(
            ToolSpec(
                name="finish_node",
                description=(
                    "Deliver the finished node function: the complete "
                    "script and the interface it consumes/produces. This "
                    "ends the work."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "script": {"type": "string", "minLength": 1},
                        "inputs": {"type": "array", "items": _IO_ITEM_SCHEMA},
                        "outputs": {"type": "array", "items": _IO_ITEM_SCHEMA},
                    },
                    "required": ["script"],
                    "additionalProperties": False,
                },
            ),
            lambda arguments: self._finish(arguments, outcome),
        )
        router.register(
            ToolSpec(
                name="decline",
                description=(
                    "Refuse to build: the request is conversation, not "
                    "executable work. Give the reason."
                ),
                parameters={
                    "type": "object",
                    "properties": {"reason": {"type": "string", "minLength": 1}},
                    "required": ["reason"],
                    "additionalProperties": False,
                },
            ),
            lambda arguments: self._decline(arguments, outcome),
        )
        if self._catalog is not None:
            router.register(
                ToolSpec(
                    name="list_nodes",
                    description=(
                        "The desk's nodes with their contracts — the slot "
                        "names in circulation to REUSE, and each node's id "
                        "for read_node_output."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                ),
                lambda _arguments: self._catalog(),
            )
        if self._outputs is not None:
            router.register(
                ToolSpec(
                    name="read_node_output",
                    description=(
                        "A node's recent run outputs — the ACTUAL shape "
                        "upstream data arrives in. Read it before writing "
                        "code that consumes that node's work."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "node_id": {"type": "string", "minLength": 1}
                        },
                        "required": ["node_id"],
                        "additionalProperties": False,
                    },
                ),
                lambda arguments: self._outputs(arguments["node_id"]),
            )
        if self._read_file is not None:
            router.register(
                ToolSpec(
                    name="read_file",
                    description="Read a file in this node's own drawer.",
                    parameters={
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "minLength": 1}
                        },
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                ),
                lambda arguments: self._read_file(arguments["path"])
                or f"no such file: {arguments['path']}",
            )
        if self._verify is not None:
            router.register(
                ToolSpec(
                    name="verify_function",
                    description=(
                        "Run a candidate script in the sandbox and report "
                        "the outcome — verify before finishing."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "script": {"type": "string", "minLength": 1}
                        },
                        "required": ["script"],
                        "additionalProperties": False,
                    },
                ),
                lambda arguments: self._verify(arguments["script"]),
            )
        return router

    def _finish(self, arguments: dict, outcome: dict[str, Any]) -> str:
        script = arguments["script"]
        problem = self._script_problem(script)
        if problem is not None:
            # A str return still SUCCEEDS as a tool result; the refusal
            # must be an error the model corrects, so raise — the router
            # answers it as a failed result, and the loop continues.
            raise ValueError(problem)
        io = {
            "inputs": list(arguments.get("inputs") or []),
            "outputs": list(arguments.get("outputs") or _DEFAULT_OUTPUTS),
        }
        for item in io["inputs"] + io["outputs"]:
            item.setdefault("type", "str")
        outcome["finished"] = {"script": script, "io": io}
        return "recorded — the node function is finished"

    def _decline(self, arguments: dict, outcome: dict[str, Any]) -> str:
        outcome["declined"] = str(arguments["reason"]).strip() or (
            "the model judged this conversation, not an executable task"
        )
        return "recorded — nothing will be built"

    def _script_problem(self, script: str) -> str | None:
        if "emit_result" not in script:
            return (
                "the script never calls emit_result — it must import "
                "emit_result from _oolu_runtime and call it exactly once "
                "with its final answer"
            )
        if self._verify is not None:
            report = self._verify(script)
            if not report.get("ok", False):
                detail = report.get("error") or json.dumps(
                    report, ensure_ascii=False, default=str
                )
                return f"verification failed: {detail}"
        return None
