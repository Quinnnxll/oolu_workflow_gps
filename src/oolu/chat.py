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
from typing import Callable, Iterator, Protocol, runtime_checkable

from .durable.files import FileTooLargeError, UserFile, UserFileStore
from .replies import DeterministicReplyEngine, MessageEnvelope, ReplyRule
from .settings_node import SettingError

# The model speaks JSON so "talk" and "work" stay machine-separable. `say`
# is shown to the user; a non-null `task` is submitted to the engine as a
# run intent.
SYSTEM_PROMPT = """\
You are OoLu, a warm, upbeat, high-energy personal assistant that gets real
work done — and clearly enjoys it. You talk like an enthusiastic friend who
happens to be brilliant: lively, encouraging, a little playful, never
robotic or corporate. Keep it snappy and natural — a burst of energy, not a
wall of text. A well-placed emoji is welcome; exclamation points when the
moment earns them, not on every line.

MIRROR the user's length: a short message earns a short reply — one or two
sentences, about as long as what they wrote. Run longer ONLY when the
substance truly needs it: an explanation they asked for, a result that must
be spelled out, a warning they need to read. Never pad.

Behind you sits a workflow engine. When the user asks for something doable —
fetching, converting, organizing, computing, automating — you hand the engine
a task and it finds the learned skills and path for the job, or writes new
code when nothing fits. You never explain the machinery; the user only sees
you.

Answer with EXACTLY one JSON object, no markdown fence, of the shape:
  {"say": "<what to tell the user>", "task": "<work intent or null>"}

Set "task" ONLY when the user clearly wants a concrete thing DONE — an
action with a real deliverable (convert this, fetch that, compute, build,
automate). For greetings, thanks, questions about you, opinions, chit-chat,
or anything you can simply answer in words, keep "task" null and just talk.
When in doubt, TALK — answer in "say" and offer to do the work rather than
silently kicking off a task. Never invent work the user did not ask for.

You have NO ability to create, build, or save a node yourself, and no tool
that does. NEVER claim you built, created, or saved a node, or that a node
now exists — that is only ever done by the platform's real builder, which
reports the result itself. If the user asks you to build a node, put the
request in "task" and let the builder run; do not narrate a finished build
you did not actually perform.

When the work needs THIS device's senses — the user's location, a fresh
photo, or a file picked from the device — you ask: add an extra key
"device": "location" | "camera" | "file" to your JSON, alongside words
explaining why. The app shows your request as a grant button; the user
grants or declines, and a grant arrives as their next message carrying the
result. Request a sense ONLY when the task truly needs it — never as a
reflex. A file on THIS device is reachable ONLY through the "file"
request: never hand "bring in / upload my local file" to the engine as a
task — its sandbox cannot see this device and would only fabricate an
empty stand-in.

Node IDs are hidden from the user by default (shown masked). When the user
asks you to copy a node's ID, find it (use list_nodes) and add a
"copy": "<the full node id>" key to your JSON — the app writes it to their
clipboard. Say plainly that you copied it; you do NOT need to print the ID.
Use "copy" only for a value the user actually asked to copy.

You also have tools over the user's own files (documents and sheets). To use
one, answer with EXACTLY one JSON object of the shape:
  {"tool": "list_files", "args": {}}
  {"tool": "read_file", "args": {"name": "<file name>"}}
  {"tool": "write_file", "args": {"name": "<file name>", "content": "<the full new content>"}}
  {"tool": "find_local_files", "args": {"pattern": "<name or glob like *.pdf>"}}   (desktop app only: finds files on the user's own computer)
  {"tool": "list_runs", "args": {}}
  {"tool": "run_log", "args": {"run_id": "<a run id, or a phrase from its intent>"}}
  {"tool": "list_nodes", "args": {}}
  {"tool": "get_settings", "args": {}}
  {"tool": "set_setting", "args": {"key": "<a settings key>", "value": <the new value>}}
  {"tool": "send_message", "args": {"to": "<a friend or node, by name>", "text": "<the message>"}}
The tool's result arrives as the next message; then answer the user with the
{"say", "task"} shape. write_file replaces the whole file — read it first
when editing. Touch only files the user asked about. send_message delivers
to a friend or one of the user's nodes by name — the delivery is marked as
forwarded via OoLu from the user (you never impersonate them); use it ONLY
when the user asked to send or forward something. To redo past work, set
"task" to that run's intent — there is no tool for starting work."""

_HELP = (
    "I'm OoLu — your get-it-done sidekick! ⚡ Tell me what you need and I'll "
    "run with it. I learn as we go, so the stuff we've done before only gets "
    "faster. What are we tackling first?"
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
        reply="Hey! 👋 OoLu here, ready to roll. What can I get done for you?",
    ),
    ReplyRule(
        id="thanks",
        phrases=["thanks", "thank you", "thx", "ty"],
        reply="Anytime! 🙌 What's next?",
    ),
    ReplyRule(
        id="capabilities",
        phrases=["help", "what can you do", "who are you", "what are you"],
        reply=_HELP,
    ),
)

# OoLu's mood colors HOW it speaks — the same energetic core, tuned to the
# moment. The frontend's avatar already tracks a mood; the chat turn takes
# it as context so the words match the face.
MOOD_DIRECTIVES: dict[str, str] = {
    "excited": "You're buzzing with excitement right now — let it show: "
    "extra energy, a big grin in your words, ready to sprint.",
    "happy": "You're in a great mood — warm, cheerful, generous with "
    "encouragement.",
    "thinking": "You're focused and heads-down — still upbeat, but a touch "
    "more measured and precise while you work through it.",
    "worried": "Something's off and you're on it — reassuring and steady, "
    "energy channeled into fixing it, never panicked.",
    "calm": "You're relaxed and easygoing — friendly, unhurried, present.",
}


def mood_directive(mood: str | None) -> str | None:
    """A one-line system note tuning OoLu's voice to its current mood."""
    if not mood:
        return None
    return MOOD_DIRECTIVES.get(mood.strip().lower())


