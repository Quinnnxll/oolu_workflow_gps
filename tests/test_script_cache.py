"""Offline coverage for script-cache signatures and graph integration."""

from __future__ import annotations

from conftest import drive

from oolu.cache import (
    LocalScriptCache,
    ScriptCacheSignature,
    make_script_cache_key,
)
from oolu.graph.edges import EdgeRouter
from oolu.graph.nodes import GraphNodes
from oolu.models import ErrorClass, ErrorRecord, GraphState, GraphStatus
from oolu.routing.gateway import FakeGateway
from oolu.runtime.backend import StubBackend, make_failure, make_success


def _signature(**changes) -> ScriptCacheSignature:
    values = {
        "intent": "  Make   A Chart ",
        "prompt_fingerprint": "prompt-a",
        "routing_models": ("fast-a", "reasoning-a"),
        "backend_kind": "docker",
        "backend_image": "sandbox:1",
        "pinned_index_url": "https://packages.example/simple",
    }
    values.update(changes)
    return ScriptCacheSignature(**values)


def test_same_normalized_intent_creates_same_cache_key():
    a = make_script_cache_key(_signature(intent="  Make   A Chart "))
    b = make_script_cache_key(_signature(intent="make a chart"))
    assert a == b


def test_backend_model_and_prompt_change_cache_key():
    original = make_script_cache_key(_signature())
    variants = [
        _signature(backend_kind="subprocess"),
        _signature(backend_image="sandbox:2"),
        _signature(routing_models=("fast-b", "reasoning-a")),
        _signature(prompt_fingerprint="prompt-b"),
    ]
    assert all(make_script_cache_key(item) != original for item in variants)


def test_second_identical_task_skips_gateway_call(tmp_path):
    cache = LocalScriptCache(tmp_path / "scripts.db")
    intent = "return a stable answer"
    first_gateway = FakeGateway(["```python\nprint('cached')\n```"])
    first_nodes = GraphNodes(
        gateway=first_gateway,
        backend=StubBackend([make_success({"ok": True})]),
        script_cache=cache,
        backend_kind="subprocess",
    )
    first, _, _ = drive(
        first_nodes, EdgeRouter(), GraphState(intent=intent, session_id="one")
    )
    assert first.status is GraphStatus.COMPLETED
    assert len(first_gateway.calls) == 1

    second_gateway = FakeGateway([])
    second_nodes = GraphNodes(
        gateway=second_gateway,
        backend=StubBackend([make_success({"ok": True})]),
        script_cache=cache,
        backend_kind="subprocess",
    )
    second, _, _ = drive(
        second_nodes, EdgeRouter(), GraphState(intent=intent, session_id="two")
    )
    assert second.status is GraphStatus.COMPLETED
    assert second.cache_hit is True and second.cache_kind == "script"
    assert len(second_gateway.calls) == 0
    cache.close()


def test_cached_script_failure_increments_failure_count(tmp_path):
    cache = LocalScriptCache(tmp_path / "scripts.db")
    nodes = GraphNodes(
        gateway=FakeGateway([]),
        backend=StubBackend([]),
        script_cache=cache,
        backend_kind="subprocess",
    )
    initial = GraphState(intent="cached failure", session_id="s")
    key = nodes.synthesize(initial)["cache_key"]
    cache.store_success(
        key, script="raise ValueError", dependencies=[], tier="fast", model="m"
    )

    synthesized = initial.model_copy(update=nodes.synthesize(initial))
    failure = make_failure(
        stderr="ValueError: broken",
        error=ErrorRecord.create(
            error_class=ErrorClass.RUNTIME_EXCEPTION,
            message="broken",
            exception_type="ValueError",
        ),
    )
    classified = synthesized.model_copy(update={"last_result": failure})
    updates = nodes.classify(classified)

    assert updates["cache_status"] == "failed"
    assert cache.get(key).failure_count == 1
    cache.record_failure(key)
    assert cache.get(key) is None
    cache.close()
