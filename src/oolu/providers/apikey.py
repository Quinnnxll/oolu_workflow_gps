"""API-key provider adapters: OpenAI and Anthropic.

Both share the API-key pipeline in :class:`ApiKeyProviderAdapter`: capability
discovery against a models endpoint, authenticated requests with idempotency keys,
retries, rate-limit/budget enforcement, and error classification. The key lives in
the vault and is minted into an auth header only at call time. Organization/project
service identities (OpenAI) and the managed enterprise gateway (Anthropic) are
configuration on top of the same surface.
"""

from __future__ import annotations

from typing import Any

from .base import BaseProviderAdapter, HttpTransport
from .errors import classify_status
from .vault import CredentialRef, SecretVault


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
        idempotency_key: str | None = None,
        cost: float = 1.0,
    ) -> dict:
        return self.invoke(
            "/chat/completions",
            {"model": model, "messages": messages},
            idempotency_key=idempotency_key,
            cost=cost,
        )


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
        idempotency_key: str | None = None,
        cost: float = 1.0,
    ) -> dict:
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        # Anthropic takes the system prompt as a top-level parameter, not a
        # "system"-role message.
        if system:
            body["system"] = system
        return self.invoke(
            "/messages",
            body,
            idempotency_key=idempotency_key,
            cost=cost,
        )