# Measurement system: the user's reply should speak the units they think in.
# A preference wins outright; "auto" reads the account's own regional signal —
# its spending currency, a stored per-tenant setting BOTH the chat assistant
# and the representative read, so "auto" resolves identically on every surface
# (no dependence on a transient browser header). Only the imperial holdouts'
# currencies get imperial; everyone else gets SI, the international default.
METRIC_UNITS_NOTE = (
    "Use metric / SI units in every reply — metres and kilometres, grams and "
    "kilograms, litres, °C. If a source is imperial, convert it (you may keep "
    "the original in parentheses)."
)
IMPERIAL_UNITS_NOTE = (
    "Use US customary / imperial units in every reply — feet and miles, "
    "ounces and pounds, gallons, °F. If a source is metric, convert it (you "
    "may keep the original in parentheses)."
)
# The currencies of the countries whose everyday system is US-customary /
# imperial (US dollar, Liberian dollar, Myanmar kyat). A metric account that
# happens to spend in USD (e.g. Ecuador) can still choose "metric" outright.
IMPERIAL_CURRENCIES = frozenset({"USD", "LRD", "MMK"})


def units_directive(
    preference: str | None, *, currency: str | None = None
) -> str | None:
    """A one-line system note fixing the reply's measurement system.

    ``imperial``/``metric`` are honoured outright; ``auto`` (or anything
    unrecognised) resolves from the account's ``currency`` — imperial for the
    US/Liberia/Myanmar currencies, SI everywhere else (and when unknown)."""
    choice = (preference or "auto").strip().lower()
    if choice == "imperial":
        return IMPERIAL_UNITS_NOTE
    if choice == "metric":
        return METRIC_UNITS_NOTE
    # auto: the account's spending currency is its stored regional signal.
    if (currency or "").strip().upper() in IMPERIAL_CURRENCIES:
        return IMPERIAL_UNITS_NOTE
    return METRIC_UNITS_NOTE


# What the user hears when a message becomes work — energetic, and it
# varies so it never sounds like a canned recording.
ACK = "On it! 🚀 I'll ping you the second it's done or I need a hand."

# A rotating set the frontend/model can draw from; the run card also
# varies its own status line. Index chosen by run id so it's stable per run.
ACK_VARIANTS = (
    "On it! 🚀 I'll ping you the second it's done or I need a hand.",
    "Love it — diving in now! I'll shout when it's ready.",
    "Consider it handled. ⚡ I'll let you know the moment it lands.",
    "Got it! Rolling up my sleeves — back in a flash with the result.",
)


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
    # OoLu ASKING for one of this device's senses ("location" | "camera"
    # | "file"): the app renders the request as a grant button — the user
    # decides, and only a grant runs the sense.
    device: str | None = None
    # Text OoLu is putting on the user's clipboard at their request (e.g. a
    # node ID they asked to copy). The client writes it to the clipboard; the
    # value never has to be shown on screen.
    copy: str | None = None
    # The model's own thinking, when it showed it (reasoning models emit a
    # <think> block before the answer). Split off so the spoken turn stays
    # clean; the UI shows it dimmed, as proof the assistant is working.
    reasoning: str | None = None


@dataclass(frozen=True)
class _ToolCall:
    name: str
    args: dict


# The model may use at most this many tools per turn; then it must speak.
MAX_TOOL_ROUNDS = 4

# Reasoning models (qwen3 and friends) prefix the answer with their
# monologue. It is split off — never spoken, never parsed as the turn.
_THINK_RE = re.compile(r"<think>(.*?)(?:</think>|\Z)", re.S | re.I)


def _split_reasoning(raw: str) -> tuple[str, str | None]:
    """(clean reply, the model's thinking or None).

    The <think> block must come off BEFORE turn parsing: it is prose, so
    leaving it in would leak the monologue into the spoken reply — and a
    brace inside it could even be mistaken for the turn's JSON. An
    unclosed block (the model ran out of budget mid-thought) still counts
    as thinking, with everything after the tag treated as monologue."""
    thoughts = [m.group(1).strip() for m in _THINK_RE.finditer(raw)]
    cleaned = _THINK_RE.sub("", raw).strip()
    reasoning = "\n\n".join(t for t in thoughts if t) or None
    return cleaned, reasoning


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
    # A device request rides the same JSON: only the three named senses
    # count — anything else the model invents is dropped, never granted.
    device = data.get("device")
    device = (
        device.strip().lower()
        if isinstance(device, str)
        and device.strip().lower() in {"location", "camera", "file"}
        else None
    )
    # A "copy" request rides the same JSON: a short string OoLu puts on the
    # user's clipboard (a node ID they asked for). Bounded so it can't be
    # abused to shove a wall of text onto the clipboard.
    copy = data.get("copy")
    copy = copy.strip() if isinstance(copy, str) and 0 < len(copy.strip()) <= 200 else None
    return ChatTurn(
        say=say or (ACK if task else "…"), task=task, device=device, copy=copy
    )


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


# --------------------------------------------------------------------------- #
# OoLu's outbox: messages to friends and nodes, on the user's behalf.          #
# --------------------------------------------------------------------------- #
# The user names the destination in their own words; OoLu resolves the best
# compatible target (name match, tie-broken by the user's own habits) and
# the BACKEND delivers to the exact id — a friend gets a real server
# message, a node gets a document in its own drawer. Every delivery is
# marked as forwarded via OoLu from the user: OoLu carries the words, it
# never impersonates.
VIA_OOLU_MARK = "↪ forwarded via OoLu from"


@runtime_checkable
class MessagingTools(Protocol):
    """Where a chat turn may send words, and how they are delivered."""

    def message_targets(self) -> list[dict]: ...
    def deliver_message(self, kind: str, target_id: str, text: str) -> str: ...


