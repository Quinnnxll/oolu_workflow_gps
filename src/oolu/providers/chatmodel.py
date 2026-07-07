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
from .keyring import PROVIDERS, ModelKeyring, fingerprint
from .vault import SecretVault

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


@dataclass(frozen=True)
class _Telemetry:
    """Duck-typed for ``ModelCallMeter.record``: the fields it reads."""

    model: str
    tier: str
    prompt_tokens: int
    completion_tokens: int
    duration_s: float


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
        budget: Callable[[], float] | None = None,  # USD cap; 0/None = no cap
        preference: Callable[[], str] | None = None,  # auto|anthropic|openai
        tier: Callable[[], str] | None = None,  # fast|reasoning
        max_tokens: int = 1024,
    ) -> None:
        self._keyring = keyring
        self._tenant = tenant
        self._transport = transport
        self._meter = meter
        self._budget = budget or (lambda: 0.0)
        self._preference = preference or (lambda: "auto")
        self._tier = tier or (lambda: "fast")
        self._max_tokens = max_tokens
        self._adapters: dict[tuple[str, str], Any] = {}

    # ------------------------------------------------------------------ #
    def reply(self, messages: list[dict]) -> str:
        self._check_budget()
        errors: list[str] = []
        for provider in self._order():
            secret = self._keyring.secret_for(self._tenant, provider)
            if secret is None:
                continue
            try:
                return self._ask(provider, secret, messages)
            except ProviderError as exc:
                errors.append(f"{provider}: {exc}")
                continue
        if errors:
            raise ModelUnavailable("; ".join(errors))
        raise ModelUnavailable("no model key is configured")

    # ------------------------------------------------------------------ #
    def _check_budget(self) -> None:
        cap = float(self._budget() or 0.0)
        if cap <= 0 or self._meter is None:
            return
        spent = self._meter.total_cost(CHAT_PURPOSE)
        if spent >= cap:
            raise ModelBudgetExceeded(
                f"I've reached the model spending cap you set "
                f"(${spent:.2f} of ${cap:.2f}). Raise budget.model_cap in "
                f"Settings to keep the model on — meanwhile I'll still run "
                f"your tasks the direct way."
            )

    def _order(self) -> tuple[str, ...]:
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
                rest, model=model_id, max_tokens=self._max_tokens, system=system
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
            choices = data.get("choices") or []
            message = (choices[0] or {}).get("message", {}) if choices else {}
            text = message.get("content") or ""
            usage = data.get("usage", {}) or {}
            prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
            completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        if self._meter is not None:
            self._meter.record(
                CHAT_PURPOSE,
                _Telemetry(
                    model=str(data.get("model") or model_id),
                    tier=tier,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    duration_s=time.monotonic() - started,
                ),
            )
        if not text:
            raise ModelUnavailable(f"{provider} returned an empty reply")
        return text
