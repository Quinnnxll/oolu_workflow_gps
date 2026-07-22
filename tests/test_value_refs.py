"""The exact-value reference layer: refs in, exact values out.

Exit gate: an authoritative value is stored once — immutable, typed,
tenant-owned, content-addressed — and everything upstream speaks by
reference. The binder resolves refs deterministically (tenant wall,
type check, honest lookup failure) just before execution, so the
sandbox stages what the runtime holds; result outputs snapshot into
the same store; and the renderer puts exact stored values into a
model-shaped response — a missing reference refuses, never fabricates.
"""

from __future__ import annotations

import json

import pytest
from test_http_gateway import _app, _req

from oolu.durable.connection import DurableConnection
from oolu.gateway import GatewayApp
from oolu.values import ValueError_, ValueStore, render_segments


def _store(tmp_path):
    conn = DurableConnection(tmp_path / "v.db")
    return conn, ValueStore(conn)


def test_values_are_immutable_typed_and_exact(tmp_path):
    conn, store = _store(tmp_path)
    # Exactness: leading zeros, decimal scale, case — verbatim.
    order = store.put("t1", "007-A", value_type="identifier")
    amount = store.put("t1", "125.370", value_type="decimal")
    assert store.resolve(order.ref, tenant="t1") == "007-A"
    assert store.resolve(amount.ref, tenant="t1") == "125.370"
    # Content-addressed: the same typed value is the same reference.
    again = store.put("t1", "125.370", value_type="decimal")
    assert again.ref == amount.ref
    # Type check at resolution.
    with pytest.raises(ValueError_, match="type mismatch"):
        store.resolve(amount.ref, tenant="t1", expected_type="identifier")
    conn.close()


def test_the_wall_and_the_honest_misses(tmp_path):
    conn, store = _store(tmp_path)
    record = store.put("t1", "secret-adjacent", value_type="str")
    # Cross-tenant references are refused, not found-elsewhere.
    with pytest.raises(ValueError_, match="another tenant"):
        store.get(record.ref, tenant="t2")
    with pytest.raises(ValueError_, match="not found"):
        store.get("value://t1/valdeadbeefdeadbeefdead", tenant="t1")
    with pytest.raises(ValueError_, match="not a value reference"):
        store.get("dbvalue://nope", tenant="t1")
    conn.close()


def test_the_binder_resolves_refs_and_names_what_it_cannot(tmp_path):
    conn, store = _store(tmp_path)
    amount = store.put("t1", "125.37", value_type="decimal")
    resolved, provenance = store.resolve_bindings(
        {"amount": {"$ref": amount.ref}, "limit": 3},
        tenant="t1",
    )
    assert resolved == {"amount": "125.37", "limit": 3}
    (line,) = provenance
    assert line["parameter"] == "amount"
    assert line["value_ref"] == amount.ref
    assert line["sha256"]
    # An unhonorable reference names the binding; nothing half-binds.
    with pytest.raises(ValueError_, match="binding 'amount'"):
        store.resolve_bindings(
            {"amount": "value://t1/valmissing0000000000000"}, tenant="t1"
        )
    conn.close()