def resolve_message_target(
    targets: list[dict],
    wanted: str,
    *,
    exact_lookup=None,  # (name) -> dict | None: reach past the listing
) -> list[dict]:
    """The best compatible destination for a name the user typed.

    Exact name first; else substring; else every-word match. Ties break on
    HABIT — who the user actually talks to (bigger = more recent/frequent)
    — and only a CLEAR winner is chosen: equals stay ambiguous so the
    caller asks instead of guessing. ``exact_lookup`` reaches accounts the
    target listing deliberately omits (a host is never a directory): an
    exact username still resolves."""
    wanted_cf = re.sub(r"\s+", " ", (wanted or "").strip().casefold())
    if not wanted_cf:
        return []

    def named(target: dict) -> str:
        return str(target.get("name", "")).casefold()

    pool = [t for t in targets if named(t) == wanted_cf]
    if not pool:
        pool = [t for t in targets if wanted_cf in named(t)]
    if not pool:
        words = wanted_cf.split()
        pool = [t for t in targets if all(w in named(t) for w in words)]
    if not pool and exact_lookup is not None:
        found = exact_lookup(wanted.strip())
        return [found] if found else []
    if len(pool) <= 1:
        return pool
    pool = sorted(pool, key=lambda t: -float(t.get("habit", 0.0)))
    if float(pool[0].get("habit", 0.0)) > float(pool[1].get("habit", 0.0)):
        return [pool[0]]
    return pool


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
        accounts=None,  # identity.LocalAccountService: exact-name friends
        direct_messages=None,  # social.DirectMessageStore: real deliveries
        # The DESKTOP's own machine, when this gateway runs on it (the
        # `oolu desktop` loopback). A multi-user host never sets this —
        # a server has no business in anyone's home directory.
        local_root=None,  # pathlib.Path | None
    ):
        super().__init__(store, tenant=tenant)
        self._chat_tenant = tenant
        self._principal = principal
        self._durable = durable
        self._desk = desk
        self._settings = settings
        self._accounts = accounts
        self._direct_messages = direct_messages
        self._local_root = local_root

    def local_search_enabled(self) -> bool:
        return self._local_root is not None

    def search_local_files(self, pattern: str) -> list[dict]:
        """Find files on THIS computer by name or glob — Edge's own disk.

        Listing only (path + size), never content: finding a file and
        reading it are different trust levels. Bounded walk: hidden and
        bulky tool directories are skipped, the scan stops after a cap,
        and at most 40 matches return."""
        if self._local_root is None:
            return []
        import fnmatch
        import os

        wanted = str(pattern or "").strip()
        if not wanted:
            return []
        needle = wanted.casefold()
        is_glob = any(ch in wanted for ch in "*?[")
        skip = {
            ".git", "node_modules", ".cache", "__pycache__", ".venv",
            "venv", "AppData", "Library", ".Trash", ".oolu",
        }
        matches: list[dict] = []
        scanned = 0
        for dirpath, dirnames, filenames in os.walk(self._local_root):
            dirnames[:] = [
                d for d in dirnames if d not in skip and not d.startswith(".")
            ]
            for name in filenames:
                scanned += 1
                if scanned > 50_000:
                    return matches
                hit = (
                    fnmatch.fnmatch(name.casefold(), needle)
                    if is_glob
                    else needle in name.casefold()
                )
                if not hit:
                    continue
                path = os.path.join(dirpath, name)
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = 0
                matches.append(
                    {
                        "path": os.path.relpath(path, self._local_root),
                        "size": size,
                    }
                )
                if len(matches) >= 40:
                    return matches
        return matches

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

    def exact_friend(self, name: str) -> dict | None:
        """An account by EXACT username — the one reach past the target
        listing, mirroring the friends surface: a host is never a
        directory, but a name you already know still resolves."""
        if self._accounts is None:
            return None
        account = self._accounts.user(str(name or "").strip())
        if (
            account is None
            or account.tenant_id != self._chat_tenant
            or account.disabled
            or account.username == self._principal
        ):
            return None
        return {
            "kind": "friend",
            "id": account.username,
            "name": account.username,
            "habit": 0.0,
        }

    def message_targets(self) -> list[dict]:
        """Where a message can go from here: the friends the user actually
        talks to — habit is how recent the conversation, the user's own
        behaviour as the tiebreak — and the nodes on their desk. Never a
        roster of strangers; an exact username resolves via exact_friend."""
        targets: list[dict] = []
        if self._direct_messages is not None:
            convos = self._direct_messages.conversations(
                tenant=self._chat_tenant, principal=self._principal
            )
            total = len(convos)
            for rank, convo in enumerate(convos):
                targets.append(
                    {
                        "kind": "friend",
                        "id": convo["peer"],
                        "name": convo["peer"],
                        "habit": float(total - rank),
                    }
                )
        if self._desk is not None:
            for entry in self._desk.overview(
                principal=self._principal, tenant=self._chat_tenant
            ):
                targets.append(
                    {
                        "kind": "node",
                        "id": entry.node_id,
                        "name": entry.title,
                        "habit": 0.0,
                    }
                )
        return targets

    def deliver_message(self, kind: str, target_id: str, text: str) -> str:
        """Exact-ID delivery behind the name resolution: a friend gets a
        real server message, a node gets a document in its own drawer —
        and both arrive marked as forwarded via OoLu from the user, so
        the recipient always sees WHO sent it. OoLu never impersonates."""
        text = (text or "").strip()
        if not text:
            return "error: the message needs words"
        reachable = any(
            t["kind"] == kind and t["id"] == target_id
            for t in self.message_targets()
        )
        if not reachable and not (
            kind == "friend" and self.exact_friend(target_id) is not None
        ):
            return "error: that isn't a destination you can reach from here"
        if kind == "friend":
            if self._direct_messages is None:
                return (
                    "error: friends live on a server — OoLu Global, or "
                    "your own private network server"
                )
            try:
                self._direct_messages.send(
                    tenant=self._chat_tenant,
                    sender=self._principal,
                    recipient=target_id,
                    body=f"{VIA_OOLU_MARK} {self._principal}:\n{text}",
                )
            except ValueError as exc:
                return f"error: {exc}"
            return f"sent to {target_id}"
        if kind == "node":
            from uuid import uuid4

            from .naming import concise_name

            name = (
                f"{(concise_name(text) or 'message').lower()}"
                f"-{uuid4().hex[:6]}.md"
            )
            try:
                saved = self._store.save(
                    UserFile(
                        tenant_id=self._chat_tenant,
                        node_id=target_id,
                        name=name,
                        folder="messages",
                        content=f"> {VIA_OOLU_MARK} {self._principal}\n\n{text}",
                    )
                )
            except FileTooLargeError as exc:
                return f"error: {exc}"
            return f"delivered to the node's drawer as “{saved.name}”"
        return "error: unknown destination kind"

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


