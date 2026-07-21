"""The bridge from the chat's ``ChatModel`` port to real LLM providers.

``ChatAssistant`` speaks to one thing: ``reply(messages) -> str``. This module
gives that port a real brain — the tenant's own keys from the
:class:`~oolu.providers.keyring.ModelKeyring`, the existing Anthropic/OpenAI
adapters for transport, retries and secret handling, the
:class:`~oolu.billing.model_calls.ModelCallMeter` so every consultation enters
the books, and a spending cap read from the settings node.

Routing is deliberately v0: try the preferred provider, fall back to the next
configured one on failure, and if nothing answers raise
:class:`~oolu.chat.ModelUnavailable` — which the assistant catches to degrade
into its model-less path, so chat never dies with the network. A reached
spending cap raises :class:`~oolu.chat.ModelBudgetExceeded` with the words the
assistant says out loud; nothing is silently skipped.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from ..chat import ModelBudgetExceeded, ModelUnavailable
from .apikey import AnthropicAdapter, OpenAiAdapter
from .base import HttpTransport
from .errors import ProviderError
from .keyring import PROVIDERS, KeyringError, ModelKeyring, fingerprint
from .tools import (
    ToolReply,
    ToolSpec,
    parse_anthropic_tool_reply,
    parse_openai_tool_reply,
    to_anthropic_messages,
    to_openai_messages,
)
from .vault import SecretVault

# What the user must hear when a SAVED key can't be decrypted on this
# machine (the machine key changed under the ciphertext — a moved volume,
# a rebuilt install). The key is unusable but removable; chat must degrade
# to these words, never die.
_UNREADABLE_KEY = (
    "the saved {provider} key can't be read on this machine (the install's"
    " encryption key changed) — remove it in Settings and paste it again"
)

# What each (provider, tier) means concretely. Data, not policy: the tier is
# the setting users choose; these ids are the current defaults behind it.
DEFAULT_MODELS: dict[str, dict[str, str]] = {
    "anthropic": {
        "fast": "claude-haiku-4-5-20251001",
        "reasoning": "claude-sonnet-5",
    },
    "openai": {
        "fast": "gpt-4o-mini",
        "reasoning": "gpt-4o",
    },
}

# The purpose tag every chat consultation is metered under.
CHAT_PURPOSE = "chat.turn"

# Where the default brain lives (the model.source setting):
#   subscription — the OoLu plan's brain, Claude first, always.
#   own-api      — the key the user added IS the default model; their
#                  model.provider preference overrides the plan's order.
#   local        — a model server on this machine (OpenAI-compatible:
#                  Ollama, LM Studio, llama.cpp server), no key, no cloud.
MODEL_SOURCES = ("subscription", "own-api", "local")


@dataclass(frozen=True)
class _Telemetry:
    """Duck-typed for ``ModelCallMeter.record``: the fields it reads."""

    model: str
    tier: str
    prompt_tokens: int
    completion_tokens: int
    duration_s: float


def _parse_openai_shape(data: dict) -> tuple[str, int, int]:
    """(text, prompt_tokens, completion_tokens) from a chat/completions
    response — the wire shape OpenAI and every local server speaks."""
    choices = data.get("choices") or []
    message = (choices[0] or {}).get("message", {}) if choices else {}
    text = message.get("content") or ""
    usage = data.get("usage", {}) or {}
    return (
        text,
        int(usage.get("prompt_tokens", 0) or 0),
        int(usage.get("completion_tokens", 0) or 0),
    )


def _split_system(messages: list[dict]) -> tuple[str, list[dict]]:
    """Anthropic takes the system prompt as a parameter, not a message."""
    system_parts = [
        m.get("content", "") for m in messages if m.get("role") == "system"
    ]
    rest = [m for m in messages if m.get("role") != "system"]
    return "\n\n".join(p for p in system_parts if p), rest


class ChatModelRouter:
    """A ``ChatModel`` over the tenant's configured providers.

    Built per tenant (keys and settings are tenant-scoped); adapters are
    cached by key fingerprint so a replaced key gets a fresh adapter and a
    revoked one stops being used.
    """

    def __init__(
        self,
        keyring: ModelKeyring,
        tenant: str,
        *,
        transport: HttpTransport | None = None,
        meter=None,  # billing.ModelCallMeter
        # The cap in the USER'S spending currency; 0/None = no cap. The
        # router converts it through `currency` to the meter's USD unit.
        budget: Callable[[], float] | None = None,
        currency: Callable[[], str] | None = None,  # regional code, e.g. EUR
        preference: Callable[[], str] | None = None,  # auto|anthropic|openai
        tier: Callable[[], str] | None = None,  # fast|reasoning
        source: Callable[[], str] | None = None,  # see MODEL_SOURCES
        local_url: Callable[[], str] | None = None,  # OpenAI-compatible base
        local_model: Callable[[], str] | None = None,  # e.g. llama3.2
        # Whether the model may use its provider's server-side web search
        # (Anthropic today). The search happens inside the API call — the
        # local machine needs no web access of its own.
        web_search: Callable[[], bool] | None = None,
        max_tokens: int = 1024,
        purpose: str = CHAT_PURPOSE,  # what the meter books this under
        # billing.SubscriptionBrain: the HOSTED plan's keys + allowance.
        # None (every self-hosted install) keeps the honest "not live yet".
        subscription=None,
    ) -> None:
        self._keyring = keyring
        self._tenant = tenant
        self._transport = transport
        self._meter = meter
        self._subscription = subscription
        self._budget = budget or (lambda: 0.0)
        self._currency = currency or (lambda: "USD")
        self._preference = preference or (lambda: "auto")
        self._tier = tier or (lambda: "fast")
        self._source = source or (lambda: "subscription")
        self._local_url = local_url or (lambda: "")
        self._local_model = local_model or (lambda: "")
        self._web_search = web_search or (lambda: False)
        self._max_tokens = max_tokens
        self._purpose = purpose
        self._adapters: dict[tuple[str, str], Any] = {}

    # ------------------------------------------------------------------ #
    def web_search_ready(self) -> bool:
        """Whether a reply on this router carries the provider's server-side
        web-search tool — the setting is on AND the path that will answer is
        an Anthropic one. A local model never searches (local means local),
        and only the Anthropic adapter speaks the tool today, so a router
        that would answer through another provider reports False."""
        if not self._web_search():
            return False
        source = self._source()
        if source == "local":
            return False
        if (
            source == "subscription"
            and self._subscription is not None
            and self._subscription.configured()
        ):
            # The plan's order is Claude first: search rides whenever the
            # platform holds an Anthropic key.
            return self._subscription.secret_for("anthropic") is not None
        for provider in self._order():
            try:
                if self._keyring.secret_for(self._tenant, provider) is None:
                    continue
            except KeyringError:
                continue  # an unreadable key can't answer, let alone search
            # The first keyed provider answers — search only if it's Claude.
            return provider == "anthropic"
        return False

    def reply(self, messages: list[dict]) -> str:
        self._check_budget()
        return self._route(
            keyed=lambda provider, secret: self._ask(provider, secret, messages),
            local=lambda: self._ask_local(messages),
        )

    def consult(
        self,
        messages: list[dict],
        *,
        tools: list[ToolSpec],
        tool_choice: str = "auto",
    ) -> ToolReply:
        """``reply``'s structured sibling: the same routing, budget, and
        books, but the tools ride natively on the wire and the answer
        comes back parsed — text plus :class:`ToolCall`\\ s — instead of
        prose to regex through. The transcript may already carry earlier
        tool exchanges (the neutral shape ``providers.tools`` documents);
        each dialect conversion happens here, per provider."""
        self._check_budget()
        return self._route(
            keyed=lambda provider, secret: self._consult_provider(
                provider, secret, messages, tools, tool_choice
            ),
            local=lambda: self._consult_local(messages, tools, tool_choice),
        )

    def _route(self, *, keyed, local):
        """One routing skeleton for every consultation shape: local means
        local, a configured subscription answers on the plan's keys, and
        otherwise the tenant's own keys take the preference order."""
        source = self._source()
        if source == "local":
            # The machine's own brain: no key, no cloud, no fallback into
            # one — choosing local means local, so a dead local server
            # degrades to the model-less path instead of quietly phoning
            # a provider.
            return local()
        if (
            source == "subscription"
            and self._subscription is not None
            and self._subscription.configured()
        ):
            # The hosted plan's brain: platform keys, metered per tenant
            # against the plan's monthly allowance.
            return self._subscription_route(keyed)
        errors: list[str] = []
        for provider in self._order():
            try:
                secret = self._keyring.secret_for(self._tenant, provider)
            except KeyringError:
                errors.append(_UNREADABLE_KEY.format(provider=provider))
                continue
            if secret is None:
                continue
            try:
                return keyed(provider, secret)
            except ProviderError as exc:
                errors.append(f"{provider}: {exc}")
                continue
        if errors:
            raise ModelUnavailable("; ".join(errors))
        if source == "subscription":
            # Honesty over aspiration: this host has no platform keys, so
            # "subscription" with no keys is a dead end — say so and point
            # at the two doors that do open today.
            raise ModelUnavailable(
                "the OoLu subscription brain isn't live yet — add your own"
                " API key in Settings (model.source switches to 'own-api'"
                " automatically) or point model.source at 'local'"
            )
        raise ModelUnavailable("no model key is configured")

    def reply_stream(self, messages: list[dict]):
        """Yield the reply's text deltas as the model generates them.

        Real token streaming for the local brain (the product default) and
        keyed OpenAI-shape providers; every other source (subscription,
        Anthropic) yields the finished reply as a single chunk — the streaming
        transport is identical, only the granularity differs, and callers that
        forward ⟨think⟩ deltas still work either way."""
        self._check_budget()
        source = self._source()
        if source == "local":
            yield from self._stream_local(messages)
            return
        if source != "subscription":
            for provider in self._order():
                if provider != "openai":
                    continue
                try:
                    secret = self._keyring.secret_for(self._tenant, provider)
                except KeyringError:
                    continue
                if secret is None:
                    continue
                try:
                    yield from self._stream_openai(provider, secret, messages)
                    return
                except ProviderError:
                    break  # fall through to the blocking reply
        # Subscription/Anthropic/no-key: the full reply, in one chunk.
        yield self.reply(messages)

    def _stream_local(self, messages):  # pragma: no cover - needs a live server
        url = str(self._local_url() or "").strip().rstrip("/")
        model_id = str(self._local_model() or "").strip()
        if not url or not model_id:
            raise ModelUnavailable(
                "local model is selected but not configured — set the "
                "local model URL and name in Settings"
            )
        started = time.monotonic()
        parts: list[str] = []
        usage: dict = {}
        try:
            for text, u in self._local_adapter(url).chat_stream(
                messages, model=model_id
            ):
                if u:
                    usage = u
                if text:
                    parts.append(text)
                    yield text
        except ProviderError as exc:
            raise ModelUnavailable(f"local ({url}): {exc}") from exc
        if not "".join(parts):
            raise ModelUnavailable(f"local ({url}) returned an empty reply")
        self._record_stream(model_id, "local", usage, started)

    def _stream_openai(self, provider, secret, messages):  # pragma: no cover - live
        tier = self._tier()
        model_id = DEFAULT_MODELS[provider].get(
            tier, DEFAULT_MODELS[provider]["fast"]
        )
        started = time.monotonic()
        parts: list[str] = []
        usage: dict = {}
        for text, u in self._adapter(provider, secret).chat_stream(
            messages, model=model_id
        ):
            if u:
                usage = u
            if text:
                parts.append(text)
                yield text
        if not "".join(parts):
            raise ModelUnavailable(f"{provider} returned an empty reply")
        self._record_stream(model_id, tier, usage, started)

    def _record_stream(self, model, tier, usage, started):  # pragma: no cover
        if self._meter is None:
            return
        record = self._meter.record(
            self._purpose,
            _Telemetry(
                model=model,
                tier=tier,
                prompt_tokens=int((usage or {}).get("prompt_tokens", 0) or 0),
                completion_tokens=int((usage or {}).get("completion_tokens", 0) or 0),
                duration_s=time.monotonic() - started,
            ),
        )
        self._book_usage(record)

    def _subscription_route(self, ask):
        """Answer through the PLATFORM's keys, inside the plan's allowance.

        The plan gate first (free includes no hosted brain), then the
        month's spend against the allowance, then the plan's provider
        order — Claude first, always. Every failure names the way out.
        ``ask(provider, secret)`` is whichever consultation shape the
        caller is routing — plain reply or a tool consultation.
        """
        brain = self._subscription
        allowance = brain.allowance_for(self._tenant)
        trial = bool(getattr(brain, "is_trial", lambda _t: False)(self._tenant))
        if allowance <= 0:
            raise ModelUnavailable(
                "the hosted OoLu brain comes with a paid plan — choose one"
                " in Settings, add your own API key, or run a local model"
            )
        spent = getattr(brain, "spend_for", brain.month_spend)(self._tenant)
        if spent >= allowance:
            from ..currency import format_amount, from_usd

            code = self._currency() or "USD"
            amount = format_amount(from_usd(allowance, code), code)
            if trial:
                # The trial is a lifetime total: it ends, it never renews.
                raise ModelBudgetExceeded(
                    f"your free {amount} trial of the hosted brain is used"
                    " up — choose a plan in Settings, add your own API key,"
                    " or run a local model; your work here isn't going"
                    " anywhere"
                )
            raise ModelBudgetExceeded(
                f"this month's included model use ({amount}) is used"
                " up — it renews with the next month; add your own key in"
                " Settings to keep going meanwhile"
            )
        errors: list[str] = []
        for provider in PROVIDERS:  # the plan's order, not a preference
            try:
                secret = brain.secret_for(provider)
            except KeyringError:
                errors.append(_UNREADABLE_KEY.format(provider=provider))
                continue
            if secret is None:
                continue
            try:
                return ask(provider, secret)
            except ProviderError as exc:
                errors.append(f"{provider}: {exc}")
        if errors:
            raise ModelUnavailable("; ".join(errors))
        raise ModelUnavailable(
            "the hosted brain has no live provider right now — try again"
            " shortly, or add your own key in Settings"
        )

    # ------------------------------------------------------------------ #
    def _check_budget(self) -> None:
        cap = float(self._budget() or 0.0)
        if cap <= 0 or self._meter is None:
            return
        from ..currency import format_amount, from_usd, to_usd

        # The cap covers ALL model spend on this install — chat turns and
        # planning consultations share one pool, so the number the user set
        # is the number that holds. The user set it in THEIR regional
        # currency; the meter counts USD, so the comparison converts the
        # cap in and the words convert the spend back out.
        code = self._currency() or "USD"
        spent = self._meter.total_cost()
        if spent >= to_usd(cap, code):
            raise ModelBudgetExceeded(
                f"I've reached the model spending cap you set "
                f"({format_amount(from_usd(spent, code), code)} of "
                f"{format_amount(cap, code)}). Raise the cap in Settings "
                f"to keep the model on — meanwhile I'll still run your "
                f"tasks the direct way."
            )

    def _order(self) -> tuple[str, ...]:
        # The subscription's brain is Claude first — the plan's order, not
        # the user's key preference. Setting model.source to "own-api" is
        # the explicit act that lets the added key override that default.
        if self._source() == "subscription":
            return PROVIDERS
        preferred = self._preference()
        if preferred in PROVIDERS:
            rest = tuple(p for p in PROVIDERS if p != preferred)
            return (preferred, *rest)
        return PROVIDERS

    def _transport_or_real(self) -> HttpTransport:
        if self._transport is not None:
            return self._transport
        try:
            from .transport import HttpxTransport
        except ModuleNotFoundError as exc:
            raise ModelUnavailable(
                "no HTTP transport installed (pip install 'oolu[http]')"
            ) from exc
        self._transport = HttpxTransport()
        return self._transport

    def _adapter(self, provider: str, secret: str):
        cache_key = (provider, fingerprint(secret))
        cached = self._adapters.get(cache_key)
        if cached is not None:
            return cached
        # One vault per adapter: the secret's only in-process home, so its
        # redaction covers exactly the error messages this adapter raises.
        vault = SecretVault()
        ref = vault.put(secret, kind="api_key")
        cls = AnthropicAdapter if provider == "anthropic" else OpenAiAdapter
        adapter = cls(
            vault=vault, transport=self._transport_or_real(), api_key_ref=ref
        )
        self._adapters[cache_key] = adapter
        return adapter

    def _local_adapter(self, url: str):
        # Cached by URL so pointing at a different server gets a fresh
        # adapter. Local servers (Ollama, LM Studio) speak the OpenAI wire
        # shape and ignore the bearer token — "local" is a placeholder,
        # not a secret.
        cache_key = ("local", url)
        cached = self._adapters.get(cache_key)
        if cached is not None:
            return cached
        vault = SecretVault()
        ref = vault.put("local", kind="api_key")
        adapter = OpenAiAdapter(
            vault=vault,
            transport=self._transport_or_real(),
            api_key_ref=ref,
            base_url=url,
        )
        self._adapters[cache_key] = adapter
        return adapter

    def _ask_local(self, messages: list[dict]) -> str:
        url = str(self._local_url() or "").strip().rstrip("/")
        model_id = str(self._local_model() or "").strip()
        if not url or not model_id:
            raise ModelUnavailable(
                "local model is selected but not configured — set the "
                "local model URL and name in Settings"
            )
        started = time.monotonic()
        try:
            data = self._local_adapter(url).chat(messages, model=model_id)
        except ProviderError as exc:
            raise ModelUnavailable(f"local ({url}): {exc}") from exc
        text, prompt_tokens, completion_tokens = _parse_openai_shape(data)
        # Local turns still enter the books — usage is real telemetry
        # even when the marginal dollar cost is the machine's own.
        self._book(
            model=str(data.get("model") or model_id),
            tier="local",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            started=started,
        )
        if not text:
            raise ModelUnavailable(f"local ({url}) returned an empty reply")
        return text

    def _ask(self, provider: str, secret: str, messages: list[dict]) -> str:
        tier = self._tier()
        model_id = DEFAULT_MODELS[provider].get(
            tier, DEFAULT_MODELS[provider]["fast"]
        )
        adapter = self._adapter(provider, secret)
        started = time.monotonic()
        if provider == "anthropic":
            system, rest = _split_system(messages)
            data = adapter.messages(
                rest,
                model=model_id,
                max_tokens=self._max_tokens,
                system=system,
                web_search=self._web_search(),
            )
            text = "".join(
                block.get("text", "")
                for block in data.get("content", [])
                if isinstance(block, dict) and block.get("type") == "text"
            )
            usage = data.get("usage", {}) or {}
            prompt_tokens = int(usage.get("input_tokens", 0) or 0)
            completion_tokens = int(usage.get("output_tokens", 0) or 0)
        else:
            data = adapter.chat(messages, model=model_id)
            text, prompt_tokens, completion_tokens = _parse_openai_shape(data)
        self._book(
            model=str(data.get("model") or model_id),
            tier=tier,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            started=started,
        )
        if not text:
            raise ModelUnavailable(f"{provider} returned an empty reply")
        return text

    def _consult_provider(
        self,
        provider: str,
        secret: str,
        messages: list[dict],
        tools: list[ToolSpec],
        tool_choice: str,
    ) -> ToolReply:
        tier = self._tier()
        model_id = DEFAULT_MODELS[provider].get(
            tier, DEFAULT_MODELS[provider]["fast"]
        )
        adapter = self._adapter(provider, secret)
        started = time.monotonic()
        if provider == "anthropic":
            system, rest = _split_system(messages)
            data = adapter.messages(
                to_anthropic_messages(rest),
                model=model_id,
                max_tokens=self._max_tokens,
                system=system,
                tools=tools,
                tool_choice=tool_choice,
                web_search=self._web_search(),
            )
            reply = parse_anthropic_tool_reply(data)
        else:
            data = adapter.chat(
                to_openai_messages(messages),
                model=model_id,
                tools=tools,
                tool_choice=tool_choice,
            )
            reply = parse_openai_tool_reply(data)
        self._book(
            model=str(data.get("model") or model_id),
            tier=tier,
            prompt_tokens=reply.prompt_tokens,
            completion_tokens=reply.completion_tokens,
            started=started,
        )
        # A pure tool-call turn legitimately has no text; only a reply
        # with neither words nor calls is a dead one.
        if not reply.text and not reply.tool_calls:
            raise ModelUnavailable(f"{provider} returned an empty reply")
        return reply

    def _consult_local(
        self, messages: list[dict], tools: list[ToolSpec], tool_choice: str
    ) -> ToolReply:
        url = str(self._local_url() or "").strip().rstrip("/")
        model_id = str(self._local_model() or "").strip()
        if not url or not model_id:
            raise ModelUnavailable(
                "local model is selected but not configured — set the "
                "local model URL and name in Settings"
            )
        started = time.monotonic()
        try:
            data = self._local_adapter(url).chat(
                to_openai_messages(messages),
                model=model_id,
                tools=tools,
                tool_choice=tool_choice,
            )
        except ProviderError as exc:
            raise ModelUnavailable(f"local ({url}): {exc}") from exc
        reply = parse_openai_tool_reply(data)
        self._book(
            model=str(data.get("model") or model_id),
            tier="local",
            prompt_tokens=reply.prompt_tokens,
            completion_tokens=reply.completion_tokens,
            started=started,
        )
        if not reply.text and not reply.tool_calls:
            raise ModelUnavailable(f"local ({url}) returned an empty reply")
        return reply

    def _book(
        self,
        *,
        model: str,
        tier: str,
        prompt_tokens: int,
        completion_tokens: int,
        started: float,
    ) -> None:
        if self._meter is None:
            return
        record = self._meter.record(
            self._purpose,
            _Telemetry(
                model=model,
                tier=tier,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                duration_s=time.monotonic() - started,
            ),
        )
        self._book_usage(record)

    def _book_usage(self, record) -> None:
        """Per-tenant durable usage next to the in-memory telemetry: the
        subscription quota reads these books, so they must survive
        restarts. Booked under the source that answered."""
        if self._subscription is not None:
            self._subscription.record(self._tenant, record, self._source())


class RouterIntakeModel:
    """The orchestrator's ``IntakeModel`` over the tenant's keyring router.

    The bridge from Milestone A into planning: the same pasted key that
    answers chat now structures briefs. Degradation is silence, not noise —
    no key, a dead provider, or a reached cap returns ``""``, which the
    intaker treats as "no proposal" and falls back to its heuristic floor.
    """

    def __init__(self, router: ChatModelRouter):
        self._router = router

    def propose(self, intent: str) -> str:
        from ..chat import ModelBudgetExceeded, ModelUnavailable
        from ..orchestrator.intake import INTAKE_SYSTEM_PROMPT

        try:
            return self._router.reply(
                [
                    {"role": "system", "content": INTAKE_SYSTEM_PROMPT},
                    {"role": "user", "content": intent},
                ]
            )
        except (ModelUnavailable, ModelBudgetExceeded):
            return ""
