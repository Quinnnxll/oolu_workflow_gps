"""API-key provider adapters: OpenAI and Anthropic.

Both share the API-key pipeline in :class:`ApiKeyProviderAdapter`: capability
discovery against a models endpoint, authenticated requests with idempotency keys,
retries, rate-limit/budget enforcement, and error classification. The key lives in
the vault and is minted into an auth header only at call time. Organization/project
service identities (OpenAI) and the managed enterprise gateway (Anthropic) are
configuration on top of the same surface.
"""

from __future__ import annotations

import json
from typing import Any, Iterator

from .base import BaseProviderAdapter, HttpTransport
from .errors import classify_status
from .registry import anthropic_thinking_model, openai_reasoning_model
from .tools import ToolSpec
from .vault import CredentialRef, SecretVault

# Which models speak which knobs is the registry's knowledge
# (providers/registry.py) — one capability table for routing AND wire
# construction. The adapters keep only the wire-level guards.
_openai_reasoning_model = openai_reasoning_model
_anthropic_thinking_model = anthropic_thinking_model

# Anthropic's floor for a thinking budget; below it the block is invalid.
_MIN_THINKING_BUDGET = 1024


def openai_sse_events(
    lines: Iterator[str],
) -> Iterator[tuple[str | None, dict | None]]:
    """Parse OpenAI/Ollama chat-completions SSE lines into (delta, usage).

    Each yielded pair is ``(text_delta_or_None, usage_dict_or_None)``; the
    ``[DONE]`` sentinel ends the stream. Pure and transport-agnostic, so it is
    unit-testable against a list of lines — the streaming HTTP call is the only
    part that needs a live server."""
    for line in lines:
        text = (line or "").strip()
        if not text.startswith("data:"):
            continue
        payload = text[len("data:") :].strip()
        if payload == "[DONE]":
            return
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        usage = data.get("usage") or None
        delta = None
        choices = data.get("choices") or []
        if choices:
            delta = ((choices[0] or {}).get("delta") or {}).get("content") or None
        if delta is not None or usage is not None:
            yield delta, usage


class ApiKeyProviderAdapter(BaseProviderAdapter):
    def __init__(
        self,
        *,
        name: str,
        vault: SecretVault,
        transport: HttpTransport,
        base_url: str,
        api_key_ref: CredentialRef,
        auth_scheme: str = "Bearer",
        auth_header: str = "Authorization",
        extra_headers: dict[str, str] | None = None,
        discovery_path: str = "/models",
        ping_path: str = "/models",
        **kwargs,
    ):
        super().__init__(vault=vault, transport=transport, base_url=base_url, **kwargs)
        self.name = name
        self._api_key_ref = api_key_ref
        self._scheme = auth_scheme
        self._header = auth_header
        self._extra = dict(extra_headers or {})
        self._discovery_path = discovery_path
        self._ping_path = ping_path
        self._caps: frozenset[str] | None = None

    # --- BaseProviderAdapter hooks --------------------------------------- #
    def _primary_ref(self) -> CredentialRef:
        return self._api_key_ref

    def _auth_headers(self) -> dict[str, str]:
        return {
            **self._vault.authorize_header(
                self._api_key_ref, scheme=self._scheme, header=self._header
            ),
            **self._extra,
        }

    def revoke_credentials(self) -> None:
        self._vault.revoke(self._api_key_ref)

    # --- capability discovery -------------------------------------------- #
    def capabilities(self) -> frozenset[str]:
        if self._caps is None:
            response = self._transport.request(
                "GET",
                f"{self._base_url}{self._discovery_path}",
                headers=self._auth_headers(),
            )
            error_cls = classify_status(response.status)
            if error_cls is not None:
                raise error_cls(f"{self.name}: discovery status {response.status}")
            self._caps = self._parse_capabilities(response.json)
        return self._caps

    @staticmethod
    def _parse_capabilities(data: dict[str, Any]) -> frozenset[str]:
        items = data.get("data") or data.get("models") or []
        ids = {item.get("id") for item in items if isinstance(item, dict)}
        return frozenset(item for item in ids if item)

    # --- representative + generic authenticated requests ----------------- #
    def ping(self, *, idempotency_key: str | None = None) -> dict:
        return self.authenticated_call(
            method="GET", path=self._ping_path, idempotency_key=idempotency_key
        )

    def invoke(
        self,
        path: str,
        body: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        cost: float = 1.0,
    ) -> dict:
        return self.authenticated_call(
            method="POST",
            path=path,
            body=body,
            idempotency_key=idempotency_key,
            cost=cost,
        )