def test_the_runner_stages_resolved_values_and_blocks_bad_refs(tmp_path):
    from oolu.cache.store import LocalScriptCache
    from oolu.runtime.backend import StubBackend, make_success
    from oolu.runtime.script_node import NodeScriptRunner
    from oolu.skills.models import ActionEvent, ExecutionStatus

    conn, store = _store(tmp_path)
    amount = store.put("t1", "125.370", value_type="decimal")
    backend = StubBackend([make_success({"ok": True})])
    runner = NodeScriptRunner(
        backend,
        LocalScriptCache(":memory:"),
        value_resolver=lambda b, tenant: store.resolve_bindings(
            b, tenant=tenant
        ),
    )

    def _action(bindings):
        return ActionEvent(
            correlation_id="fn",
            adapter="script",
            operation="run",
            parameters={
                "goal": "pay",
                "script": (
                    "from _oolu_runtime import emit_result\nimport json\n"
                    "emit_result(json.load(open('bindings.json'))['amount'])"
                ),
                "bindings": bindings,
                "_value_tenant": "t1",
            },
        )

    outcome = runner.execute(
        _action({"amount": {"$ref": amount.ref}}), idempotency_key="run-1"
    )
    assert outcome.status is ExecutionStatus.SUCCEEDED
    staged = json.loads(backend.requests[-1].files["bindings.json"])
    assert staged == {"amount": "125.370"}  # scale preserved, verbatim

    # A reference from another tenant blocks the run in words.
    blocked = runner.execute(
        _action({"amount": {"$ref": "value://t2/valdeadbeef00000000dead"}}),
        idempotency_key="run-2",
    )
    assert blocked.status is ExecutionStatus.BLOCKED
    assert "unresolved value reference" in (blocked.error or "")
    conn.close()


def test_the_renderer_puts_exact_values_into_model_shaped_text(tmp_path):
    conn, store = _store(tmp_path)
    refund = store.put("t1", "R98765", value_type="identifier")
    amount = store.put("t1", "125.370", value_type="decimal")
    currency = store.put("t1", "usd", value_type="currency")
    text = render_segments(
        [
            {"type": "text", "content": "Refund "},
            {"type": "value", "ref": refund.ref, "format": "identifier"},
            {"type": "text", "content": " was submitted for "},
            {"type": "value", "ref": amount.ref, "format": "decimal_exact"},
            {"type": "text", "content": " "},
            {"type": "value", "ref": currency.ref, "format": "currency_code"},
            {"type": "text", "content": "."},
        ],
        store=store,
        tenant="t1",
    )
    assert text == "Refund R98765 was submitted for 125.370 USD."
    # A missing reference refuses — the renderer never fabricates.
    with pytest.raises(ValueError_, match="not found"):
        render_segments(
            [{"type": "value", "ref": "value://t1/valmissing0000000000000",
              "format": "raw"}],
            store=store,
            tenant="t1",
        )
    # Only registered formatters render.
    with pytest.raises(ValueError_, match="unknown formatter"):
        render_segments(
            [{"type": "value", "ref": refund.ref, "format": "creative"}],
            store=store,
            tenant="t1",
        )
    conn.close()


def test_the_gateway_files_run_outputs_and_renders_them(tmp_path):
    app, conn, ident = _app(tmp_path)
    gateway = GatewayApp(
        app._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        values=ValueStore(conn),
    )
    try:
        submitted = gateway.handle(
            _req(
                "POST", "/v1/runs",
                token=ident.token("user-1", "t1"), body={"intent": "tidy"},
            )
        )
        run_id = submitted.body["run_id"]

        filed = gateway.handle(
            _req(
                "GET", f"/v1/runs/{run_id}/values",
                token=ident.token("user-1", "t1"),
            )
        )
        assert filed.status == 200, filed.body
        # Walled to the submitter, like every run read.
        walled = gateway.handle(
            _req(
                "GET", f"/v1/runs/{run_id}/values",
                token=ident.token("stranger", "t1"),
            )
        )
        assert walled.status == 404

        # Render through the gateway: exact values, session-walled.
        ref = ValueStore(conn).put("t1", "A12345", value_type="identifier").ref
        rendered = gateway.handle(
            _req(
                "POST", "/v1/values/render",
                token=ident.token("user-1", "t1"),
                body={"segments": [
                    {"type": "text", "content": "Order "},
                    {"type": "value", "ref": ref, "format": "identifier"},
                ]},
            )
        )
        assert rendered.status == 200 and rendered.body["text"] == "Order A12345"
        missing = gateway.handle(
            _req(
                "POST", "/v1/values/render",
                token=ident.token("user-1", "t1"),
                body={"segments": [
                    {"type": "value",
                     "ref": "value://t1/valmissing0000000000000",
                     "format": "raw"},
                ]},
            )
        )
        assert missing.status == 422
    finally:
        conn.close()
