"""Contract tests for the production ``HttpxTransport`` (workstream C).

The mapping logic — status, JSON decode, header pass-through, body encoding by
content type, and transport-failure handling — is exercised through httpx's
``MockTransport`` so no network is touched. One ``needs_network`` smoke test
exercises the real client path end to end and self-skips when offline.
"""

from __future__ import annotations

import json

import httpx
import pytest

from workflow_gps.providers.base import HttpTransport, ProviderResponse
from workflow_gps.providers.transport import HttpxTransport


def _transport(handler) -> HttpxTransport:
    """An ``HttpxTransport`` whose client is driven by an in-process handler."""
    return HttpxTransport(client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_satisfies_http_transport_port() -> None:
    transport = _transport(lambda request: httpx.Response(200))
    assert isinstance(transport, HttpTransport)


def test_json_body_is_the_default_encoding() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["content_type"] = request.headers.get("content-type")
        captured["body"] = request.content
        return httpx.Response(200, json={"ok": True})

    resp = _transport(handler).request("POST", "https://api.test/x", body={"a": 1})

    assert resp == ProviderResponse(status=200, json={"ok": True}, headers=resp.headers)
    assert "application/json" in captured["content_type"]
    assert json.loads(captured["body"]) == {"a": 1}


def test_form_content_type_is_form_encoded() -> None:
    """OAuth token endpoints declare form encoding; the body must be a form."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["content_type"] = request.headers.get("content-type")
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"access_token": "t"})

    resp = _transport(handler).request(
        "POST",
        "https://idp.test/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body={"grant_type": "authorization_code", "code": "abc"},
    )

    assert resp.status == 200
    assert "x-www-form-urlencoded" in captured["content_type"]
    assert "grant_type=authorization_code" in captured["body"]
    assert "code=abc" in captured["body"]


def test_non_json_body_decodes_to_empty_dict() -> None:
    resp = _transport(lambda request: httpx.Response(200, text="not json")).request(
        "GET", "https://api.test/x"
    )
    assert resp.status == 200
    assert resp.json == {}


def test_empty_body_decodes_to_empty_dict() -> None:
    resp = _transport(lambda request: httpx.Response(204)).request(
        "GET", "https://api.test/x"
    )
    assert resp.status == 204
    assert resp.json == {}


def test_non_dict_json_is_wrapped_under_data() -> None:
    resp = _transport(lambda request: httpx.Response(200, json=[1, 2, 3])).request(
        "GET", "https://api.test/x"
    )
    assert resp.json == {"data": [1, 2, 3]}


def test_response_headers_are_preserved() -> None:
    resp = _transport(
        lambda request: httpx.Response(200, headers={"X-Thing": "v"})
    ).request("GET", "https://api.test/x")
    # httpx normalises header names to lower case.
    assert resp.headers.get("x-thing") == "v"


def test_error_status_passes_through_unraised() -> None:
    """4xx/5xx are returned for the pipeline to classify, not raised here."""
    resp = _transport(
        lambda request: httpx.Response(503, json={"error": "down"})
    ).request("GET", "https://api.test/x")
    assert resp.status == 503
    assert resp.json == {"error": "down"}


def test_transport_failure_becomes_retryable_503() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    resp = _transport(handler).request("GET", "https://api.test/x")
    assert resp.status == 503
    assert "boom" in resp.json["error"]


def test_drives_a_provider_adapter_end_to_end() -> None:
    """Wire the transport through the real BaseProviderAdapter pipeline."""
    from workflow_gps.providers import OpenAiAdapter, SecretVault

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer sk-test"
        assert request.headers.get("x-request-id")
        return httpx.Response(200, json={"data": []})

    vault = SecretVault()
    ref = vault.put("sk-test", kind="api_key")
    adapter = OpenAiAdapter(vault=vault, transport=_transport(handler), api_key_ref=ref)
    assert adapter.authenticated_call(method="GET", path="/models") == {"data": []}


@pytest.mark.needs_network
def test_live_request_through_the_managed_proxy() -> None:
    """Real client path against an allow-listed host; skips when unreachable."""
    transport = HttpxTransport()
    try:
        resp = transport.request("GET", "https://pypi.org/pypi/pip/json")
    finally:
        transport.close()
    if resp.status != 200:
        pytest.skip(f"network unavailable (status {resp.status})")
    assert isinstance(resp.json, dict)
    assert resp.json.get("info", {}).get("name") == "pip"
