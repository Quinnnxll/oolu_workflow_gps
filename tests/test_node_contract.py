"""NodeContract: the unified schema, its converters, and its compilation."""

from __future__ import annotations

import pytest
from test_dag_scheduler import StubExecutor

from oolu.cache import LocalScriptCache
from oolu.models import ExecutionResult, Phase
from oolu.orchestrator import DagRouteRunner, RoutePlan, contract_to_blueprint
from oolu.runtime import NodeScriptRunner, NodeSynthesis, StubBackend
from oolu.skills import (
    ActionsBody,
    ContractEdge,
    NodeContract,
    NodeStats,
    ScriptBody,
    Slot,
    SubgraphBody,
    derive_data_edges,
)
from oolu.skills.models import (
    ActionEvent,
    ConstraintSpec,
    ExecutionStatus,
    ReusableSkill,
    SkillParameter,
    SkillSignature,
)


def _actions_contract(name, *operations, consumes=(), produces=(), fallback=None):
    return NodeContract(
        id=f"node.{name}",
        name=name,
        consumes=list(consumes),
        produces=list(produces),
        body=ActionsBody(
            actions=[
                ActionEvent(correlation_id="c", adapter="stub", operation=op)
                for op in operations
            ]
        ),
        fallback=fallback,
    )


def _route(blueprint):
    return RoutePlan(chosen=blueprint, alternatives=[], total_cost=0.0)


# --------------------------------------------------------------------------- #
# The unified schema and its converters.                                       #
# --------------------------------------------------------------------------- #
def test_skill_round_trips_through_the_contract():
    skill = ReusableSkill(
        id="learned.convert",
        name="convert",
        description="convert a file",
        signature=SkillSignature(application="cli", adapter="cli"),
        parameters=[
            SkillParameter(name="argv_1", value_type="path", domain={"role": "path"})
        ],
        actions=[ActionEvent(correlation_id="c", adapter="cli", operation="run")],
        validators=[
            ConstraintSpec(
                id="artifacts",
                description="",
                validator="workspace.expected_artifacts",
                evidence={"expected_files": ["out/report.csv"]},
            )
        ],
        demonstration_ids=["d1", "d2"],
    )
    contract = NodeContract.from_skill(skill, stats=NodeStats(successes=5))
    assert contract.id == "learned.convert"
    (slot,) = contract.consumes
    assert slot == Slot(name="argv_1", value_type="path", role="path")
    # Artifact validators imply produced slots.
    assert contract.produces == [
        Slot(name="out/report.csv", value_type="path", role="path")
    ]
    assert contract.stats is not None and contract.stats.success_mean > 0.5

    back = contract.to_skill(signature=skill.signature)
    assert back.id == skill.id
    assert back.actions == skill.actions
    assert back.validators == skill.validators
    assert [p.name for p in back.parameters] == ["argv_1"]
    assert back.parameters[0].domain == {"role": "path"}


def test_only_actions_bodies_round_trip_to_skills():
    contract = NodeContract(
        name="synth", body=ScriptBody(goal="do it"), provenance="synthesized"
    )
    with pytest.raises(ValueError, match="actions-bodied"):
        contract.to_skill()


def test_contract_risk_is_the_worst_operation_class():
    assert _actions_contract("reader", "list_files").risk == "read"
    assert _actions_contract("writer", "list_files", "send").risk == "write"
    assert _actions_contract("nuke", "get", "delete_all").risk == "irreversible"
    assert NodeContract(name="s", body=ScriptBody(goal="g")).risk == "write"


def test_data_edges_derive_from_slot_unification():
    export = _actions_contract(
        "export", "export", produces=[Slot(name="raw", value_type="path")]
    )
    clean = _actions_contract(
        "clean",
        "clean",
        consumes=[Slot(name="raw", value_type="path")],
        produces=[Slot(name="tidy", value_type="path")],
    )
    unrelated = _actions_contract("audit", "audit")
    edges = derive_data_edges([export, clean, unrelated])
    assert [(e.source, e.target, e.provenance) for e in edges] == [
        ("node.export", "node.clean", "data")
    ]
    # A name match with a type mismatch is NOT a dependency.
    assert not Slot(name="raw", value_type="path").matches(
        Slot(name="raw", value_type="int")
    )


