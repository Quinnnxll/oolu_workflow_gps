"""Goal-directed assembly: want slots in, executable subgraph contract out."""

from __future__ import annotations

import random

from test_dag_scheduler import StubExecutor

from workflow_gps.knowledge import NodeObservation, TraceStore
from workflow_gps.orchestrator import (
    AssemblyResult,
    ContractAssembler,
    DagRouteRunner,
    GoalSpec,
    RoutePlan,
    contract_from_registered,
    contract_to_blueprint,
)
from workflow_gps.skills import (
    ActionsBody,
    NodeContract,
    NodeStats,
    ScriptBody,
    SkillRegistry,
    Slot,
)
from workflow_gps.skills.models import (
    ActionEvent,
    ExecutionStatus,
    ReusableSkill,
    SkillSignature,
)


def _contract(name, *, operation=None, consumes=(), produces=(), stats=None):
    return NodeContract(
        id=f"lib.{name}",
        name=name,
        consumes=list(consumes),
        produces=list(produces),
        body=ActionsBody(
            actions=[
                ActionEvent(
                    correlation_id="c", adapter="stub", operation=operation or name
                )
            ]
        ),
        stats=stats,
    )


RAW = Slot(name="raw", value_type="path")
TIDY = Slot(name="tidy", value_type="path")
FINDINGS = Slot(name="findings", value_type="path")
REPORT = Slot(name="report", value_type="path")


def _library():
    return [
        _contract("export", produces=[RAW]),
        _contract("clean", consumes=[RAW], produces=[TIDY]),
        _contract("audit", consumes=[RAW], produces=[FINDINGS]),
        _contract("report", consumes=[TIDY, FINDINGS], produces=[REPORT]),
        _contract("unrelated", produces=[Slot(name="noise", value_type="str")]),
    ]


def test_assembles_the_whole_chain_from_one_wanted_slot():
    result = ContractAssembler(_library()).assemble(
        GoalSpec(name="monthly-close", want=[REPORT])
    )
    assert result.complete
    assert set(result.selected) == {"report", "clean", "audit", "export"}
    assert "unrelated" not in result.selected  # nothing irrelevant dragged in

    # The assembled contract executes: ordering fell out of the slots.
    executor = StubExecutor({"export", "clean", "audit", "report"})
    record = DagRouteRunner({"stub": executor}).execute(
        RoutePlan(
            chosen=contract_to_blueprint(result.contract),
            alternatives=[],
            total_cost=0.0,
        ),
        idempotency_key="k",
        attempt=1,
    )
    assert record.status is ExecutionStatus.SUCCEEDED
    assert executor.order[0] == "export" and executor.order[-1] == "report"


def test_slots_on_hand_are_not_re_produced():
    result = ContractAssembler(_library()).assemble(
        GoalSpec(name="finish-up", want=[REPORT], have=[RAW])
    )
    assert result.complete
    assert "export" not in result.selected  # raw is already on hand


def test_verified_history_picks_the_producer():
    proven = _contract(
        "proven-export",
        operation="export",
        produces=[RAW],
        stats=NodeStats(successes=90, failures=2, cost_ewma=2.0),
    )
    flaky = _contract(
        "flaky-export",
        operation="export",
        produces=[RAW],
        stats=NodeStats(successes=2, failures=20, cost_ewma=0.1),
    )
    result = ContractAssembler([proven, flaky]).assemble(
        GoalSpec(name="get-raw", want=[RAW])
    )
    assert result.selected == ["proven-export"]  # history beats a cheap sticker

    # Thompson mode explores but still overwhelmingly prefers the proven one.
    rng = random.Random(7)
    picks = [
        ContractAssembler([proven, flaky], rng=rng)
        .assemble(GoalSpec(name="get-raw", want=[RAW]))
        .selected[0]
        for _ in range(20)
    ]
    assert picks.count("proven-export") >= 16


def test_missing_slots_are_reported_not_guessed():
    result = ContractAssembler(_library()).assemble(
        GoalSpec(name="impossible", want=[Slot(name="unicorn", value_type="path")])
    )
    assert not result.complete
    assert result.contract is None
    assert [slot.name for slot in result.missing] == ["unicorn"]


def test_gap_filling_synthesizes_script_nodes_for_missing_slots():
    result = ContractAssembler(_library(), fill_gaps_with_scripts=True).assemble(
        GoalSpec(
            name="stretch",
            want=[REPORT, Slot(name="forecast", value_type="path")],
        )
    )
    assert result.complete
    assert result.gap_filled == ["forecast"]
    gap = next(
        node for node in result.contract.body.nodes if node.name == "produce forecast"
    )
    assert isinstance(gap.body, ScriptBody)
    assert gap.provenance == "synthesized"
    assert "forecast" in gap.body.goal
    # The gap node's cache key is pinned, so its synthesis memoizes.
    assert gap.body.node_key == "gap.stretch.forecast"


def test_assembler_grows_with_a_live_registry_and_trace_stats(tmp_path):
    """The callable-library form: newly registered skills join the next plan,
    carrying their verified history from the trace store."""
    registry = SkillRegistry(tmp_path / "registry.db")
    traces = TraceStore(tmp_path / "traces.db")

    def library():
        # A planner declares what each registered skill produces (here: raw);
        # stats ride in live from the trace store on every call.
        return [
            contract_from_registered(entry, trace_store=traces).model_copy(
                update={"produces": [RAW]}
            )
            for entry in registry.list()
        ]

    assembler = ContractAssembler(library)
    goal = GoalSpec(name="get-raw", want=[RAW])
    assert not assembler.assemble(goal).complete  # empty registry: nothing yet

    skill = ReusableSkill(
        id="learned.export",
        name="export",
        description="exports the raw file",
        signature=SkillSignature(application="cli", adapter="cli"),
        actions=[ActionEvent(correlation_id="c", adapter="cli", operation="export")],
    )
    registry.register(skill, semver="1.0.0")
    traces.record_run(
        goal="export", steps=[NodeObservation("export", ok=True)], success=True
    )

    result: AssemblyResult = assembler.assemble(goal)  # same assembler, no restart
    assert result.complete
    (node,) = result.contract.body.nodes
    assert node.stats is not None and node.stats.successes == 1  # live history
    registry.close()
    traces.close()
