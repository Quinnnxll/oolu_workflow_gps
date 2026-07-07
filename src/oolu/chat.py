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
from .settings_node import SettingError

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
  {"tool": "list_runs", "args": {}}
  {"tool": "run_log", "args": {"run_id": "<a run id, or a phrase from its intent>"}}
  {"tool": "list_nodes", "args": {}}
  {"tool": "get_settings", "args": {}}
  {"tool": "set_setting", "args": {"key": "<a settings key>", "value": <the new value>}}
The tool's result arrives as the next message; then answer the user with the
{"say", "task"} shape. write_file replaces the whole file — read it first
when editing. Touch only files the user asked about. To redo past work, set
"task" to that run's intent — there is no tool for starting work."""

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


class ModelUnavailable(RuntimeError):
    """The model could not answer (no key, network down, provider errors).

    The assistant catches this and degrades to its model-less path — a dead
    model must never mean a dead conversation.
    """


class ModelBudgetExceeded(RuntimeError):
    """The model spending cap is reached. ``str(exc)`` is what the assistant
    says out loud — a refusal in words, never a silent skip."""


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
        # The assistant's hands reach the Life drawer, not node files.
        return self._store.list(tenant=self._tenant, node_id=None)

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


@runtime_checkable
class EngineTools(Protocol):
    """The engine's read surface: what a chat turn may inspect."""

    def list_runs(self) -> list[dict]: ...
    def run_log(self, run_id: str) -> list[dict]: ...
    def list_nodes(self) -> list[dict]: ...
    def get_settings(self) -> list[dict]: ...
    def set_setting(self, key: str, value: object) -> str: ...


class GatewayChatTools(FileChatTools):
    """File tools plus the engine's read surface, tenant-bound.

    Everything here is read-only over stores the gateway already scopes:
    the caller's runs, their audit steps, and the caller's node desk. New
    work still flows only through the run pipeline (``ChatTurn.task``).
    """

    def __init__(
        self,
        store: UserFileStore,
        *,
        tenant: str,
        principal: str = "",
        durable=None,  # durable.DurableWorkflowService
        desk=None,  # nodeplace.WorkDesk
        settings=None,  # settings_node.SettingsNode
    ):
        super().__init__(store, tenant=tenant)
        self._chat_tenant = tenant
        self._principal = principal
        self._durable = durable
        self._desk = desk
        self._settings = settings

    def list_runs(self) -> list[dict]:
        if self._durable is None:
            return []
        runs = [
            state
            for state in self._durable.runs.list(limit=10_000)
            if state.contract.metadata.get("tenant_id") == self._chat_tenant
        ]
        summaries = []
        for state in runs:
            awaiting = None
            pause = getattr(state, "pause", None)
            if pause is not None:
                kind = pause.kind
                awaiting = kind.value if hasattr(kind, "value") else str(kind)
            summaries.append(
                {
                    "run_id": state.run_id,
                    "intent": state.intent,
                    "phase": state.phase.value
                    if hasattr(state.phase, "value")
                    else str(state.phase),
                    "awaiting": awaiting,
                }
            )
        return summaries

    def run_log(self, run_id: str) -> list[dict]:
        if self._durable is None:
            return []
        return [
            {"seq": r.seq, "event_type": r.event_type, "at": r.at.isoformat()}
            for r in self._durable.audit.records(run_id=run_id)
        ]

    def list_nodes(self) -> list[dict]:
        if self._desk is None:
            return []
        return [
            {
                "title": entry.title,
                "status": entry.status,
                "earnings_micros": entry.earnings_micros,
                "health": entry.health.score,
            }
            for entry in self._desk.overview(
                principal=self._principal, tenant=self._chat_tenant
            )
        ]

    def get_settings(self) -> list[dict]:
        if self._settings is None:
            return []
        return self._settings.describe(self._chat_tenant)

    def set_setting(self, key: str, value: object) -> str:
        """Apply one setting through the node's bounded door, or report why
        it was refused — the assistant never gets a code path around it."""
        if self._settings is None:
            return "error: settings are not enabled"
        try:
            applied = self._settings.set(self._chat_tenant, key, value)
        except SettingError as exc:
            return f"error: {exc}"
        return f"set {key} to {applied}"