# --------------------------------------------------------------------------- #
# Node creation: a node IS its function.                                       #
# --------------------------------------------------------------------------- #
# The key thing about creating a node is creating its own function for the
# task — an empty shell called by the global workflow machinery is not a
# node. So building takes two verified steps in ONE model consultation:
# first the sentence must be judged executable work (not conversation),
# then the model must actually WRITE the node's execution function. Either
# gate failing means nothing is created.
NODE_FUNCTION_PROMPT = """\
You are the function writer for OoLu nodes. A node is published only WITH
its own execution function — an empty node is unnecessary.

First decide: does the request describe executable work (fetching,
converting, computing, organizing, automating — something a program can
DO)? If it is conversation, a greeting, or a question to answer in words,
reply with exactly:
NO_TASK

Otherwise write the node's execution function:
1. A short numbered plan (one step per line).
2. ONE line starting with IO: declaring the node's interface as JSON —
   what it consumes and what it produces, so nodes chain reliably on a
   route. Types are str, path, or number:
   IO: {"inputs": [{"name": "...", "type": "str"}], "outputs": [{"name": "result", "type": "str"}]}
3. ONE complete, self-contained Python script in a single fenced
   ```python block that performs the whole task in one run. The script
   MUST import and call emit_result exactly once with its final answer:
       from _oolu_runtime import emit_result
   Missing third-party packages install automatically; the sandbox has NO
   network and NO host credentials at run time."""


_IO_LINE_RE = re.compile(r"^\s*IO:\s*(\{.*\})\s*$", re.M)
_IO_TYPES = {"str", "path", "number"}


def parse_node_io(raw: str) -> dict:
    """The declared interface from the model's IO: line — normalized to
    ``{"inputs": [...], "outputs": [...]}`` with only the fields the slot
    vocabulary knows. A missing or broken declaration degrades to the
    honest default: no inputs, one string result."""
    default = {"inputs": [], "outputs": [{"name": "result", "type": "str"}]}
    match = _IO_LINE_RE.search(raw or "")
    if not match:
        return default
    try:
        declared = json.loads(match.group(1))
    except ValueError:
        return default
    def clean(items):
        out = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            kind = str(item.get("type", "str")).strip().lower()
            out.append(
                {"name": name, "type": kind if kind in _IO_TYPES else "str"}
            )
        return out
    inputs = clean(declared.get("inputs"))
    outputs = clean(declared.get("outputs")) or default["outputs"]
    return {"inputs": inputs, "outputs": outputs}

_CHAT_SHAPED = frozenset(
    phrase for rule in DEFAULT_RULES for phrase in rule.phrases
)


def obviously_chat(goal: str) -> bool:
    """The cheap pre-filter: greetings, thanks, and questions are
    conversation, never a node — no model needed to refuse them."""
    text = (goal or "").strip()
    if not text:
        return True
    if text.endswith("?"):
        return True
    return text.casefold().rstrip(".!?") in _CHAT_SHAPED


def author_node_function(
    model: ChatModel, goal: str
) -> tuple[str | None, dict, str]:
    """``(script, io, refusal_reason)`` — the creation gates in one call.

    ``script`` is the node's own execution function, or None with the
    exact reason nothing was built: the sentence is conversation, the
    model wrote no usable code, or the model could not be reached.
    ``io`` is the declared interface (inputs/outputs) that makes the
    node chainable on a route — defaulted when the model omits it.
    """
    from .routing.gateway import extract_script

    try:
        raw = model.reply(
            [
                {"role": "system", "content": NODE_FUNCTION_PROMPT},
                {"role": "user", "content": goal},
            ]
        )
    except Exception as exc:  # noqa: BLE001 - a dead model builds nothing
        return (
            None,
            {},
            f"the model could not be reached to write the function: {exc}",
        )
    if "NO_TASK" in raw.strip().upper()[:40]:
        return None, {}, (
            "that reads as conversation, not an executable task — a node "
            "is its function, so there is nothing to build"
        )
    script = extract_script(raw)
    if not script:
        return None, {}, (
            "the model wrote no usable function, so nothing was built — "
            "an empty node is unnecessary"
        )
    return script, parse_node_io(raw), ""


