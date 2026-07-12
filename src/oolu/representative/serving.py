"""Which model speaks for a user: the adapter-serving seam.

Three layers, weakest first:

* ``NoopAdapterServer`` — Phase 0: everyone shares the base model.
* ``StoreAdapterServer`` — registry-aware: knows WHICH adapter is a
  scope's live voice (for status and routing decisions), no transport.
* ``VllmAdapterServer`` — the real thing: one shared base on a vLLM
  server (``--enable-lora`` + runtime updating), per-user LoRAs loaded
  and unloaded at runtime, and a per-adapter ChatModel whose requests
  select the user's voice by the OpenAI ``model`` field.

Adapter names are ``rep-{digest}-v{n}`` — scope strings carry tenant
separators and usernames, so the filesystem- and URL-facing name is a
digest, never the raw scope.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..chat import ChatModel, ModelUnavailable
from .store import RepresentativeStore


def scope_digest(scope: str) -> str:
    return hashlib.sha256(scope.encode("utf-8")).hexdigest()[:12]


def adapter_name(scope: str, version: int) -> str:
    return f"rep-{scope_digest(scope)}-v{int(version)}"


@runtime_checkable
class AdapterServer(Protocol):
    """Port for per-user adapter routing on the inference server."""

    def model_for(self, scope: str) -> str | None:
        """The scope's model string, or None to use the shared base."""
        ...


class NoopAdapterServer:
    def model_for(self, scope: str) -> str | None:
        return None


class StoreAdapterServer:
    """The registry's answer, no transport: name the live voice."""

    def __init__(self, store: RepresentativeStore):
        self._store = store

    def model_for(self, scope: str) -> str | None:
        active = self._store.active_adapter(scope)
        if active is None:
            return None
        return adapter_name(scope, int(active["version"]))


# A transport is (url, payload-or-None) -> decoded JSON dict. Injectable so
# every vLLM interaction is testable without a server.
Transport = Callable[[str, dict | None], dict]


def _urllib_transport(url: str, payload: dict | None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=120.0) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if body.strip() else {}


class VllmAdapterServer(StoreAdapterServer):
    """Runtime LoRA management + per-adapter chat on one vLLM server.

    Loading needs the server started with ``--enable-lora`` and
    ``VLLM_ALLOW_RUNTIME_LORA_UPDATING=1``; a load survives until the
    server restarts, so ``load()`` is called again idempotently by the
    worker on every activation."""

    def __init__(
        self,
        store: RepresentativeStore,
        *,
        api_base: str,
        transport: Transport | None = None,
    ):
        super().__init__(store)
        self._api_base = api_base.rstrip("/")
        self._transport = transport or _urllib_transport

    def load(self, name: str, adapter_dir: str | Path) -> None:
        self._call(
            "/load_lora_adapter",
            {"lora_name": name, "lora_path": str(adapter_dir)},
        )

    def unload(self, name: str) -> None:
        self._call("/unload_lora_adapter", {"lora_name": name})

    def chat_model(self, scope: str) -> ChatModel | None:
        """A ChatModel speaking as this scope's live adapter, or None when
        the user has no adapter yet (the shared model then answers)."""
        name = self.model_for(scope)
        if name is None:
            return None
        return _VllmChatModel(self._api_base, name, self._transport)

    def _call(self, path: str, payload: dict) -> dict:
        try:
            return self._transport(f"{self._api_base}{path}", payload)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ModelUnavailable(f"vLLM adapter call failed: {exc}") from exc


class _VllmChatModel:
    """The narrow ChatModel port over one adapter's completions."""

    def __init__(self, api_base: str, model: str, transport: Transport):
        self._api_base = api_base
        self._model = model
        self._transport = transport

    def reply(self, messages: list[dict]) -> str:
        try:
            response = self._transport(
                f"{self._api_base}/chat/completions",
                {"model": self._model, "messages": messages, "temperature": 0.7},
            )
            return str(response["choices"][0]["message"]["content"])
        except (urllib.error.URLError, OSError, LookupError, TypeError, ValueError) as exc:
            # A dead adapter server degrades to the shared model upstream —
            # it must never be a dead conversation.
            raise ModelUnavailable(f"adapter completion failed: {exc}") from exc
