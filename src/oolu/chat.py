"""The OoLu assistant: one chat surface over the whole engine.

The commercial UI is a single conversation. Users never see assembly,
skill search, or run consoles — those are the machinery. Every message
lands here and is answered one of three ways, tried in order:

1. A deterministic rule (greetings, "what can you do") — instant and
   model-free, reusing the replies engine's narrow matching.
2. The configured chat model. It either just talks, or decides the
   message is work and hands back an intent for the engine — which does
   what it always does: find the learned skills (nodes) and path for the
   job, or synthesize new code when nothing fits.
3. No model configured: anything that isn't small talk IS work — the
   message becomes the run intent verbatim.

The assistant is transport-free: the gateway's ``/v1/chat`` route calls
``respond()`` and owns run submission, so this stays testable without
HTTP or a database.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .replies import DeterministicReplyEngine, MessageEnvelope, ReplyRule

# The model speaks JSON so "talk" and "work" stay machine-separable. `say`
# is shown to the user; a non-null `task` is submitted to the engine as a
# run intent.
SYSTEM_PROMPT = """\
You are OoLu, a personal assistant that gets real work done.

Behind you sits a workflow engine. When the user asks for something doable —
fetching, converting, organizing, computing, automating — you hand the engine
a task and it finds the learned skills and path for the job, or writes new
code when nothing fits. You never explain the machinery; the user only sees
you.

Answer with EXACTLY one JSON object, no markdown fence, of the shape:
  {"say": "<what to tell the user>", "task": "<work intent or null>"}

Set "task" to a self-contained instruction only when the user wants something
DONE. For greetings, questions about you, or ordinary conversation, answer in
"say" and set "task" to null. Never invent work the user did not ask for."""

_HELP = (
    "I'm OoLu — tell me what you need done and I'll take care of it. "
    "I learn as we go, so jobs I've done before get faster."
)

DEFAULT_RULES: tuple[ReplyRule, ...] = (
    ReplyRule(
        id="greeting",
        phrases=[
            "hi",
            "hello",
            "hey",
            "yo",
            "good morning",
            "good afternoon",
            "good evening",
        ],
        reply="Hi! I'm OoLu. Tell me what you need done.",
    ),
    ReplyRule(
        id="thanks",
        phrases=["thanks", "thank you", "thx", "ty"],
        reply="Anytime.",
    ),
    ReplyRule(
        id="capabilities",
        phrases=["help", "what can you do", "who are you", "what are you"],
        reply=_HELP,
    ),
)

# What the user hears when a message becomes work.
ACK = "On it — I'll let you know as soon as it's done or I need something from you."


@runtime_checkable
class ChatModel(Protocol):
    """Port for whichever LLM answers the conversation.

    Takes the full OpenAI-style message list (system prompt included) and
    returns the model's raw text. Adapters own transport and retries.
    """

    def reply(self, messages: list[dict]) -> str: ...


@dataclass(frozen=True)
class ChatTurn:
    """One assistant answer: something to say, and optionally work to start."""

    say: str
    task: str | None = None
    source: str = "model"  # "rule" | "model" | "intent"


def _parse_model_turn(raw: str) -> ChatTurn:
    """Extract {"say", "task"} from model text, degrading to plain speech.

    Models drift: fences, prose around the JSON, or no JSON at all. Anything
    unparseable is treated as pure conversation — a malformed reply must
    never START work the user cannot see.
    """
    candidate = raw.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```[a-zA-Z]*\n?|```$", "", candidate).strip()
    if not candidate.startswith("{"):
        match = re.search(r"\{.*\}", candidate, re.S)
        candidate = match.group(0) if match else ""
    try:
        data = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return ChatTurn(say=raw.strip() or "…", task=None)
    if not isinstance(data, dict):
        return ChatTurn(say=raw.strip() or "…", task=None)
    say = data.get("say")
    task = data.get("task")
    say = say.strip() if isinstance(say, str) and say.strip() else None
    task = task.strip() if isinstance(task, str) and task.strip() else None
    return ChatTurn(say=say or (ACK if task else "…"), task=task)


class ChatAssistant:
    """Rules first, then the model, then "everything is work"."""

    def __init__(
        self,
        *,
        rules: tuple[ReplyRule, ...] | list[ReplyRule] = DEFAULT_RULES,
        model: ChatModel | None = None,
        channel: str = "desktop",
    ):
        self._engine = DeterministicReplyEngine(list(rules))
        self._model = model
        self._channel = channel

    def respond(
        self,
        message: str,
        *,
        history: list[dict] | None = None,
        sender: str = "user",
    ) -> ChatTurn:
        envelope = MessageEnvelope(
            channel=self._channel,
            conversation_id=sender,
            sender_id=sender,
            text=message,
        )
        decision = self._engine.decide(envelope, context={})
        if decision.source == "rule" and decision.text:
            return ChatTurn(say=decision.text, task=None, source="rule")

        if self._model is not None:
            messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            for entry in history or []:
                role = entry.get("role")
                content = entry.get("content")
                if role in ("user", "assistant") and isinstance(content, str):
                    messages.append({"role": role, "content": content})
            messages.append({"role": "user", "content": message})
            return _parse_model_turn(self._model.reply(messages))

        # Model-less installs stay useful: the message is the intent.
        return ChatTurn(say=ACK, task=message.strip(), source="intent")