class NodeChatTools(GatewayChatTools):
    """The gateway tools plus one node's own desk, bound by injected hands.

    The gateway supplies the callables, so every wall it already enforces —
    tenant scope, approve authority, the budget re-check, the audit trail,
    the auto-build consent — applies unchanged; this class only holds the
    node the conversation is standing in.
    """

    def __init__(
        self,
        store: UserFileStore,
        *,
        tenant: str,
        principal: str = "",
        durable=None,
        desk=None,
        settings=None,
        accounts=None,
        direct_messages=None,
        node: dict,
        holds_list,  # () -> list[dict]
        holds_decide,  # (pending_id, approved, signature) -> str
        holds_reply,  # (pending_id, message) -> str
        builder,  # (goal) -> str
    ):
        super().__init__(
            store,
            tenant=tenant,
            principal=principal,
            durable=durable,
            desk=desk,
            settings=settings,
            accounts=accounts,
            direct_messages=direct_messages,
        )
        self._node = dict(node)
        self._holds_list = holds_list
        self._holds_decide = holds_decide
        self._holds_reply = holds_reply
        self._builder = builder

    def list_files(self) -> list[UserFile]:
        """The interact window's file hands reach THIS NODE's drawer —
        the inbox where the route's previous node (or a user) delivered
        the work — never the Life drawer: the operator processes what
        arrived HERE and passes the results onward."""
        return self._store.list(
            tenant=self._chat_tenant,
            node_id=str(self._node.get("node_id") or "") or None,
        )

    def write_file(self, name: str, content: str) -> UserFile:
        matches = self.resolve(name)
        if len(matches) == 1:
            updated = matches[0].model_copy(update={"content": content})
            return self._store.save(updated)
        return self._store.save(
            UserFile(
                tenant_id=self._chat_tenant,
                node_id=str(self._node.get("node_id") or "") or None,
                name=name.strip(),
                content=content,
            )
        )

    def message_targets(self) -> list[dict]:
        """The gateway targets plus this node's own org: from a node's
        interact window, the nodes under the SAME Supernode are reachable
        too — colleagues on the fleet, not strangers."""
        targets = super().message_targets()
        node_id = str(self._node.get("node_id") or "")
        if self._desk is None or not node_id:
            return targets
        seen = {(t["kind"], t["id"]) for t in targets}
        for member in self._desk.siblings(node_id, tenant=self._chat_tenant):
            key = ("node", member["node_id"])
            if key in seen:
                continue
            targets.append(
                {
                    "kind": "node",
                    "id": member["node_id"],
                    "name": member["title"],
                    "habit": 0.0,
                }
            )
        return targets

    def node_context(self) -> dict:
        return dict(self._node)

    def node_holds(self) -> list[dict]:
        return self._holds_list()

    def decide_hold(
        self, pending_id: str, approved: bool, signature: str = ""
    ) -> str:
        return self._holds_decide(pending_id, approved, signature)

    def reply_hold(self, pending_id: str, message: str) -> str:
        return self._holds_reply(pending_id, message)

    def build_node(self, goal: str) -> str:
        return self._builder(goal)


@runtime_checkable
class NodeTools(Protocol):
    """The assistant's hands INSIDE one node's thread (the Work interact
    window): the held-request desk and consented node building. Every
    method returns words — an ``error: …`` prefix means refusal, and the
    tool loop hands the string straight back to the model or the user."""

    def node_context(self) -> dict: ...
    def node_holds(self) -> list[dict]: ...
    def decide_hold(
        self, pending_id: str, approved: bool, signature: str = ""
    ) -> str: ...
    def reply_hold(self, pending_id: str, message: str) -> str: ...
    def build_node(self, goal: str) -> str: ...


def _resolve_hold(holds: list[dict], ref: str) -> list[dict]:
    """A held request by id, id prefix, or name substring — never a guess."""
    wanted = ref.strip().casefold()
    if not wanted:
        return []
    exact = [h for h in holds if h["pending_id"].casefold() == wanted]
    if exact:
        return exact
    prefix = [h for h in holds if h["pending_id"].casefold().startswith(wanted)]
    if len(prefix) == 1:
        return prefix
    by_name = [h for h in holds if wanted in str(h.get("name", "")).casefold()]
    if by_name:
        return by_name
    return prefix


def _speak_hold(hold: dict) -> str:
    return (
        f"• {hold.get('name', 'contract')} — from"
        f" {hold.get('submitted_by') or 'unknown'} ({hold['pending_id'][:8]})"
    )


_SIGN_ALL_RE = re.compile(r"^sign\s+all\s+as\s+(.+?)\s*$", re.I)
_SIGN_RE = re.compile(r"^sign\s+(.+?)\s+as\s+(.+?)\s*$", re.I)
_DECIDE_RE = re.compile(r"^(allow|approve|reject|decline)\s+(.+?)\s*$", re.I)
_HOLD_REPLY_RE = re.compile(r"^reply\s+([^:]+):\s*(.+)$", re.I | re.S)
_BUILD_RE = re.compile(r"^build\s+(?:a\s+node\s+(?:for|to)\s+)?(.+?)\s*$", re.I)
_PENDING_PHRASES = frozenset(
    {"pending", "holds", "pending requests", "show pending", "what is pending",
     "what's pending"}
)


