"""W0 — wire the dataflow: compile auto-bind, identity stamping, the value
pipe, and the SlotIndex. Fresh data crosses one contract run."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmarks"))

from route_scale import chain_goal, marketplace  # noqa: E402

from test_http_gateway import _app, _req  # noqa: E402

from oolu.gateway import GatewayApp  # noqa: E402
from oolu.metering.attribution import AttributionStore  # noqa: E402
from oolu.nodeplace import (  # noqa: E402
    CandidateAssembler,
    LiveVersionStats,
    NodeplaceService,
    PriceBook,
    RatingService,
    RatingStore,
    RegistryStore,
    compile_contract,
    stamp_value_tenant,
)
from oolu.metering.store import MeteringLedger  # noqa: E402
from oolu.orchestrator import (  # noqa: E402
    Blueprint,
    ContractAssembler,
    DagRouteRunner,
    GoalSpec,
    ReservedAction,
    RoutePlan,
)
from oolu.skills import SlotIndex  # noqa: E402
from oolu.skills.contract import (  # noqa: E402
    ActionsBody,
    NodeContract,
    ScriptBody,
    Slot,
    SubgraphBody,
)
from oolu.skills.models import ActionEvent, ExecutionOutcome, ExecutionStatus  # noqa: E402
from oolu.values import ValueStore  # noqa: E402


def _slot(name: str, role: str | None = None) -> Slot:
    return Slot(name=name, value_type="str", role=role)


def _script_child(
    name: str,
    *,
    consumes: list[Slot] | None = None,
    produces: list[Slot] | None = None,
    bindings: dict | None = None,
) -> NodeContract:
    return NodeContract(
        name=name,
        provenance="synthesized",
        consumes=list(consumes or []),
        produces=list(produces or []),
        body=ScriptBody(goal=f"goal:{name}", bindings=dict(bindings or {})),
    )


def _subgraph(name: str, children: list[NodeContract]) -> NodeContract:
    return NodeContract(
        name=name,
        provenance="synthesized",
        body=SubgraphBody(nodes=children),
    )


def _script_bindings(blueprint: Blueprint) -> dict[str, dict]:
    """goal -> compiled bindings, for every script action."""
    return {
        str(item.action.parameters.get("goal")): dict(
            item.action.parameters.get("bindings") or {}
        )
        for item in blueprint.actions
        if item.action.adapter == "script"
    }


# --------------------------------------------------------------------------- #
# Compile-level auto-binding.                                                  #
# --------------------------------------------------------------------------- #
def test_wire_dataflow_binds_consumer_to_producer_port():
    producer = _script_child("exporter", produces=[_slot("rows_csv")])
    consumer = _script_child("cleaner", consumes=[_slot("rows_csv")])
    contract = _subgraph("pipeline", [producer, consumer])

    wired = compile_contract(contract, wire_dataflow=True)
    bindings = _script_bindings(wired.blueprint)
    assert bindings["goal:cleaner"] == {
        "rows_csv": f"output://{producer.id}/rows_csv"
    }
    assert bindings["goal:exporter"] == {}


def test_wire_dataflow_uses_the_canonical_producer_key_when_mapped():
    producer = _script_child("exporter", produces=[_slot("rows_csv")])
    consumer = _script_child("cleaner", consumes=[_slot("rows_csv")])
    contract = _subgraph("pipeline", [producer, consumer])

    wired = compile_contract(
        contract,
        wire_dataflow=True,
        producer_keys={producer.id: "desk-node-7"},
    )
    bindings = _script_bindings(wired.blueprint)
    assert bindings["goal:cleaner"] == {"rows_csv": "output://desk-node-7/rows_csv"}


def test_wire_dataflow_off_by_default_leaves_bindings_byte_identical():
    producer = _script_child("exporter", produces=[_slot("rows_csv")])
    consumer = _script_child("cleaner", consumes=[_slot("rows_csv")])
    contract = _subgraph("pipeline", [producer, consumer])

    plain = compile_contract(contract)
    assert _script_bindings(plain.blueprint)["goal:cleaner"] == {}


def test_wire_dataflow_never_binds_actionsbody_producers():
    # An ActionsBody outcome need not carry a result payload — an edge
    # whose port can never fill would refuse honest runs (v1 exclusion).
    producer = NodeContract(
        name="recorded exporter",
        provenance="demonstrated",
        produces=[_slot("rows_csv")],
        body=ActionsBody(
            actions=[
                ActionEvent(correlation_id="c", adapter="cli", operation="run")
            ]
        ),
    )
    consumer = _script_child("cleaner", consumes=[_slot("rows_csv")])
    contract = _subgraph("pipeline", [producer, consumer])

    wired = compile_contract(contract, wire_dataflow=True)
    assert _script_bindings(wired.blueprint)["goal:cleaner"] == {}


def test_wire_dataflow_leaves_ambiguous_and_bound_slots_alone():
    producer_a = _script_child("a", produces=[_slot("rows_csv")])
    producer_b = _script_child("b", produces=[_slot("rows_csv")])
    consumer = _script_child(
        "cleaner",
        consumes=[_slot("rows_csv"), _slot("mode")],
        bindings={"mode": "strict"},
    )
    provider = _script_child("modes", produces=[_slot("mode")])
    contract = _subgraph("pipeline", [producer_a, producer_b, consumer, provider])

    wired = compile_contract(contract, wire_dataflow=True)
    bindings = _script_bindings(wired.blueprint)["goal:cleaner"]
    # Two rival producers of rows_csv: ambiguity stays unbound, as today.
    assert "rows_csv" not in bindings
    # An already-bound slot is never overwritten.
    assert bindings["mode"] == "strict"


# --------------------------------------------------------------------------- #
# Identity stamping at submission.                                             #
# --------------------------------------------------------------------------- #
def test_stamp_value_tenant_stamps_script_actions_only():
    producer = _script_child("exporter", produces=[_slot("rows_csv")])
    recorded = NodeContract(
        name="recorded",
        provenance="demonstrated",
        body=ActionsBody(
            actions=[
                ActionEvent(correlation_id="c", adapter="cli", operation="run")
            ]
        ),
    )
    contract = _subgraph("pipeline", [producer, recorded])
    compiled = compile_contract(contract)
    stamped = stamp_value_tenant(compiled, "tenant-9")

    by_adapter = {
        item.action.adapter: item.action.parameters
        for item in stamped.blueprint.actions
    }
    assert by_adapter["script"]["_value_tenant"] == "tenant-9"
    assert "_value_tenant" not in by_adapter["cli"]
    # Owners survive the stamp; an empty tenant is a no-op.
    assert stamped.owners == compiled.owners
    assert stamp_value_tenant(compiled, "") is compiled


# --------------------------------------------------------------------------- #
# The per-run value pipe on the DAG runner.                                    #
# --------------------------------------------------------------------------- #
class _StubExecutor:
    name = "stub"

    def __init__(self, fail: set[str] | None = None):
        self._fail = set(fail or ())

    def capabilities(self):
        return frozenset({"a", "b", "c"})

    def execute(self, action, *, idempotency_key):
        ok = action.operation not in self._fail
        now = datetime.now(UTC)
        return ExecutionOutcome(
            idempotency_key=idempotency_key,
            skill_id="s",
            status=ExecutionStatus.SUCCEEDED if ok else ExecutionStatus.FAILED,
            evidence={"result": {"op": action.operation}},
            error=None if ok else "boom",
            started_at=now,
            completed_at=now,
        )

    def cancel(self, idempotency_key):
        return None


def _blueprint(*ops: str) -> Blueprint:
    return Blueprint(
        name="piped",
        actions=[
            ReservedAction(
                action=ActionEvent(correlation_id="c", adapter="stub", operation=op)
            )
            for op in ops
        ],
    )


def test_value_pipe_fires_per_succeeded_settle_and_never_wedges_the_route():
    seen: list[tuple[str, str]] = []

    def pipe(action_id: str, outcome: ExecutionOutcome) -> None:
        seen.append((action_id, outcome.evidence["result"]["op"]))
        raise RuntimeError("filing hiccup")  # best-effort: must not wedge

    blueprint = _blueprint("a", "b")
    record = DagRouteRunner({"stub": _StubExecutor()}).execute(
        RoutePlan(chosen=blueprint, alternatives=[], total_cost=0.0),
        idempotency_key="k",
        attempt=1,
        value_pipe=pipe,
    )
    assert record.status is ExecutionStatus.SUCCEEDED
    assert [op for _, op in seen] == ["a", "b"]


def test_value_pipe_skips_failed_and_cancelled_settles():
    seen: list[str] = []
    blueprint = _blueprint("a", "b", "c")
    record = DagRouteRunner({"stub": _StubExecutor(fail={"b"})}).execute(
        RoutePlan(chosen=blueprint, alternatives=[], total_cost=0.0),
        idempotency_key="k",
        attempt=1,
        value_pipe=lambda action_id, outcome: seen.append(
            outcome.evidence["result"]["op"]
        ),
    )
    assert record.status is ExecutionStatus.FAILED
    assert seen == ["a"]  # b failed, c cascaded — neither filed


# --------------------------------------------------------------------------- #
# SlotIndex — behavior-identical candidate lookup.                             #
# --------------------------------------------------------------------------- #
def test_slot_index_matches_the_scan_exactly_including_role_asymmetry():
    library = [
        _script_child("plain", produces=[_slot("rows")]),
        _script_child("pathy", produces=[_slot("rows", role="path")]),
        _script_child("other", produces=[_slot("cols")]),
        _script_child("wants-rows", consumes=[_slot("rows")]),
    ]
    index = SlotIndex(library)

    for wanted in (_slot("rows"), _slot("rows", role="path"), _slot("nope")):
        scan = [
            c
            for c in library
            if any(p.matches(wanted) for p in c.produces)
        ]
        assert index.producers(wanted) == scan

    # Consumers: the produced slot satisfies the consumed one.
    assert index.consumers(_slot("rows")) == [library[3]]
    assert index.consumers(_slot("rows", role="path")) == [library[3]]


def test_slot_index_parity_on_the_route_scale_marketplace():
    library = marketplace(depth=5, width=6, noise=200)
    goal: GoalSpec = chain_goal(5)

    plain = ContractAssembler(library).assemble(goal)
    indexed = ContractAssembler(library, index=SlotIndex(library)).assemble(goal)

    assert plain.selected == indexed.selected
    assert plain.gap_filled == indexed.gap_filled
    assert [s.name for s in plain.missing] == [s.name for s in indexed.missing]
    if plain.contract is not None:
        assert indexed.contract is not None
        assert [c.id for c in plain.contract.body.nodes] == [
            c.id for c in indexed.contract.body.nodes
        ]


# --------------------------------------------------------------------------- #
# The loop-closure: fresh data crosses one contract run, through the door.     #
# --------------------------------------------------------------------------- #
class _ScriptResolverStub:
    """Stands in for the sandbox ONLY: resolves bindings through the real
    ValueStore binder (tenant wall included) exactly as NodeScriptRunner
    does, records what was staged, and emits a scripted payload."""

    name = "script"

    def __init__(self, values: ValueStore, payloads: dict[str, list[dict]]):
        self._values = values
        self._payloads = {k: list(v) for k, v in payloads.items()}
        self.staged: dict[str, list[dict]] = {}
        self.provenance: dict[str, list[dict]] = {}

    def capabilities(self):
        return frozenset({"run"})

    def execute(self, action, *, idempotency_key):
        params = action.parameters
        goal = str(params.get("goal"))
        tenant = str(params.get("_value_tenant") or "")
        now = datetime.now(UTC)
        try:
            resolved, provenance = self._values.resolve_bindings(
                dict(params.get("bindings") or {}), tenant=tenant
            )
        except Exception as exc:  # the runner's honest-refusal shape
            return ExecutionOutcome(
                idempotency_key=idempotency_key,
                skill_id="s",
                status=ExecutionStatus.BLOCKED,
                error=f"unresolved value reference: {exc}",
                started_at=now,
                completed_at=now,
            )
        self.staged.setdefault(goal, []).append(resolved)
        self.provenance.setdefault(goal, []).extend(provenance)
        series = self._payloads.get(goal) or [{}]
        payload = series.pop(0) if len(series) > 1 else series[0]
        return ExecutionOutcome(
            idempotency_key=idempotency_key,
            skill_id="s",
            status=ExecutionStatus.SUCCEEDED,
            evidence={"result": payload, "value_provenance": provenance},
            started_at=now,
            completed_at=now,
        )

    def cancel(self, idempotency_key):
        return None


def _values_app(tmp_path, executor):
    base, conn, ident = _app(tmp_path)
    registry = RegistryStore(conn)
    metering = MeteringLedger(conn)
    attribution = AttributionStore(conn)
    ratings = RatingService(
        RatingStore(conn), verified_run=metering.verified_run
    )
    assembler = CandidateAssembler(
        registry=registry,
        stats=LiveVersionStats(
            metering=metering, audit=base._durable.audit, attribution=attribution
        ),
        ratings=ratings,
    )
    values = ValueStore(conn)
    app = GatewayApp(
        base._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        nodeplace=NodeplaceService(registry),
        ratings=ratings,
        market=assembler,
        price_book=PriceBook(tmp_path / "prices.db"),
        attribution=attribution,
        contract_executors={"script": executor},
        values=values,
    )
    return app, conn, ident, values


def test_fresh_data_crosses_one_contract_run_with_no_human_handoff(tmp_path):
    producer = _script_child("exporter", produces=[_slot("rows_csv")])
    consumer = _script_child("cleaner", consumes=[_slot("rows_csv")])
    contract = _subgraph("pipeline", [producer, consumer])

    executor = _ScriptResolverStub(
        None,  # bound below, after the app owns the store
        {
            "goal:exporter": [
                {"rows_csv": "run-one rows"},
                {"rows_csv": "run-two rows"},
            ],
            "goal:cleaner": [{"tidy_csv": "tidy"}],
        },
    )
    app, conn, ident, values = _values_app(tmp_path, executor)
    executor._values = values

    def run(key: str):
        resp = app.handle(
            _req(
                "POST",
                "/v1/runs/contract",
                token=ident.token("consumer", "t2"),
                body={"contract": contract.model_dump(mode="json")},
                headers={"Idempotency-Key": key},
            )
        )
        assert resp.status == 200, resp.body
        assert resp.body["status"] == "succeeded"
        return resp

    run("w0-1")
    # The consumer's staged bindings hold the producer's FRESH payload —
    # resolved mid-run, no human handoff, no stale value.
    assert executor.staged["goal:cleaner"][0] == {"rows_csv": "run-one rows"}
    # Provenance cites the port edge the compiler wrote.
    port_lines = [
        line
        for line in executor.provenance["goal:cleaner"]
        if line.get("port_source")
    ]
    assert port_lines and port_lines[0]["port_source"] == (
        f"output://{producer.id}/rows_csv"
    )
    # The port index holds the producer's answer under the canonical key.
    ref = values.port_ref("t2", producer.id, "rows_csv")
    assert ref is not None
    assert values.resolve(ref, tenant="t2") == "run-one rows"

    # Run again: the consumer reads run TWO's answer — fresh every run.
    run("w0-2")
    assert executor.staged["goal:cleaner"][1] == {"rows_csv": "run-two rows"}
    assert (
        values.resolve(
            values.port_ref("t2", producer.id, "rows_csv"), tenant="t2"
        )
        == "run-two rows"
    )
    conn.close()
