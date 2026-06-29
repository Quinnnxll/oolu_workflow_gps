"""Shared pytest fixtures and helpers for the Workflow-GPS suite.

Centralizes the test doubles (StubBackend scenarios, FakeGateway scripts, in-memory
knowledge) and the capability gates so individual test files stay focused on
assertions rather than setup.
"""

from __future__ import annotations

import shutil

import pytest

from workflow_gps.models import ErrorClass, ErrorRecord, ExecutionResult
from workflow_gps.routing.gateway import FakeGateway
from workflow_gps.runtime.backend import (
    ExecutionRequest,
    StubBackend,
    make_failure,
    make_success,
)


# --------------------------------------------------------------------------- #
# Capability gates — skip cleanly when an optional dependency is absent.       #
# --------------------------------------------------------------------------- #
def _has(spec: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(spec) is not None


def _docker_up() -> bool:
    if not _has("docker"):
        return False
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:  # noqa: BLE001
        return False


HAS_UV = shutil.which("uv") is not None
HAS_LANGGRAPH = _has("langgraph")
DOCKER_UP = _docker_up()


def pytest_collection_modifyitems(config, items):
    """Auto-skip marked tests whose capability is unavailable."""
    skip_uv = pytest.mark.skip(reason="uv binary not on PATH")
    skip_lg = pytest.mark.skip(reason="langgraph not installed")
    skip_dk = pytest.mark.skip(reason="no reachable Docker daemon")
    for item in items:
        if "needs_uv" in item.keywords and not HAS_UV:
            item.add_marker(skip_uv)
        if "needs_langgraph" in item.keywords and not HAS_LANGGRAPH:
            item.add_marker(skip_lg)
        if "needs_docker" in item.keywords and not DOCKER_UP:
            item.add_marker(skip_dk)


# --------------------------------------------------------------------------- #
# Result builders.                                                            #
# --------------------------------------------------------------------------- #
def missing_dep_result(module: str) -> ExecutionResult:
    """A Phase-B failure with a classifiable ModuleNotFoundError for `module`."""
    rec = ErrorRecord.create(
        error_class=ErrorClass.MISSING_DEPENDENCY,
        message=f"No module named '{module}'",
        exception_type="ModuleNotFoundError",
        missing_module=module,
    )
    return make_failure(
        stderr=f"ModuleNotFoundError: No module named '{module}'", error=rec
    )


@pytest.fixture
def success_result():
    return make_success({"ok": True})


# --------------------------------------------------------------------------- #
# Backend / gateway doubles.                                                  #
# --------------------------------------------------------------------------- #
@pytest.fixture
def heal_backend():
    """A StubBackend factory: fails with `module` missing until `package` is in the
    request's dependencies, then succeeds. Returns (backend, builder)."""

    def build(
        module: str, package: str, *, payload=None, attempts: int = 4
    ) -> StubBackend:
        def factory(req: ExecutionRequest) -> ExecutionResult:
            if package in req.dependencies:
                return make_success(payload or {"ok": True})
            return missing_dep_result(module)

        return StubBackend([factory] * attempts)

    return build


@pytest.fixture
def script_gateway():
    """A FakeGateway returning a single python code block wrapping `body`."""

    def build(body: str) -> FakeGateway:
        return FakeGateway([f"```python\n{body}\n```"])

    return build


# --------------------------------------------------------------------------- #
# Knowledge.                                                                   #
# --------------------------------------------------------------------------- #
@pytest.fixture
def mem_knowledge():
    from workflow_gps.knowledge import LocalKnowledgeClient

    client = LocalKnowledgeClient(":memory:")
    yield client
    client.close()


# --------------------------------------------------------------------------- #
# Hand-rolled graph driver — mirrors builder.py topology without LangGraph.    #
# --------------------------------------------------------------------------- #
def drive(nodes, router, state, *, max_steps: int = 40):
    """Run the node/edge cycle exactly as builder.py wires it. Returns
    (final_state, terminal_node, path_trace)."""

    def apply(st, update):
        if not update:
            return st
        merged = dict(update)
        if "error_history" in merged:
            merged["error_history"] = st.error_history + merged["error_history"]
        return st.model_copy(update=merged)

    node, trace = "plan", []
    for _ in range(max_steps):
        trace.append(node)
        state = apply(state, getattr(nodes, node)(state))
        if node == "plan":
            node = "synthesize"
        elif node == "synthesize":
            node = router.after_synthesis(state)
        elif node == "execute":
            node = "classify"
        elif node == "classify":
            node = router.after_classify(state)
        elif node == "recalculate":
            node = router.after_recalculate(state)
        elif node in ("finalize", "halt"):
            return state, node, trace
    raise RuntimeError("graph did not terminate")
