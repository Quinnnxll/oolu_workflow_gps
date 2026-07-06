"""Unit tests for the graph nodes, composed with edges via the conftest driver."""

from __future__ import annotations

from conftest import drive

from oolu.graph.edges import EdgeRouter
from oolu.graph.nodes import GraphNodes
from oolu.models import (
    ErrorClass,
    ErrorRecord,
    GraphState,
    GraphStatus,
    ModelTier,
)
from oolu.routing.gateway import FakeGateway, GatewayError
from oolu.runtime.backend import (
    StubBackend,
    make_failure,
)

ROUTER = EdgeRouter()
PY = "```python\nimport cowsay\n```"


def _run(gateway, backend):
    nodes = GraphNodes(gateway=gateway, backend=backend)
    return drive(nodes, ROUTER, GraphState(intent="t", session_id="s"))


def test_self_heal_reruns_without_resynthesis(heal_backend):
    gw = FakeGateway([PY])  # only one completion
    final, terminal, trace = _run(gw, heal_backend("cowsay", "cowsay"))
    assert terminal == "finalize" and final.status is GraphStatus.COMPLETED
    assert len(gw.calls) == 1  # dep-heal re-ran execute, not synthesize
    assert trace.count("execute") == 2 and trace.count("synthesize") == 1


def test_exhaustion_escalates_then_halts():
    final, terminal, _ = _run(FakeGateway(["nope"] * 12), StubBackend([]))
    assert terminal == "halt" and final.current_tier is ModelTier.REASONING
    assert "exhausted" in final.failure_reason


def test_halting_class_immediate_halt():
    auth = ErrorRecord.create(
        error_class=ErrorClass.AUTH_FAILURE, message="401", exception_type="HTTPError"
    )
    be = StubBackend([make_failure(stderr="HTTPError: 401 Unauthorized", error=auth)])
    final, terminal, _ = _run(FakeGateway([PY]), be)
    assert terminal == "halt" and final.recalc_count == 0
    assert "unrecoverable auth_failure" in final.failure_reason


def test_gateway_transport_failure_halts():
    final, terminal, _ = _run(FakeGateway([GatewayError("refused")]), StubBackend([]))
    assert terminal == "halt" and "gateway error" in final.failure_reason
    assert final.recalc_count == 0
