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
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .durable.files import FileTooLargeError, UserFile, UserFileStore
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
"say" and set "task" to null. Never invent work the user did not ask for.

You also have tools over the user's own files (documents and sheets). To use
one, answer with EXACTLY one JSON object of the shape:
  {"tool": "list_files", "args": {}}
  {"tool": "read_file", "args": {"name": "<file name>"}}
  {"tool": "write_file", "args": {"name": "<file name>", "content": "<the full new content>"}}
The tool's result arrives as the next message; then answer the user with the
{"say", "task"} shape. write_file replaces the whole file — read it first
when editing. Touch only files the user asked about."""

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
    """One assistant answer: something to say, and optionally work to start.

    ``actions`` is the audited trail of tool uses behind the answer — the
    function words the UI shows so the user can verify what was touched.
    """

    say: str
    task: str | None = None
    source: str = "model"  # "rule" | "model" | "intent" | "tool"
    actions: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class _ToolCall:
    name: str
    args: dict


# The model may use at most this many tools per turn; then it must speak.
MAX_TOOL_ROUNDS = 4


def _parse_model_reply(raw: str) -> "ChatTurn | _ToolCall":
    """A model reply is either a spoken turn or a tool call."""
    turn = _parse_model_turn(raw)
    candidate = raw.strip()
    match = re.search(r"\{.*\}", candidate, re.S)
    if match:
        try:
            data = json.loads(match.group(0))
        except (json.JSONDecodeError, ValueError):
            data = None
        if isinstance(data, dict) and isinstance(data.get("tool"), str):
            args = data.get("args")
            return _ToolCall(
                name=data["tool"].strip(),
                args=args if isinstance(args, dict) else {},
            )
    return turn


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


@runtime_checkable
class ChatTools(Protocol):
    """The assistant's hands: what a chat turn may touch besides words."""

    def list_files(self) -> list[UserFile]: ...
    def resolve(self, name: str) -> list[UserFile]: ...
    def write_file(self, name: str, content: str) -> UserFile: ...


class FileChatTools:
    """Tenant-bound file tools over the durable file store."""

    def __init__(self, store: UserFileStore, *, tenant: str):
        self._store = store
        self._tenant = tenant

    def list_files(self) -> list[UserFile]:
        return self._store.list(tenant=self._tenant)

    def resolve(self, name: str) -> list[UserFile]:
        """Exact name first; else case-insensitive substring matches."""
        wanted = name.strip().casefold()
        files = self.list_files()
        exact = [f for f in files if f.name.casefold() == wanted]
        if exact:
            return exact
        return [f for f in files if wanted and wanted in f.name.casefold()]

    def write_file(self, name: str, content: str) -> UserFile:
        matches = self.resolve(name)
        if len(matches) == 1:
            updated = matches[0].model_copy(update={"content": content})
            return self._store.save(updated)
        return self._store.save(
            UserFile(tenant_id=self._tenant, name=name.strip(), content=content)
        )


# What a file's content preview in chat is capped at.
_READ_CAP = 4_000

_READ_RE = re.compile(r"^(?:read|open|show)\s+(.+?)\s*$", re.I)
_WRITE_RE = re.compile(r"^(?:write to|append to)\s+([^:]+):\s*(.+)$", re.I | re.S)
_LIST_PHRASES = frozenset(
    {"list files", "list my files", "show files", "show my files", "my files"}
)