# --------------------------------------------------------------------------- #
# Compilation: one contract -> one executable blueprint.                       #
# --------------------------------------------------------------------------- #
def test_subgraph_compiles_to_a_parallel_dag_and_executes():
    """Slot flow alone produces the fan-out/fan-in the prototype hardcoded."""
    export = _actions_contract(
        "export", "export", produces=[Slot(name="raw", value_type="path")]
    )
    clean = _actions_contract(
        "clean",
        "clean",
        consumes=[Slot(name="raw", value_type="path")],
        produces=[Slot(name="tidy", value_type="path")],
    )
    audit = _actions_contract(
        "audit",
        "audit",
        consumes=[Slot(name="raw", value_type="path")],
        produces=[Slot(name="findings", value_type="path")],
    )
    report = _actions_contract(
        "report",
        "report",
        consumes=[
            Slot(name="tidy", value_type="path"),
            Slot(name="findings", value_type="path"),
        ],
    )
    workflow = NodeContract(
        name="monthly-close",
        body=SubgraphBody(nodes=[export, clean, audit, report]),
    )
    blueprint = contract_to_blueprint(workflow)
    assert blueprint.ordering == "graph"
    assert all(e.provenance == "data" for e in blueprint.edges)

    executor = StubExecutor(
        {"export", "clean", "audit", "report"}, delays={"clean": 0.05}
    )
    record = DagRouteRunner({"stub": executor}, max_workers=2).execute(
        _route(blueprint), idempotency_key="k", attempt=1
    )
    assert record.status is ExecutionStatus.SUCCEEDED
    # audit (parallel, undelayed) finished before the delayed clean;
    # export first, report last: the DAG came from the slots alone.
    assert executor.order == ["export", "audit", "clean", "report"]


def test_mutual_slot_production_is_rejected_loudly():
    a = _actions_contract(
        "a",
        "a",
        consumes=[Slot(name="y", value_type="str")],
        produces=[Slot(name="x", value_type="str")],
    )
    b = _actions_contract(
        "b",
        "b",
        consumes=[Slot(name="x", value_type="str")],
        produces=[Slot(name="y", value_type="str")],
    )
    blueprint = contract_to_blueprint(
        NodeContract(name="tangle", body=SubgraphBody(nodes=[a, b]))
    )
    record = DagRouteRunner({"stub": StubExecutor({"a", "b"})}).execute(
        _route(blueprint), idempotency_key="k", attempt=1
    )
    assert record.status is ExecutionStatus.BLOCKED
    assert "cycle" in (record.error or "")


def test_fallback_contract_compiles_to_a_repair_branch():
    primary = _actions_contract(
        "flaky-export",
        "export",
        produces=[Slot(name="raw", value_type="path")],
        fallback=_actions_contract("manual-export", "fetch", "convert"),
    )
    consume = _actions_contract(
        "report", "report", consumes=[Slot(name="raw", value_type="path")]
    )
    blueprint = contract_to_blueprint(
        NodeContract(name="resilient", body=SubgraphBody(nodes=[primary, consume]))
    )
    executor = StubExecutor(
        {"export", "fetch", "convert", "report"}, fail_operations={"export"}
    )
    record = DagRouteRunner({"stub": executor}).execute(
        _route(blueprint), idempotency_key="k", attempt=1
    )
    # export failed -> the two-step repair ran in order -> report still ran.
    assert executor.order == ["export", "fetch", "convert", "report"]
    assert record.status is ExecutionStatus.SUCCEEDED


def test_script_contract_executes_through_the_node_cache():
    class OneShotSynthesizer:
        calls = 0

        def synthesize(self, goal, *, session_id):
            type(self).calls += 1
            return NodeSynthesis(script="print('summarize')")

    ok = lambda request: ExecutionResult(  # noqa: E731
        phase=Phase.EXECUTE, exit_code=0, contract_ok=True, contract_payload={}
    )
    cache = LocalScriptCache(":memory:")
    script_runner = NodeScriptRunner(
        StubBackend([ok, ok, ok]),
        cache,
        synthesizer=OneShotSynthesizer(),
        backend_kind="stub",
    )
    contract = NodeContract(
        id="node.summarize",
        name="summarize",
        provenance="synthesized",
        body=ScriptBody(goal="summarize the export", bindings={"month": "June"}),
    )
    blueprint = contract_to_blueprint(contract)
    (action,) = blueprint.actions
    assert action.action.adapter == "script"
    assert action.action.parameters["node_key"] == "node.summarize"

    dag = DagRouteRunner({"script": script_runner})
    first = dag.execute(_route(blueprint), idempotency_key="r1", attempt=1)
    assert first.status is ExecutionStatus.SUCCEEDED
    # Recompiling the same contract hits the node cache: no second synthesis.
    second = dag.execute(
        _route(contract_to_blueprint(contract)), idempotency_key="r2", attempt=1
    )
    assert second.status is ExecutionStatus.SUCCEEDED
    assert OneShotSynthesizer.calls == 1


def test_empty_bodies_and_unknown_edge_children_fail_loudly():
    with pytest.raises(ValueError, match="empty actions body"):
        contract_to_blueprint(NodeContract(name="n", body=ActionsBody(actions=[])))
    with pytest.raises(ValueError, match="empty subgraph"):
        contract_to_blueprint(NodeContract(name="n", body=SubgraphBody(nodes=[])))
    with pytest.raises(ValueError, match="unknown child"):
        contract_to_blueprint(
            NodeContract(
                name="n",
                body=SubgraphBody(
                    nodes=[_actions_contract("a", "a")],
                    edges=[ContractEdge(source="node.a", target="ghost")],
                ),
            )
        )