class OpenAiAdapter(ApiKeyProviderAdapter):
    """OpenAI project/API-key adapter with optional org/project service identity."""

    def __init__(
        self,
        *,
        vault: SecretVault,
        transport: HttpTransport,
        api_key_ref: CredentialRef,
        base_url: str = "https://api.openai.com/v1",
        organization: str | None = None,
        project: str | None = None,
        **kwargs,
    ):
        extra: dict[str, str] = {}
        if organization:
            extra["OpenAI-Organization"] = organization
        if project:
            extra["OpenAI-Project"] = project
        super().__init__(
            name="openai",
            vault=vault,
            transport=transport,
            base_url=base_url,
            api_key_ref=api_key_ref,
            auth_scheme="Bearer",
            extra_headers=extra,
            **kwargs,
        )

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        tools: list[ToolSpec] | None = None,
        tool_choice: str | None = None,
        idempotency_key: str | None = None,
        cost: float = 1.0,
        max_tokens: int | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {"model": model, "messages": messages}
        # Effort rides per model family: reasoning models take the
        # modern ceiling + effort dial and refuse a temperature; the
        # classic wire (gpt-4o, every local OpenAI-compatible server)
        # takes max_tokens + temperature and predates the dial.
        if _openai_reasoning_model(model):
            if max_tokens is not None:
                body["max_completion_tokens"] = int(max_tokens)
            if reasoning_effort:
                body["reasoning_effort"] = str(reasoning_effort)
        else:
            if max_tokens is not None:
                body["max_tokens"] = int(max_tokens)
            if temperature is not None:
                body["temperature"] = float(temperature)
        if tools:
            body["tools"] = [spec.as_openai() for spec in tools]
            # "auto" is this wire's default; only a real preference rides.
            if tool_choice and tool_choice != "auto":
                body["tool_choice"] = (
                    {"type": "function", "function": {"name": tool_choice}}
                    if tool_choice != "required"
                    else "required"
                )
        return self.invoke(
            "/chat/completions",
            body,
            idempotency_key=idempotency_key,
            cost=cost,
        )

    def chat_stream(  # pragma: no cover - needs a live streaming server
        self, messages: list[dict[str, Any]], *, model: str
    ) -> Iterator[tuple[str | None, dict | None]]:
        """Stream a chat completion as (text-delta, usage) pairs. Requests
        usage in the terminal frame so the call still meters honestly."""
        lines = self.stream_call(
            method="POST",
            path="/chat/completions",
            body={
                "model": model,
                "messages": messages,
                "stream": True,
                "stream_options": {"include_usage": True},
            },
        )
        return openai_sse_events(lines)


class AnthropicAdapter(ApiKeyProviderAdapter):
    """Anthropic API-key adapter, or the managed enterprise gateway variant."""

    def __init__(
        self,
        *,
        vault: SecretVault,
        transport: HttpTransport,
        api_key_ref: CredentialRef,
        base_url: str = "https://api.anthropic.com/v1",
        version: str = "2023-06-01",
        gateway: bool = False,
        **kwargs,
    ):
        if gateway:
            # The managed gateway brokers the real key; callers present a gateway
            # bearer token instead of the raw Anthropic key.
            super().__init__(
                name="anthropic-gateway",
                vault=vault,
                transport=transport,
                base_url=base_url,
                api_key_ref=api_key_ref,
                auth_scheme="Bearer",
                auth_header="Authorization",
                extra_headers={"anthropic-version": version},
                **kwargs,
            )
        else:
            super().__init__(
                name="anthropic",
                vault=vault,
                transport=transport,
                base_url=base_url,
                api_key_ref=api_key_ref,
                auth_scheme="",
                auth_header="x-api-key",
                extra_headers={"anthropic-version": version},
                **kwargs,
            )

    def messages(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        max_tokens: int = 1024,
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
        tool_choice: str | None = None,
        idempotency_key: str | None = None,
        cost: float = 1.0,
        web_search: bool = False,
        temperature: float | None = None,
        thinking_budget: int | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        # Extended thinking, where the seat asks for it AND the model
        # speaks it. The API's own rules ride here: the budget needs a
        # floor, must fit under the output ceiling, refuses a sampling
        # temperature beside it, and requires the model's own choice of
        # tools (auto) — a forced tool_choice keeps thinking off.
        thinking = (
            thinking_budget is not None
            and thinking_budget >= _MIN_THINKING_BUDGET
            and _anthropic_thinking_model(model)
            and (not tool_choice or tool_choice == "auto")
        )
        if thinking:
            budget = min(int(thinking_budget), max(max_tokens // 2, _MIN_THINKING_BUDGET))
            if budget >= _MIN_THINKING_BUDGET and budget < max_tokens:
                body["thinking"] = {"type": "enabled", "budget_tokens": budget}
            else:
                thinking = False
        if temperature is not None and not thinking:
            body["temperature"] = float(temperature)
        # Anthropic takes the system prompt as a top-level parameter, not a
        # "system"-role message — carried as a block so the frozen prefix
        # (tools + system) gets a prompt-cache breakpoint: multi-turn
        # authoring loops stop re-paying the full prompt every step.
        if system:
            body["system"] = [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        wire_tools: list[dict[str, Any]] = [
            spec.as_anthropic() for spec in tools or []
        ]
        # The provider's own web-search tool: the SEARCH runs on Anthropic's
        # servers inside this same API call — the machine here needs no web
        # access of its own, which is exactly what lets a keyed OoLu answer
        # current-facts questions from any install.
        if web_search:
            wire_tools.append(
                {
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": 3,
                }
            )
        if wire_tools:
            body["tools"] = wire_tools
        if tools and tool_choice and tool_choice != "auto":
            body["tool_choice"] = (
                {"type": "any"}
                if tool_choice == "required"
                else {"type": "tool", "name": tool_choice}
            )
        return self.invoke(
            "/messages",
            body,
            idempotency_key=idempotency_key,
            cost=cost,
        )