# The engine's events in the assistant's voice (compact, chat-sized; the
# frontend keeps its own richer map for run cards).
_EVENT_WORDS = {
    "workflow.submitted": "accepted the job",
    "workflow.started": "started working",
    "workflow.advance": "moved to the next step",
    "workflow.advanced": "moved to the next step",
    "workflow.executed": "carried out the actions",
    "workflow.paused": "paused for input",
    "workflow.resumed": "picked it back up",
    "workflow.completed": "finished the job",
    "workflow.failed": "failed",
    "workflow.incident": "hit a problem",
    "workflow.cancelled": "stopped on request",
    "workflow.preflight_failed": "stopped before running",
    "skill.blocked": "blocked an unsafe action",
}


def _speak_event(event_type: str) -> str:
    return _EVENT_WORDS.get(event_type, event_type.replace(".", " ").replace("_", " "))


def _speak_status(run: dict) -> str:
    if run.get("awaiting"):
        return f"waiting on you ({run['awaiting']})"
    return str(run.get("phase", "working"))


def _resolve_run(runs: list[dict], ref: str) -> list[dict]:
    """A run by id, id prefix, or intent substring — never a guess."""
    wanted = ref.strip().casefold()
    if not wanted:
        return []
    exact = [r for r in runs if r["run_id"].casefold() == wanted]
    if exact:
        return exact
    prefix = [r for r in runs if r["run_id"].casefold().startswith(wanted)]
    if len(prefix) == 1:
        return prefix
    by_intent = [r for r in runs if wanted in r["intent"].casefold()]
    if by_intent:
        return by_intent
    # Word-wise: every non-filler word must appear in the intent.
    tokens = [t for t in wanted.split() if t not in {"the", "a", "an", "my", "that"}]
    if tokens:
        by_words = [
            r for r in runs if all(t in r["intent"].casefold() for t in tokens)
        ]
        if by_words:
            return by_words
    return prefix


