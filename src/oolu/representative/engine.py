"""The representative engine: drafts in the user's voice, decisions on them.

Transport-free, like the chat assistant — the gateway owns HTTP and
delivery, channels own polling. The engine retrieves how the user has
replied before, asks the model to continue the user's side of the thread,
gates the result, and files a draft. Nothing here ever sends.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Sequence
from uuid import uuid4

from ..chat import ChatModel, ModelUnavailable
from ..replies.models import MessageEnvelope
from .gate import DEFAULT_SIMILARITY_MIN, judge
from .memory import ExchangeMemory, StoreExchangeMemory
from .models import MAX_ABOUT_CHARS, MODES, Draft, PersonaCard
from .persona import build_messages
from .serving import AdapterServer, NoopAdapterServer
from .store import RepresentativeStore

# Autonomy is earned over the trailing window of the user's own verdicts:
# at least this many decisions, and at least this share sent as written.
AUTO_WINDOW = 50
AUTO_MIN_DECIDED = 20
AUTO_ACCEPT_RATE = 0.8


def pair_exchanges(
    turns: Sequence[tuple[str, str, str]], *, me: str
) -> list[tuple[str, str, str]]:
    """Fold a thread into (key, prompt, reply) training/memory pairs.

    ``turns`` is (key, sender, text) in conversation order. A pair is the
    user's FIRST reply after someone else's message — follow-ups without a
    new inbound aren't answers to anything and are skipped.
    """
    pairs: list[tuple[str, str, str]] = []
    inbound: str | None = None
    for key, sender, text in turns:
        text = (text or "").strip()
        if not text:
            continue
        if sender != me:
            inbound = text
        elif inbound is not None:
            pairs.append((key, inbound, text))
            inbound = None
    return pairs


class RepresentativeEngine:
    """One account's representative: settings, memory, drafts, decisions.

    ``model`` is the constructor default; a per-call model (the gateway's
    per-tenant router) outranks it, mirroring the chat assistant's seam.
    """

    def __init__(
        self,
        store: RepresentativeStore,
        *,
        memory: ExchangeMemory | None = None,
        model: ChatModel | None = None,
        adapters: AdapterServer | None = None,
        clock: Callable[[], float] = time.time,
        similarity_min: float = DEFAULT_SIMILARITY_MIN,
        few_shot_k: int = 4,
    ):
        self._store = store
        self._memory = memory or StoreExchangeMemory(store)
        self._model = model
        self._adapters = adapters or NoopAdapterServer()
        self._clock = clock
        self._similarity_min = similarity_min
        self._few_shot_k = few_shot_k

    # -------------------------------------------------------------- #
    # Settings and status.                                            #
    # -------------------------------------------------------------- #
    def mode(self, scope: str) -> str:
        return self._store.mode(scope)

    def configure(
        self, scope: str, *, mode: str | None = None, about: str | None = None
    ) -> dict:
        if mode is not None and mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        if about is not None and len(about) > MAX_ABOUT_CHARS:
            raise ValueError("about is a short standing note, not a biography")
        self._store.configure(scope, mode=mode, about=about)
        return self.status(scope)

    def status(self, scope: str) -> dict:
        outcomes = self._store.outcome_counts(scope)
        decided = sum(
            n
            for status, n in outcomes.items()
            if status in ("sent", "edited", "discarded")
        )
        return {
            "mode": self._store.mode(scope),
            "about": self._store.about(scope),
            "exchanges": self._memory.count(scope),
            "drafts_pending": outcomes.get("pending", 0),
            "drafts_decided": decided,
            "sent_unedited": outcomes.get("sent", 0),
            "auto_sent": outcomes.get("auto_sent", 0),
            "accept_rate": self.accept_rate(scope),
            "auto_earned": self.auto_allowed(scope),
            "adapter": self._adapters.model_for(scope) or "base",
        }

    # -------------------------------------------------------------- #
    # Earned autonomy.                                                #
    # -------------------------------------------------------------- #
    def accept_rate(self, scope: str) -> float | None:
        """Sent-unedited over the trailing window of the user's own
        verdicts; None until there are any."""
        decisions = self._store.recent_decisions(scope, limit=AUTO_WINDOW)
        if not decisions:
            return None
        return round(decisions.count("sent") / len(decisions), 3)

    def auto_allowed(self, scope: str) -> bool:
        """Autonomy is earned, never configured: enough verdicts, and
        almost all of them 'sent as written'."""
        decisions = self._store.recent_decisions(scope, limit=AUTO_WINDOW)
        if len(decisions) < AUTO_MIN_DECIDED:
            return False
        return decisions.count("sent") / len(decisions) >= AUTO_ACCEPT_RATE

    # -------------------------------------------------------------- #
    # Memory.                                                         #
    # -------------------------------------------------------------- #
    def ingest(self, scope: str, exchanges: Iterable[tuple[str, str, str]]) -> int:
        """Upsert (key, prompt, reply) triples into memory; idempotent."""
        count = 0
        for key, prompt, reply in exchanges:
            self._memory.remember(scope, key=key, prompt=prompt, reply=reply)
            count += 1
        return count

    # -------------------------------------------------------------- #
    # Drafting and deciding.                                          #
    # -------------------------------------------------------------- #
    def draft(
        self,
        scope: str,
        *,
        conversation_id: str,
        inbound_text: str,
        display_name: str,
        history: list[dict] | None = None,
        model: ChatModel | None = None,
    ) -> Draft:
        inbound_text = inbound_text.strip()
        if not inbound_text:
            raise ValueError("nothing to reply to")
        # The user's own trained voice outranks every shared model, but a
        # dead adapter server degrades to the per-call router (then the
        # constructor default) — never to a dead conversation. The draft
        # records which voice actually spoke.
        chat_model = getattr(self._adapters, "chat_model", None)
        personal = chat_model(scope) if chat_model is not None else None
        voices: list[tuple[str, ChatModel]] = [
            (label, candidate)
            for label, candidate in (
                (self._adapters.model_for(scope) or "base", personal),
                ("base", model),
                ("base", self._model),
            )
            if candidate is not None
        ]
        if not voices:
            raise ModelUnavailable("no model is configured to draft with")
        hits = self._memory.recall(scope, inbound_text, k=self._few_shot_k)
        card = PersonaCard(display_name=display_name, about=self._store.about(scope))
        messages = build_messages(card, hits, inbound_text, history=history)
        generated = spoke = None
        failure: ModelUnavailable | None = None
        for label, candidate in voices:
            try:
                generated, spoke = candidate.reply(messages).strip(), label
                break
            except ModelUnavailable as exc:
                failure = exc
        if generated is None:
            raise failure or ModelUnavailable("no model answered")
        if not generated:
            raise ModelUnavailable("the model returned an empty draft")
        draft = Draft(
            draft_id=uuid4().hex,
            scope=scope,
            conversation_id=conversation_id,
            inbound_text=inbound_text,
            generated_text=generated,
            gate=judge(generated, hits, similarity_min=self._similarity_min),
            adapter_version=spoke or "base",
            created_at=self._clock(),
        )
        self._store.add_draft(draft)
        return draft

    def auto_reply(
        self,
        scope: str,
        *,
        conversation_id: str,
        inbound_text: str,
        display_name: str,
        history: list[dict] | None = None,
        model: ChatModel | None = None,
    ) -> Draft:
        """Draft, then send only what is both earned and gated: mode
        "auto", a proven accept-rate, a grounded reply, no commitment.
        Anything less stays a pending draft for the inbox. Status
        "auto_sent" on the returned draft is the caller's cue to deliver
        ``final_text`` — the engine itself still never sends. Auto-sent
        words never feed memory: the representative doesn't grade itself.
        """
        draft = self.draft(
            scope,
            conversation_id=conversation_id,
            inbound_text=inbound_text,
            display_name=display_name,
            history=history,
            model=model,
        )
        if (
            self._store.mode(scope) == "auto"
            and self.auto_allowed(scope)
            and draft.gate.auto_ok
        ):
            decided = self._store.decide_draft(
                draft.draft_id,
                status="auto_sent",
                final_text=draft.generated_text,
            )
            if decided is not None:
                return decided
        return draft

    def pending(self, scope: str) -> list[Draft]:
        return self._store.pending_drafts(scope)

    def get(self, scope: str, draft_id: str) -> Draft:
        """The scope's own draft; a stranger's is indistinguishable from
        missing. Raises KeyError either way."""
        draft = self._store.get_draft(draft_id)
        if draft is None or draft.scope != scope:
            raise KeyError(draft_id)
        return draft

    def decide(
        self, scope: str, draft_id: str, *, action: str, text: str | None = None
    ) -> Draft:
        """Record the user's verdict. Raises KeyError for a draft that isn't
        theirs (indistinguishable from missing), ValueError for a bad action
        or a double decision. Delivery is the caller's job — a decided draft
        is a record, not a send."""
        draft = self._store.get_draft(draft_id)
        if draft is None or draft.scope != scope:
            raise KeyError(draft_id)
        if action == "send":
            status, final = "sent", draft.generated_text
        elif action == "edit":
            final = (text or "").strip()
            if not final:
                raise ValueError("an edited draft needs the edited words")
            status = "edited"
        elif action == "discard":
            status, final = "discarded", None
        else:
            raise ValueError("action must be send, edit, or discard")
        decided = self._store.decide_draft(draft_id, status=status, final_text=final)
        if decided is None:
            raise ValueError("that draft was already decided")
        if final is not None:
            # The user approved these words as their own — the strongest
            # memory there is, and (edited) a Phase-2 preference pair.
            self._memory.remember(
                scope,
                key=f"draft:{draft_id}",
                prompt=draft.inbound_text,
                reply=final,
            )
        return decided

    # -------------------------------------------------------------- #
    # Lifecycle.                                                       #
    # -------------------------------------------------------------- #
    def erase(self, scope: str) -> int:
        """Data-subject erasure: the account's whole representative —
        settings, memory, drafts — gone. Returns rows removed."""
        return self._store.erase(scope)

    # -------------------------------------------------------------- #
    # The channels seam.                                               #
    # -------------------------------------------------------------- #
    def fallback(
        self, *, display_name: str, model: ChatModel | None = None
    ) -> "RepresentativeFallback":
        return RepresentativeFallback(self, display_name=display_name, model=model)


class RepresentativeFallback:
    """The replies engine's fallback port, representative-shaped.

    Draft mode never speaks: an inbound message the rules couldn't answer
    becomes a pending draft and the bot stays silent — returning None
    keeps the learned store's pairing behavior intact. Auto mode returns
    words ONLY when the engine's earned-autonomy path signed off; every
    ungated message still just files a draft.
    """

    def __init__(
        self,
        engine: RepresentativeEngine,
        *,
        display_name: str,
        model: ChatModel | None = None,
    ):
        self._engine = engine
        self._display_name = display_name
        self._model = model

    def reply(self, message: MessageEnvelope, context: dict[str, str]) -> str | None:
        scope = str(message.metadata.get("reply_scope") or message.channel)
        mode = self._engine.mode(scope)
        if mode not in ("draft", "auto"):
            return None
        try:
            draft = self._engine.auto_reply(
                scope,
                conversation_id=message.conversation_id,
                inbound_text=message.text,
                display_name=self._display_name,
                model=self._model,
            )
        except (ModelUnavailable, ValueError):
            # A dead model (or an empty message) must never break the bot's
            # polling loop; the message simply stays unanswered.
            return None
        if draft.status == "auto_sent" and draft.final_text:
            return draft.final_text
        return None