def _node_command(text: str, tools: "NodeTools") -> ChatTurn | None:
    """Deterministic node-desk commands for the interact window.

    The manual floor of the automation vision: listing, allowing,
    signing (single or ALL — the fast path for final-result audit
    signing), replying, and consented building all work with no model."""
    lowered = text.casefold().rstrip(".!?")

    if lowered in _PENDING_PHRASES or lowered == "accelerate":
        # "accelerate" still answers when typed, but it is not a button:
        # everything that can move automatically already moved — what's
        # listed here is exactly the work that waits on a human.
        holds = tools.node_holds()
        if not holds:
            say = (
                "Nothing is waiting on this node right now — everything "
                "that could move has moved."
            )
            if lowered == "accelerate":
                say += (
                    " To speed the node up further: run more tasks through "
                    "it — every verified run raises its automation "
                    "reliability — or say “build <what's missing>” and I'll "
                    "put a new execution node on its path."
                )
            return ChatTurn(
                say=say, source="tool", actions=[{"tool": "node_holds"}]
            )
        listing = "\n".join(_speak_hold(h) for h in holds)
        say = (
            f"Waiting on you:\n{listing}\n"
            "Sign one onward with “sign <task id> as <your name>” — the "
            "task id is in the parentheses — and it passes to the next "
            "node. “sign all as <your name>” clears everything; “allow” / "
            "“reject <task id>” decides without signing; “reply <task id>: "
            "<message>” talks back first."
        )
        return ChatTurn(say=say, source="tool", actions=[{"tool": "node_holds"}])

    sign_all = _SIGN_ALL_RE.match(text)
    if sign_all:
        holds = tools.node_holds()
        if not holds:
            return ChatTurn(
                say="Nothing is pending — there is nothing to sign.",
                source="tool",
            )
        signature = sign_all.group(1).strip()
        outcomes, actions = [], []
        for hold in holds:
            result = tools.decide_hold(hold["pending_id"], True, signature)
            if result.startswith("error:"):
                outcomes.append(f"• {hold.get('name')}: {result[7:].strip()}")
            else:
                outcomes.append(f"• {hold.get('name')}: signed and allowed")
                actions.append(
                    {"tool": "decide_hold", "name": hold["pending_id"][:8]}
                )
        return ChatTurn(
            say="Signed as " + signature + ":\n" + "\n".join(outcomes),
            source="tool",
            actions=actions,
        )

    sign_one = _SIGN_RE.match(text)
    if sign_one:
        matches = _resolve_hold(tools.node_holds(), sign_one.group(1))
        if len(matches) == 1:
            result = tools.decide_hold(
                matches[0]["pending_id"], True, sign_one.group(2).strip()
            )
            if result.startswith("error:"):
                return ChatTurn(
                    say=f"I couldn't: {result[7:].strip()}", source="tool"
                )
            return ChatTurn(
                say=f"Signed and allowed {matches[0].get('name')}.",
                source="tool",
                actions=[
                    {"tool": "decide_hold", "name": matches[0]["pending_id"][:8]}
                ],
            )
        if len(matches) > 1:
            return ChatTurn(
                say="Which one: "
                + "; ".join(_speak_hold(h) for h in matches[:6]),
                source="tool",
            )
        return ChatTurn(
            say=f"No held request matches “{sign_one.group(1).strip()}”.",
            source="tool",
        )

    decide = _DECIDE_RE.match(text)
    if decide:
        approved = decide.group(1).casefold() in {"allow", "approve"}
        matches = _resolve_hold(tools.node_holds(), decide.group(2))
        if len(matches) == 1:
            result = tools.decide_hold(matches[0]["pending_id"], approved)
            if result.startswith("error:"):
                return ChatTurn(
                    say=f"I couldn't: {result[7:].strip()}", source="tool"
                )
            verdict = "Allowed" if approved else "Rejected"
            return ChatTurn(
                say=f"{verdict} {matches[0].get('name')}.",
                source="tool",
                actions=[
                    {"tool": "decide_hold", "name": matches[0]["pending_id"][:8]}
                ],
            )
        if len(matches) > 1:
            return ChatTurn(
                say="Which one: "
                + "; ".join(_speak_hold(h) for h in matches[:6]),
                source="tool",
            )
        # Nothing pending by that name: probably not a desk command.
        return None

    hold_reply = _HOLD_REPLY_RE.match(text)
    if hold_reply:
        matches = _resolve_hold(tools.node_holds(), hold_reply.group(1))
        if len(matches) == 1:
            result = tools.reply_hold(
                matches[0]["pending_id"], hold_reply.group(2).strip()
            )
            if result.startswith("error:"):
                return ChatTurn(
                    say=f"I couldn't: {result[7:].strip()}", source="tool"
                )
            return ChatTurn(
                say=f"Reply sent on {matches[0].get('name')}.",
                source="tool",
                actions=[
                    {"tool": "reply_hold", "name": matches[0]["pending_id"][:8]}
                ],
            )
        if len(matches) > 1:
            return ChatTurn(
                say="Which one: "
                + "; ".join(_speak_hold(h) for h in matches[:6]),
                source="tool",
            )
        return None

    build = _BUILD_RE.match(text)
    if build:
        result = tools.build_node(build.group(1).strip())
        if result.startswith("error:"):
            return ChatTurn(
                say=f"I couldn't: {result[7:].strip()}", source="tool"
            )
        return ChatTurn(
            say=result, source="tool", actions=[{"tool": "build_node"}]
        )
    return None


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


# --------------------------------------------------------------------------- #
# The growth trigger: a failure that asks, instead of a wall that repeats.      #
# --------------------------------------------------------------------------- #
# Borrowed from n8n's editor: when a workflow is missing the node it needs,
# the answer is a proposal to ADD that node — not the same refusal again.
# When a chat task fails for want of a working function, the gateway records
# a standing offer and appends this question; the user's plain "yes" on the
# very next message IS the consent (scoped to this one goal, one build) —
# no trip to Settings required. Anything that isn't a yes or a no withdraws
# the offer: consent detached from the question it answered is not consent.
GROWTH_OFFER = (
    " I can grow that missing piece myself: say “yes” and I'll build the "
    "“{name}” node for “{goal}” — with its own written-and-verified function "
    "— then run it. Say “no” to leave things as they are."
)

# The reuse-first twin guard: when a node already answers for NEARLY this
# goal (the same work, said differently), the offer is to run THAT node —
# one node, one history — never to silently mint a twin. A "no" is
# answered with the plain build offer, so genuinely different work still
# gets its own node, with the user's consent.
GROWTH_REUSE_OFFER = (
    " You already have a node that answers for nearly this — “{title}”, "
    "built for “{existing}”. Say “yes” and I'll run it for this, so the "
    "execution lands in its one log instead of minting a twin. Say “no” "
    "if this is different work."
)

GROWTH_BUILD_INSTEAD = (
    "Okay — different work, then. Say “yes” and I'll build a separate "
    "“{name}” node for “{goal}” with its own written-and-verified function, "
    "then run it. Say “no” to leave things as they are."
)

_CONSENT_YES = frozenset(
    {
        "yes",
        "yes please",
        "yes build it",
        "yes do it",
        "yes go ahead",
        "build it",
        "do it",
        "go ahead",
        "go for it",
        "sure",
        "ok",
        "okay",
        "yep",
        "yeah",
        "please do",
    }
)
_CONSENT_NO = frozenset(
    {
        "no",
        "no thanks",
        "no thank you",
        "nope",
        "nah",
        "not now",
        "don't",
        "do not",
        "leave it",
        "skip it",
        "cancel",
        "stop",
    }
)


def consent_answer(text: str) -> str | None:
    """``"yes"``, ``"no"``, or None for a standing growth offer.

    Narrow on purpose: only an unmistakable yes or no counts, so an
    unrelated message never spends consent the user did not give."""
    normal = (text or "").strip().casefold().replace(",", " ")
    normal = re.sub(r"\s+", " ", normal).rstrip(".!?").strip()
    if normal in _CONSENT_YES:
        return "yes"
    if normal in _CONSENT_NO:
        return "no"
    return None