def _run_log_say(run: dict, steps: list[dict]) -> str:
    lines = []
    for step in steps[:20]:
        at = step.get("at", "")
        clock = at.split("T")[1][:8] if "T" in at else at
        lines.append(f"• {clock} — {_speak_event(step.get('event_type', ''))}")
    body = "\n".join(lines) if lines else "(no steps recorded)"
    return (
        f"Here's what happened with \"{run['intent']}\""
        f" (run {run['run_id'][:8]}):\n{body}"
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

    if isinstance(tools, EngineTools):
        engine = _engine_command(text, tools)
        if engine is not None:
            return engine

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


_RUN_LIST_PHRASES = frozenset(
    {
        "list runs",
        "my runs",
        "show runs",
        "show my runs",
        "my tasks",
        "list tasks",
        "show my tasks",
        "what is running",
        "what's running",
    }
)
_NODE_LIST_PHRASES = frozenset(
    {"my nodes", "list nodes", "show my nodes", "node status", "how are my nodes"}
)
_RERUN_RE = re.compile(r"^(?:run again|rerun|re-run|retry)\s+(.+?)\s*$", re.I)
_REVIEW_RE = re.compile(
    r"^(?:review|audit|what happened (?:with|to))\s+(.+?)\s*$", re.I
)


def _engine_command(text: str, tools: EngineTools) -> ChatTurn | None:
    """Deterministic engine commands: inspect runs and nodes, redo work."""
    lowered = text.casefold().rstrip(".!?")

    if lowered in _RUN_LIST_PHRASES:
        runs = tools.list_runs()
        if not runs:
            say = "Nothing has run yet — just tell me what you need done."
        else:
            lines = "\n".join(
                f"• {r['intent']} — {_speak_status(r)} ({r['run_id'][:8]})"
                for r in runs[-15:]
            )
            say = f"Your tasks:\n{lines}"
        return ChatTurn(say=say, source="tool", actions=[{"tool": "list_runs"}])

    if lowered in _NODE_LIST_PHRASES:
        nodes = tools.list_nodes()
        if not nodes:
            say = "You have no nodes yet — switch to Work and press + to start one."
        else:
            lines = "\n".join(
                f"• {n['title']} — {n['status'].replace('_', ' ')},"
                f" ${n['earnings_micros'] / 1_000_000:.2f} earned,"
                + (
                    f" {round(n['health'] * 100)}% healthy"
                    if n["health"] is not None
                    else " no verified runs yet"
                )
                for n in nodes
            )
            say = f"Your nodes:\n{lines}"
        return ChatTurn(say=say, source="tool", actions=[{"tool": "list_nodes"}])

    rerun = _RERUN_RE.match(text)
    if rerun:
        matches = _resolve_run(tools.list_runs(), rerun.group(1))
        if len(matches) == 1:
            run = matches[0]
            return ChatTurn(
                say=f"Running \"{run['intent']}\" again.",
                task=run["intent"],
                source="tool",
                actions=[{"tool": "run_again", "name": run["run_id"][:8]}],
            )
        if len(matches) > 1:
            names = "; ".join(
                f"{r['intent']} ({r['run_id'][:8]})" for r in matches[:6]
            )
            return ChatTurn(say=f"Which one do you mean: {names}?", source="tool")
        return ChatTurn(
            say=f"I couldn't find a past task matching \"{rerun.group(1)}\".",
            source="tool",
        )

    settings = _settings_command(text, tools)
    if settings is not None:
        return settings

    review = _REVIEW_RE.match(text)
    if review:
        matches = _resolve_run(tools.list_runs(), review.group(1))
        if len(matches) == 1:
            run = matches[0]
            return ChatTurn(
                say=_run_log_say(run, tools.run_log(run["run_id"])),
                source="tool",
                actions=[{"tool": "run_log", "name": run["run_id"][:8]}],
            )
        if len(matches) > 1:
            names = "; ".join(
                f"{r['intent']} ({r['run_id'][:8]})" for r in matches[:6]
            )
            return ChatTurn(say=f"Which one do you mean: {names}?", source="tool")
        # No matching run: the message is probably new work — fall through.
    return None


_SETTINGS_LIST_PHRASES = frozenset(
    {"settings", "my settings", "show settings", "show my settings", "app settings"}
)
# "set <key> to <value>" / "set my <label> to <value>".
_SET_RE = re.compile(r"^set\s+(?:my\s+)?(.+?)\s+to\s+(.+?)\s*$", re.I)


def _match_setting(described: list[dict], phrase: str) -> list[dict]:
    """A setting by key, or by a label/key substring — never a guess."""
    wanted = phrase.strip().casefold()
    exact = [s for s in described if s["key"].casefold() == wanted]
    if exact:
        return exact
    return [
        s
        for s in described
        if wanted in s["key"].casefold() or wanted in s["label"].casefold()
    ]


def _settings_command(text: str, tools: EngineTools) -> ChatTurn | None:
    if not hasattr(tools, "get_settings"):
        return None
    lowered = text.casefold().rstrip(".!?")

    if lowered in _SETTINGS_LIST_PHRASES:
        described = tools.get_settings()
        if not described:
            return ChatTurn(say="Settings aren't available here.", source="tool")
        lines = "\n".join(f"• {s['label']}: {s['value']}" for s in described)
        return ChatTurn(
            say=f"Your settings:\n{lines}",
            source="tool",
            actions=[{"tool": "get_settings"}],
        )

    setter = _SET_RE.match(text)
    if setter:
        described = tools.get_settings()
        if not described:
            return None
        matches = _match_setting(described, setter.group(1))
        if len(matches) == 1:
            key = matches[0]["key"]
            result = tools.set_setting(key, setter.group(2).strip())
            if result.startswith("error:"):
                return ChatTurn(
                    say=f"I couldn't: {result[7:].strip()}", source="tool"
                )
            return ChatTurn(
                say=f"Done — {matches[0]['label']} is now"
                f" {result.split(' to ', 1)[-1]}.",
                source="tool",
                actions=[{"tool": "set_setting", "name": key}],
            )
        if len(matches) > 1:
            names = ", ".join(m["label"] for m in matches[:6])
            return ChatTurn(
                say=f"Which setting do you mean: {names}?", source="tool"
            )
        # No setting by that name: probably not a settings command.
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
    if call.name == "list_runs" and isinstance(tools, EngineTools):
        runs = tools.list_runs()
        listing = (
            "\n".join(
                f"{r['run_id'][:8]} {r['intent']} — {_speak_status(r)}"
                for r in runs[-20:]
            )
            or "(none)"
        )
        return listing, {"tool": "list_runs"}
    if call.name == "run_log" and isinstance(tools, EngineTools):
        matches = _resolve_run(tools.list_runs(), str(call.args.get("run_id", "")))
        if len(matches) != 1:
            return (
                "error: no such run" if not matches else "error: ambiguous reference",
                None,
            )
        run = matches[0]
        steps = tools.run_log(run["run_id"])
        listing = "\n".join(
            f"{s['at']} {s['event_type']}" for s in steps[:40]
        ) or "(no steps recorded)"
        return f"run {run['run_id']} \"{run['intent']}\":\n{listing}", {
            "tool": "run_log",
            "name": run["run_id"][:8],
        }
    if call.name == "get_settings" and isinstance(tools, EngineTools):
        described = tools.get_settings()
        listing = "\n".join(
            f"{s['key']} = {s['value']}"
            + (f" (one of: {', '.join(s['choices'])})" if s.get("choices") else "")
            + (
                f" (range {s.get('minimum')}..{s.get('maximum')})"
                if s.get("kind") == "number"
                else ""
            )
            for s in described
        ) or "(none)"
        return listing, {"tool": "get_settings"}
    if call.name == "set_setting" and isinstance(tools, EngineTools):
        key = str(call.args.get("key", ""))
        result = tools.set_setting(key, call.args.get("value"))
        action = None if result.startswith("error:") else {
            "tool": "set_setting",
            "name": key,
        }
        return result, action
    if call.name == "list_nodes" and isinstance(tools, EngineTools):
        nodes = tools.list_nodes()
        listing = (
            "\n".join(
                f"{n['title']} — {n['status']},"
                f" earnings {n['earnings_micros']} micros, health {n['health']}"
                for n in nodes
            )
            or "(none)"
        )
        return listing, {"tool": "list_nodes"}
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
        model: ChatModel | None = None,
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

        # A per-call model (the gateway's per-tenant router) outranks the
        # constructor's; either way an unusable model degrades, not dies.
        active = model or self._model
        if active is not None:
            try:
                return self._model_turn(active, message, history, tools)
            except ModelBudgetExceeded as exc:
                return ChatTurn(say=str(exc), task=None, source="model")
            except ModelUnavailable:
                pass  # fall through to the model-less path below

        # Model-less installs stay useful: exact file commands work without
        # any model, and everything else is the intent.
        if tools is not None:
            command = _file_command(message, tools)
            if command is not None:
                return command
        return ChatTurn(say=ACK, task=message.strip(), source="intent")

    def _model_turn(
        self,
        model: ChatModel,
        message: str,
        history: list[dict] | None,
        tools: ChatTools | None,
    ) -> ChatTurn:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for entry in history or []:
            role = entry.get("role")
            content = entry.get("content")
            if role in ("user", "assistant") and isinstance(content, str):
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": message})
        actions: list[dict] = []
        for _ in range(MAX_TOOL_ROUNDS):
            raw = model.reply(messages)
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
