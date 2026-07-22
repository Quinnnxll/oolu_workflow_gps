"""Typed output ports and value lineage: the declared contract binds runs.

Exit gate: a node's declared output ports are held against every
successful payload before it is trusted or cached (a mocked answer that
skips its ports fails with the gap named, and the repair loop hears it
in words); the node-function route carries the WHOLE stamped function
into the runner (bundle, tenant wall, bindings, ports); a completed run
files per-port snapshots the port index points at, so an
``output://node/port`` edge resolves downstream to the real answer; and
lineage links each output value to the exact input values it was
computed from — both directions, walled per tenant.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from test_http_gateway import _app, _req

from oolu.cache.store import LocalScriptCache
from oolu.durable.connection import DurableConnection
from oolu.gateway import GatewayApp
from oolu.orchestrator.engine import WorkflowOrchestrator
from oolu.orchestrator.state import Phase
from oolu.runtime.backend import StubBackend, make_success
from oolu.runtime.contract import output_port_problems
from oolu.runtime.script_node import NodeScriptRunner
from oolu.skills.models import ActionEvent, ExecutionStatus
from oolu.values import ValueError_, ValueStore

SCRIPT = "from _oolu_runtime import emit_result\nemit_result('x')"


def _store(tmp_path):
    conn = DurableConnection(tmp_path / "v.db")
    return conn, ValueStore(conn)


def _action(ports=None, bindings=None):
    parameters = {"goal": "compute", "script": SCRIPT, "node_key": "node:fn-1"}
    if ports is not None:
        parameters["_output_ports"] = ports
    if bindings is not None:
        parameters["bindings"] = bindings
        parameters["_value_tenant"] = "t1"
    return ActionEvent(
        correlation_id="fn", adapter="script", operation="run",
        parameters=parameters,
    )


# --------------------------------------------------------------------- #
# The validator itself: every gap named, nothing invented.              #
# --------------------------------------------------------------------- #
def test_output_port_problems_names_every_gap():
    # No declaration, no validation — legacy nodes keep working.
    assert output_port_problems({"whatever": 1}, []) == []
    # One declared port matches the wrapped scalar emit_result produces.
    assert output_port_problems({"result": "answer"},
                                [{"name": "summary", "type": "str"}]) == []
    # Named ports with types, honored.
    assert output_port_problems(
        {"subtotal": 12.5, "note": "ok", "exact": "125.370"},
        [
            {"name": "subtotal", "type": "number"},
            {"name": "note", "type": "str"},
            {"name": "exact", "type": "decimal"},
        ],
    ) == []
    # A missing declared port is named.
    (problem,) = output_port_problems(
        {"note": "done"}, [{"name": "subtotal", "type": "number"}]
    )
    assert "declared output port 'subtotal' is missing" in problem
    # A mistyped value is named — and a bool is NOT a number.
    (problem,) = output_port_problems(
        {"subtotal": "12.5"}, [{"name": "subtotal", "type": "number"}]
    )
    assert "declares type number" in problem
    (problem,) = output_port_problems(
        {"subtotal": True}, [{"name": "subtotal", "type": "number"}]
    )
    assert "declares type number" in problem
    # Two declared ports cannot hide behind one wrapped scalar.
    problems = output_port_problems(
        "just words",
        [{"name": "tax", "type": "decimal"}, {"name": "total", "type": "decimal"}],
    )
    assert len(problems) == 2


# --------------------------------------------------------------------- #
# The runner holds every success against the declaration.               #
# --------------------------------------------------------------------- #
def test_the_runner_refuses_a_success_that_skips_declared_ports():
    ports = [{"name": "subtotal", "type": "number"}]
    # A "successful" run whose payload skips the declared port FAILS —
    # the exact shape a mocked function takes — and is never cached.
    backend = StubBackend([make_success({"note": "done"})])
    runner = NodeScriptRunner(backend, LocalScriptCache(":memory:"))
    outcome = runner.execute(_action(ports=ports), idempotency_key="r1")
    assert outcome.status is ExecutionStatus.FAILED
    assert "output_contract_violation" in (outcome.error or "")
    # The honored contract passes untouched.
    backend = StubBackend([make_success({"subtotal": 12.5})])
    runner = NodeScriptRunner(backend, LocalScriptCache(":memory:"))
    outcome = runner.execute(_action(ports=ports), idempotency_key="r2")
    assert outcome.status is ExecutionStatus.SUCCEEDED
    # And with no declaration, the old behaviour stands exactly.
    backend = StubBackend([make_success({"note": "done"})])
    runner = NodeScriptRunner(backend, LocalScriptCache(":memory:"))
    outcome = runner.execute(_action(), idempotency_key="r3")
    assert outcome.status is ExecutionStatus.SUCCEEDED


def test_the_repair_loop_hears_the_output_contract_gap():
    heard: list[str] = []

    class Repairer:
        def synthesize(self, goal, *, session_id):
            return None

        def repair(self, goal, script, error):
            heard.append(error)
            return script + "\n# repaired"

    # First run skips the port; the repaired run honors it.
    backend = StubBackend(
        [make_success({"note": "done"}), make_success({"subtotal": 3})]
    )
    runner = NodeScriptRunner(
        backend, LocalScriptCache(":memory:"), synthesizer=Repairer()
    )
    outcome = runner.execute(
        _action(ports=[{"name": "subtotal", "type": "number"}]),
        idempotency_key="r1",
    )
    assert outcome.status is ExecutionStatus.SUCCEEDED
    assert outcome.evidence.get("repair_rounds") == 1
    # The model was told the gap in words it can act on.
    assert "output_contract_violation" in heard[0]
    assert "subtotal" in heard[0]


# --------------------------------------------------------------------- #
# The route carries the WHOLE stamped function.                          #
# --------------------------------------------------------------------- #
def test_the_node_function_route_carries_every_stamped_key():
    function = {
        "node_id": "node-a",
        "skill_id": "fn-1",
        "title": "compute",
        "goal": "compute",
        "script": SCRIPT,
        "node_key": "node:fn-1",
        "bundle": "b" * 64,
        "bindings": {"amount": "value://t1/valabc"},
        "_value_tenant": "t1",
        "_output_ports": [{"name": "subtotal", "type": "number"}],
        "_egress_open": True,
    }
    state = SimpleNamespace(
        contract=SimpleNamespace(
            metadata={"node_function": function}, intent="compute"
        )
    )
    route = WorkflowOrchestrator._node_function_route(state)
    assert route is not None
    parameters = route.chosen.actions[0].action.parameters
    # Nothing the gateway stamped is dropped on the way to the runner:
    # the frozen src tree, the tenant wall, the bindings, the ports.
    for key in ("bundle", "bindings", "_value_tenant", "_output_ports",
                "_egress_open"):
        assert parameters[key] == function[key], key


# --------------------------------------------------------------------- #
# Port edges and lineage in the value store.                             #
# --------------------------------------------------------------------- #
def test_output_edges_resolve_through_the_port_index(tmp_path):
    conn, store = _store(tmp_path)
    refs = store.snapshot_outputs(
        "t1", {"subtotal": "12.50", "count": 3}, producer="node-a"
    )
    assert store.port_ref("t1", "node-a", "subtotal") == refs["subtotal"]
    assert store.ports_of("t1", "node-a") == refs
    # The edge form resolves to the exact stored value, with the edge
    # kept in the provenance next to the value it resolved to.
    resolved, provenance = store.resolve_bindings(
        {"subtotal": "output://node-a/subtotal", "limit": 5}, tenant="t1"
    )
    assert resolved == {"subtotal": "12.50", "limit": 5}
    (line,) = provenance
    assert line["port_source"] == "output://node-a/subtotal"
    assert line["value_ref"] == refs["subtotal"]
    # A retry pointing the port at the same value stays one row.
    again = store.snapshot_outputs(
        "t1", {"subtotal": "12.50"}, producer="node-a"
    )
    assert again["subtotal"] == refs["subtotal"]
    # An empty port is an honest miss, walled per tenant.
    with pytest.raises(ValueError_, match="has not produced"):
        store.resolve_bindings(
            {"tax": "output://node-a/tax"}, tenant="t1"
        )
    with pytest.raises(ValueError_, match="binding 'subtotal'"):
        store.resolve_bindings(
            {"subtotal": "output://node-a/subtotal"}, tenant="t2"
        )
    conn.close()


def test_lineage_links_outputs_to_their_inputs(tmp_path):
    conn, store = _store(tmp_path)
    price = store.put("t1", "39.99", value_type="decimal")
    refs = store.snapshot_outputs(
        "t1", {"subtotal": "119.97"}, producer="node-a"
    )
    filed = store.record_lineage(
        "t1", "node-a", [price.ref, "not-a-ref"], list(refs.values())
    )
    assert filed == 1  # the literal has no lineage
    # Both directions answer, naming the node that did the work.
    chain = store.lineage("t1", refs["subtotal"])
    assert chain["inputs"] == [{"value_ref": price.ref, "node": "node-a"}]
    back = store.lineage("t1", price.ref)
    assert back["outputs"] == [
        {"value_ref": refs["subtotal"], "node": "node-a"}
    ]
    # Idempotent: a retry files the same rows once.
    store.record_lineage("t1", "node-a", [price.ref], list(refs.values()))
    assert len(store.lineage("t1", refs["subtotal"])["inputs"]) == 1
    # An unknown reference answers empty, never an invention.
    assert store.lineage("t1", "value://t1/valnothing0000000000000") == {
        "inputs": [], "outputs": [],
    }
    conn.close()


# --------------------------------------------------------------------- #
# The gateway files ports and lineage when a node-function run lands.    #
# --------------------------------------------------------------------- #
def test_completion_files_ports_and_lineage(tmp_path):
    app, conn, ident = _app(tmp_path)
    store = ValueStore(conn)
    gateway = GatewayApp(
        app._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        values=store,
    )
    try:
        price = store.put("t1", "39.99", value_type="decimal")
        state = SimpleNamespace(
            run_id="run-1",
            phase=Phase.COMPLETED,
            contract=SimpleNamespace(
                metadata={
                    "tenant_id": "t1",
                    "node_function": {"node_id": "node-a", "skill_id": "fn-1"},
                },
                submitted_by="user-1",
            ),
            execution=SimpleNamespace(
                action_outcomes=[
                    SimpleNamespace(
                        status=ExecutionStatus.SUCCEEDED,
                        skill_id="fn-1",
                        evidence={
                            "result": {"subtotal": "119.97"},
                            "value_provenance": [
                                {"parameter": "price", "value_ref": price.ref}
                            ],
                        },
                    )
                ]
            ),
        )
        gateway._file_run_values(state)
        # The port index now answers for the node, and the lineage names
        # the exact input the output was computed from.
        ref = store.port_ref("t1", "node-a", "subtotal")
        assert ref is not None
        assert store.resolve(ref, tenant="t1") == "119.97"
        assert store.lineage("t1", ref)["inputs"] == [
            {"value_ref": price.ref, "node": "node-a"}
        ]
        # Downstream, the edge is enough — no value ever retyped.
        resolved, _ = store.resolve_bindings(
            {"subtotal": "output://node-a/subtotal"}, tenant="t1"
        )
        assert resolved == {"subtotal": "119.97"}
    finally:
        conn.close()


def test_the_run_lineage_endpoint_is_walled(tmp_path):
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
        answered = gateway.handle(
            _req(
                "GET", f"/v1/runs/{run_id}/lineage",
                token=ident.token("user-1", "t1"),
            )
        )
        assert answered.status == 200
        assert answered.body["run_id"] == run_id
        walled = gateway.handle(
            _req(
                "GET", f"/v1/runs/{run_id}/lineage",
                token=ident.token("stranger", "t1"),
            )
        )
        assert walled.status == 404
    finally:
        conn.close()