# Appended to the model's context when the active router really can search
# (an Anthropic path with model.web_search on). Without it a keyed install
# answers "I can't browse the internet" — or worse, hands the search to the
# engine, whose sandbox has no network, so the task can only ever fail.
WEB_SEARCH_NOTE = (
    "You HAVE live web search in this conversation — it runs inside your own "
    "reply, on the provider's servers. Questions about current facts (news, "
    "weather, prices, scores, anything on today's web) you answer DIRECTLY "
    'in "say", searching as needed. Never set "task" for a web search or '
    "lookup: the engine's sandbox has no network access, so a searching task "
    "can only fail."
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

    if isinstance(tools, NodeTools):
        node = _node_command(text, tools)
        if node is not None:
            return node

    if isinstance(tools, MessagingTools):
        message = _message_command(text, tools)
        if message is not None:
            return message

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


# "send <words> to <name>" / "message <name>: <words>" — the greedy first
# group means the LAST " to " splits, so a message containing "to" still
# reaches the right person ("send the go to market plan to bob").
_SEND_TO_RE = re.compile(r"^(?:send|forward)\s+(.+)\s+to\s+([^:\n]{1,60})$", re.I | re.S)
_TELL_RE = re.compile(r"^(?:message|tell)\s+([^:\n]{1,60}):\s*(.+)$", re.I | re.S)


def _message_command(text: str, tools: "MessagingTools") -> ChatTurn | None:
    """Deterministic sending for the interact windows and model-less
    installs: the user names the destination, resolution finds the best
    compatible target (habits break ties), and delivery is by exact id.
    A name nothing matches falls through — it probably wasn't a message
    command at all."""
    stripped = text.strip()
    send = _SEND_TO_RE.match(stripped)
    tell = _TELL_RE.match(stripped)
    if send:
        body, wanted = send.group(1).strip(), send.group(2).strip()
    elif tell:
        wanted, body = tell.group(1).strip(), tell.group(2).strip()
    else:
        return None
    matches = resolve_message_target(
        tools.message_targets(),
        wanted,
        exact_lookup=getattr(tools, "exact_friend", None),
    )
    if not matches:
        return None
    if len(matches) > 1:
        names = "; ".join(f"{t['name']} ({t['kind']})" for t in matches[:6])
        return ChatTurn(say=f"Which one do you mean: {names}?", source="tool")
    target = matches[0]
    result = tools.deliver_message(target["kind"], str(target["id"]), body)
    if result.startswith("error:"):
        return ChatTurn(say=f"I couldn't: {result[7:].strip()}", source="tool")
    return ChatTurn(
        say=f"Sent to {target['name']} — marked as forwarded via OoLu from you.",
        source="tool",
        actions=[{"tool": "send_message", "name": str(target["name"])}],
    )


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
    if call.name == "find_local_files":
        search = getattr(tools, "search_local_files", None)
        enabled = getattr(tools, "local_search_enabled", None)
        if search is None or enabled is None or not enabled():
            return (
                "error: local file search lives on the desktop app — this"
                " host has no access to your computer's files",
                None,
            )
        results = search(str(call.args.get("pattern", "")))
        listing = (
            "\n".join(f"{r['path']} ({r['size']} bytes)" for r in results)
            or "(no matching files on this computer)"
        )
        return listing, {"tool": "find_local_files"}
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
    if call.name == "node_holds" and isinstance(tools, NodeTools):
        holds = tools.node_holds()
        listing = (
            "\n".join(
                f"{h['pending_id'][:8]} {h.get('name')} — from"
                f" {h.get('submitted_by') or 'unknown'}"
                for h in holds
            )
            or "(none)"
        )
        return listing, {"tool": "node_holds"}
    if call.name == "decide_hold" and isinstance(tools, NodeTools):
        pending_id = str(call.args.get("pending_id", ""))
        result = tools.decide_hold(
            pending_id,
            bool(call.args.get("approved", False)),
            str(call.args.get("signature", "") or ""),
        )
        action = None if result.startswith("error:") else {
            "tool": "decide_hold",
            "name": pending_id[:8],
        }
        return result, action
    if call.name == "reply_hold" and isinstance(tools, NodeTools):
        pending_id = str(call.args.get("pending_id", ""))
        result = tools.reply_hold(
            pending_id, str(call.args.get("message", "")).strip()
        )
        action = None if result.startswith("error:") else {
            "tool": "reply_hold",
            "name": pending_id[:8],
        }
        return result, action
    if call.name == "build_node" and isinstance(tools, NodeTools):
        result = tools.build_node(str(call.args.get("goal", "")).strip())
        action = None if result.startswith("error:") else {"tool": "build_node"}
        return result, action
    if call.name == "send_message" and isinstance(tools, MessagingTools):
        wanted = str(call.args.get("to", "")).strip()
        matches = resolve_message_target(
            tools.message_targets(),
            wanted,
            exact_lookup=getattr(tools, "exact_friend", None),
        )
        if not matches:
            return f"error: no friend or node here matches '{wanted}'", None
        if len(matches) > 1:
            names = "; ".join(str(t["name"]) for t in matches[:6])
            return (
                f"error: ambiguous — could be {names}; ask the user which",
                None,
            )
        result = tools.deliver_message(
            matches[0]["kind"],
            str(matches[0]["id"]),
            str(call.args.get("text", "")),
        )
        action = None if result.startswith("error:") else {
            "tool": "send_message",
            "name": str(matches[0]["name"]),
        }
        return result, action
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
        context: str | None = None,
    ) -> ChatTurn:
        """``context`` scopes the turn (e.g. one node's interact window):
        an extra system note for the model describing where the assistant
        is standing and which extra tools apply there."""
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
                return self._model_turn(
                    active, message, history, tools, context=context
                )
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
        *,
        context: str | None = None,
    ) -> ChatTurn:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        if context:
            messages.append({"role": "system", "content": context})
        for entry in history or []:
            role = entry.get("role")
            content = entry.get("content")
            if role in ("user", "assistant") and isinstance(content, str):
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": message})
        actions: list[dict] = []
        thoughts: list[str] = []
        for _ in range(MAX_TOOL_ROUNDS):
            raw = model.reply(messages)
            # Thinking accumulates across tool rounds — the whole chain of
            # thought behind the final answer, not just its last step.
            cleaned, reasoning = _split_reasoning(raw)
            if reasoning:
                thoughts.append(reasoning)
            parsed = _parse_model_reply(cleaned)
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
                messages.append({"role": "assistant", "content": cleaned})
                messages.append(
                    {"role": "user", "content": f"[tool result]\n{result}"}
                )
                continue
            return ChatTurn(
                say=parsed.say,
                task=parsed.task,
                source="model",
                actions=actions,
                device=parsed.device,
                copy=parsed.copy,
                reasoning="\n\n".join(thoughts) or None,
            )
        return ChatTurn(
            say="I got tangled up in my tools — tell me exactly what you need.",
            source="model",
            actions=actions,
            reasoning="\n\n".join(thoughts) or None,
        )

    # ------------------------------------------------------------------ #
    # Streaming: the reasoning arrives live, the turn lands complete.     #
    # ------------------------------------------------------------------ #
    def respond_streaming(
        self,
        message: str,
        *,
        history: list[dict] | None = None,
        sender: str = "user",
        tools: ChatTools | None = None,
        model: ChatModel | None = None,
        context: str | None = None,
        on_reasoning: "Callable[[str], None] | None" = None,
    ) -> ChatTurn:
        """Same turn as :meth:`respond`, but the model's ⟨think⟩ monologue is
        streamed to ``on_reasoning`` as it is generated (when the model can
        stream). The authoritative ChatTurn is still built from the complete
        text, so say/task/tool routing is unchanged — only the reasoning is
        revealed live. Deterministic replies (rules, file commands) stream
        nothing and just return their turn."""
        final: ChatTurn | None = None
        for event in self.respond_stream(
            message,
            history=history,
            sender=sender,
            tools=tools,
            model=model,
            context=context,
        ):
            if event["type"] == "reasoning":
                if on_reasoning is not None and event["delta"]:
                    on_reasoning(event["delta"])
            elif event["type"] == "turn":
                final = event["turn"]
        assert final is not None  # respond_stream always ends with a turn
        return final

    def respond_stream(
        self,
        message: str,
        *,
        history: list[dict] | None = None,
        sender: str = "user",
        tools: ChatTools | None = None,
        model: ChatModel | None = None,
        context: str | None = None,
    ) -> "Iterator[dict]":
        """Yield ``{"type": "reasoning", "delta": str}`` events as the model
        thinks, then exactly one terminal ``{"type": "turn", "turn": ChatTurn}``.
        Mirrors :meth:`respond`'s decision order; only the model path streams."""
        envelope = MessageEnvelope(
            channel=self._channel,
            conversation_id=sender,
            sender_id=sender,
            text=message,
        )
        decision = self._engine.decide(envelope, context={})
        if decision.source == "rule" and decision.text:
            yield {"type": "turn", "turn": ChatTurn(
                say=decision.text, task=None, source="rule"
            )}
            return
        active = model or self._model
        if active is not None:
            try:
                yield from self._model_turn_stream(
                    active, message, history, tools, context=context
                )
                return
            except ModelBudgetExceeded as exc:
                yield {"type": "turn", "turn": ChatTurn(
                    say=str(exc), task=None, source="model"
                )}
                return
            except ModelUnavailable:
                pass  # fall through to the model-less path
        if tools is not None:
            command = _file_command(message, tools)
            if command is not None:
                yield {"type": "turn", "turn": command}
                return
        yield {"type": "turn", "turn": ChatTurn(
            say=ACK, task=message.strip(), source="intent"
        )}

    def _model_turn_stream(
        self,
        model: ChatModel,
        message: str,
        history: list[dict] | None,
        tools: ChatTools | None,
        *,
        context: str | None = None,
    ) -> "Iterator[dict]":
        """The streaming twin of :meth:`_model_turn`: identical loop and turn
        construction, but each round's ⟨think⟩ content is emitted as it
        arrives. A model without ``reply_stream`` degrades to one blocking
        call per round (no live deltas) — streaming is progressive."""
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        if context:
            messages.append({"role": "system", "content": context})
        for entry in history or []:
            role = entry.get("role")
            content = entry.get("content")
            if role in ("user", "assistant") and isinstance(content, str):
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": message})
        stream = getattr(model, "reply_stream", None)
        actions: list[dict] = []
        thoughts: list[str] = []
        for _ in range(MAX_TOOL_ROUNDS):
            if callable(stream):
                parts: list[str] = []
                emitted = 0
                for delta in stream(messages):
                    parts.append(delta)
                    # Re-extract the reasoning-so-far from the whole
                    # accumulator each chunk, so a ⟨think⟩ tag split across
                    # deltas is still handled correctly, and emit only what
                    # is new.
                    _, so_far = _split_reasoning("".join(parts))
                    so_far = so_far or ""
                    if len(so_far) > emitted:
                        yield {"type": "reasoning", "delta": so_far[emitted:]}
                        emitted = len(so_far)
                raw = "".join(parts)
            else:
                raw = model.reply(messages)
            cleaned, reasoning = _split_reasoning(raw)
            if reasoning:
                thoughts.append(reasoning)
            parsed = _parse_model_reply(cleaned)
            if isinstance(parsed, _ToolCall):
                if tools is None:
                    yield {"type": "turn", "turn": ChatTurn(
                        say="I can't reach any files on this host.",
                        source="model",
                        actions=actions,
                    )}
                    return
                result, action = _run_tool(tools, parsed)
                if action is not None:
                    actions.append(action)
                messages.append({"role": "assistant", "content": cleaned})
                messages.append(
                    {"role": "user", "content": f"[tool result]\n{result}"}
                )
                continue
            yield {"type": "turn", "turn": ChatTurn(
                say=parsed.say,
                task=parsed.task,
                source="model",
                actions=actions,
                device=parsed.device,
                copy=parsed.copy,
                reasoning="\n\n".join(thoughts) or None,
            )}
            return
        yield {"type": "turn", "turn": ChatTurn(
            say="I got tangled up in my tools — tell me exactly what you need.",
            source="model",
            actions=actions,
            reasoning="\n\n".join(thoughts) or None,
        )}