def _file_command(message: str, tools: ChatTools) -> ChatTurn | None:
    """Deterministic file commands for model-less installs.

    Narrow on purpose (the replies-engine philosophy): exact command shapes
    only, and a read is a read only when the named file actually exists —
    anything else falls through to the run pipeline untouched.
    """
    text = message.strip()
    if text.casefold().rstrip(".!?") in _LIST_PHRASES:
        files = tools.list_files()
        if not files:
            say = "You have no files yet — ask me to write one, or press + in Files."
        else:
            listing = "\n".join(f"• {f.name} ({f.size} bytes)" for f in files)
            say = f"Your files:\n{listing}"
        return ChatTurn(say=say, source="tool", actions=[{"tool": "list_files"}])

    write = _WRITE_RE.match(text)
    if write:
        name, content = write.group(1).strip(), write.group(2)
        appending = text.casefold().startswith("append")
        matches = tools.resolve(name)
        if len(matches) > 1:
            names = ", ".join(f.name for f in matches)
            return ChatTurn(
                say=f"Which one do you mean: {names}?", source="tool"
            )
        if appending and matches:
            base = matches[0].content
            content = (base + "\n" if base else "") + content
        elif appending and not matches:
            pass  # appending to a new file just creates it
        try:
            saved = tools.write_file(matches[0].name if matches else name, content)
        except FileTooLargeError as exc:
            return ChatTurn(say=str(exc), source="tool")
        return ChatTurn(
            say=f"Saved {saved.name}.",
            source="tool",
            actions=[{"tool": "write_file", "name": saved.name}],
        )

    read = _READ_RE.match(text)
    if read:
        matches = tools.resolve(read.group(1))
        if len(matches) == 1:
            file = matches[0]
            body = file.content or "(the file is empty)"
            if len(body) > _READ_CAP:
                body = body[:_READ_CAP] + "\n… (truncated)"
            return ChatTurn(
                say=f"{file.name}:\n{body}",
                source="tool",
                actions=[{"tool": "read_file", "name": file.name}],
            )
        if len(matches) > 1:
            names = ", ".join(f.name for f in matches)
            return ChatTurn(say=f"Which one do you mean: {names}?", source="tool")
        # No such file: not a file command after all.
    return None


def _run_tool(tools: ChatTools, call: _ToolCall) -> tuple[str, dict | None]:
    """Execute a model's tool call; the result string goes back to it."""
    if call.name == "list_files":
        files = tools.list_files()
        listing = "\n".join(f"{f.name} ({f.size} bytes)" for f in files) or "(none)"
        return listing, {"tool": "list_files"}
    if call.name == "read_file":
        matches = tools.resolve(str(call.args.get("name", "")))
        if len(matches) != 1:
            return "error: no such file" if not matches else "error: ambiguous name", None
        return matches[0].content[:_READ_CAP], {
            "tool": "read_file",
            "name": matches[0].name,
        }
    if call.name == "write_file":
        name = str(call.args.get("name", "")).strip()
        if not name:
            return "error: a file name is required", None
        try:
            saved = tools.write_file(name, str(call.args.get("content", "")))
        except FileTooLargeError as exc:
            return f"error: {exc}", None
        return f"saved {saved.name}", {"tool": "write_file", "name": saved.name}
    return f"error: unknown tool '{call.name}'", None


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
        tools: ChatTools | None = None,
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
            actions: list[dict] = []
            for _ in range(MAX_TOOL_ROUNDS):
                raw = self._model.reply(messages)
                parsed = _parse_model_reply(raw)
                if isinstance(parsed, _ToolCall):
                    if tools is None:
                        return ChatTurn(
                            say="I can't reach any files on this host.",
                            source="model",
                            actions=actions,
                        )
                    result, action = _run_tool(tools, parsed)
                    if action is not None:
                        actions.append(action)
                    messages.append({"role": "assistant", "content": raw})
                    messages.append(
                        {"role": "user", "content": f"[tool result]\n{result}"}
                    )
                    continue
                return ChatTurn(
                    say=parsed.say,
                    task=parsed.task,
                    source="model",
                    actions=actions,
                )
            return ChatTurn(
                say="I got tangled up in my tools — tell me exactly what you need.",
                source="model",
                actions=actions,
            )

        # Model-less installs stay useful: exact file commands work without
        # any model, and everything else is the intent.
        if tools is not None:
            command = _file_command(message, tools)
            if command is not None:
                return command
        return ChatTurn(say=ACK, task=message.strip(), source="intent")
