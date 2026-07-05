"""The trace corpus: recorded runs become training data and a learned planner.

Increment 3 of the planning-model arc. The trace store now logs every run
verbatim; ``knowledge.corpus`` turns the log into (goal, prefix -> next)
training examples and a portable JSONL export for offline model training;
and ``TraceProposalModel`` is the modest baseline that plugs the same
``ProposalModel`` seam a Mamba/SSM checkpoint must later beat — proposing
live from the caller's own history, judged pool-first (goal, then
co-selection, then global) with no opinion where it has no evidence.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from workflow_gps.knowledge import (
    NodeObservation,
    RecordedRun,
    TraceStore,
    build_examples,
    export_jsonl,
    route_node_key,
)
from workflow_gps.orchestrator import (
    ContractAssembler,
    GoalSpec,
    TraceProposalModel,
)
from workflow_gps.skills import ActionsBody, NodeContract, Slot
from workflow_gps.skills.models import ActionEvent

RAW = Slot(name="raw", value_type="path")


def _record(store, goal, step_names, *, success=True, context="", costs=None):
    steps = [
        NodeObservation(
            route_node_key(name),
            ok=success,
            cost=None if costs is None else costs[i],
        )
        for i, name in enumerate(step_names)
    ]
    store.record_run(goal=goal, steps=steps, success=success, context=context)


def _producer(name):
    return NodeContract(
        id=f"lib.{name}",
        name=name,
        produces=[RAW],
        body=ActionsBody(
            actions=[ActionEvent(correlation_id="c", adapter="stub", operation=name)]
        ),
    )


# --------------------------------------------------------------------------- #
# The raw run log.                                                             #
# --------------------------------------------------------------------------- #
def test_runs_round_trip_verbatim_newest_first():
    store = TraceStore()
    _record(store, "close", ["export", "clean"], costs=[0.1, 0.2])
    _record(store, "audit", ["export"], success=False, context="t1")

    newest, oldest = store.runs()
    assert newest.goal == "audit" and newest.success is False
    assert newest.context == "t1"
    assert oldest.goal == "close" and oldest.success is True
    assert [s.node_key for s in oldest.steps] == ["route:export", "route:clean"]
    assert [s.cost for s in oldest.steps] == [0.1, 0.2]
    assert oldest.step_keys() == {"route:export", "route:clean"}

    assert [r.goal for r in store.runs(goal="close")] == ["close"]
    assert [r.goal for r in store.runs(context="t1")] == ["audit"]
    assert store.runs(context="") == [store.runs()[1]]  # '' is a real bucket
    assert len(store.runs(limit=1)) == 1
    store.close()


def test_an_existing_v1_database_adopts_the_run_log():
    """A store created before the run log migrates cleanly and keeps its stats."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = f"{tmp}/traces.db"
        old = sqlite3.connect(path)
        old.execute(
            """CREATE TABLE trace_node_stats (
                   node_key TEXT NOT NULL, context TEXT NOT NULL DEFAULT '',
                   successes INTEGER NOT NULL DEFAULT 0,
                   failures INTEGER NOT NULL DEFAULT 0,
                   cost_ewma REAL, updated_at TEXT NOT NULL,
                   PRIMARY KEY (node_key, context))"""
        )
        old.execute("INSERT INTO trace_node_stats VALUES ('n', '', 4, 1, NULL, 'then')")
        old.execute("PRAGMA user_version = 1")
        old.commit()
        old.close()

        store = TraceStore(path)
        assert store.posterior("n").successes == 4  # old statistics intact
        assert store.runs() == []  # the log exists, honestly empty
        _record(store, "g", ["n"])
        assert len(store.runs()) == 1
        store.close()


# --------------------------------------------------------------------------- #
# Corpus building + JSONL export.                                              #
# --------------------------------------------------------------------------- #
def test_examples_are_every_prefix_boundary_in_completion_order():
    run = RecordedRun(
        goal="close",
        context="",
        success=True,
        steps=(
            NodeObservation("route:export", ok=True),
            NodeObservation("route:clean", ok=True),
            NodeObservation("route:report", ok=False),
        ),
        recorded_at="now",
    )
    examples = build_examples([run])
    assert [(e.prefix, e.next_node) for e in examples] == [
        ((), "route:export"),
        (("route:export",), "route:clean"),
        (("route:export", "route:clean"), "route:report"),
    ]
    assert examples[2].next_ok is False and examples[2].run_success is True


def test_failed_runs_export_flagged_and_filterable():
    store = TraceStore()
    _record(store, "g", ["a"], success=True)
    _record(store, "g", ["b"], success=False)
    everything = build_examples(store.runs())
    assert {e.run_success for e in everything} == {True, False}
    winners = build_examples(store.runs(), only_successful=True)
    assert [e.next_node for e in winners] == ["route:a"]
    store.close()


def test_jsonl_export_is_portable_and_time_ordered(tmp_path):
    store = TraceStore()
    _record(store, "first", ["a", "b"])
    _record(store, "second", ["c"], success=False)
    target = tmp_path / "corpus.jsonl"

    count = export_jsonl(store, target)
    lines = target.read_text(encoding="utf-8").splitlines()
    assert count == len(lines) == 3
    records = [json.loads(line) for line in lines]
    # Oldest run first, so a training job can honor time.
    assert [r["goal"] for r in records] == ["first", "first", "second"]
    assert records[1] == {
        "goal": "first",
        "context": "",
        "prefix": ["route:a"],
        "next_node": "route:b",
        "next_ok": True,
        "run_success": True,
    }
    assert export_jsonl(store, target, only_successful=True) == 2
    store.close()


# --------------------------------------------------------------------------- #
# The baseline learned planner.                                                #
# --------------------------------------------------------------------------- #
def test_goal_history_endorses_the_nodes_that_belong():
    store = TraceStore()
    for _ in range(3):
        _record(store, "monthly-close", ["export", "clean-a"])
    model = TraceProposalModel(store)

    proposal = model.propose(
        goal=GoalSpec(name="monthly-close", want=[RAW]),
        slot=RAW,
        selected=[],
        candidates=[_producer("clean-a"), _producer("clean-b")],
    )
    # clean-a rode three verified runs of this very goal; clean-b was
    # never seen there, so the model has no opinion about it at all.
    assert proposal.weights == {"lib.clean-a": pytest.approx(0.8)}
    assert proposal.cost == 0.0
    store.close()


def test_appearing_in_failures_counts_against():
    store = TraceStore()
    _record(store, "flaky-goal", ["clean-x"], success=False)
    _record(store, "flaky-goal", ["clean-x"], success=False)
    model = TraceProposalModel(store)
    proposal = model.propose(
        goal=GoalSpec(name="flaky-goal", want=[RAW]),
        slot=RAW,
        selected=[],
        candidates=[_producer("clean-x")],
    )
    assert proposal.weights["lib.clean-x"] == pytest.approx(0.25)
    store.close()


def test_an_unseen_goal_falls_back_to_co_selection_then_global():
    store = TraceStore()
    _record(store, "other-goal", ["export", "clean-a"])
    _record(store, "third-goal", ["different", "clean-b"])
    model = TraceProposalModel(store)

    # Co-selection: with "export" already in the plan, only runs that
    # contained export judge the candidates — clean-a belongs, clean-b
    # never ran alongside export, so no opinion.
    with_export = model.propose(
        goal=GoalSpec(name="brand-new", want=[RAW]),
        slot=RAW,
        selected=["export"],
        candidates=[_producer("clean-a"), _producer("clean-b")],
    )
    assert set(with_export.weights) == {"lib.clean-a"}

    # Nothing selected yet: all recorded runs judge them, and both have
    # one verified appearance each.
    cold = model.propose(
        goal=GoalSpec(name="brand-new", want=[RAW]),
        slot=RAW,
        selected=[],
        candidates=[_producer("clean-a"), _producer("clean-b")],
    )
    assert cold.weights == {
        "lib.clean-a": pytest.approx(2 / 3),
        "lib.clean-b": pytest.approx(2 / 3),
    }
    store.close()


def test_no_history_means_no_opinion():
    store = TraceStore()
    model = TraceProposalModel(store)
    proposal = model.propose(
        goal=GoalSpec(name="anything", want=[RAW]),
        slot=RAW,
        selected=[],
        candidates=[_producer("a")],
    )
    assert proposal.weights == {} and proposal.cost == 0.0
    store.close()


def test_the_model_learns_live_from_each_new_run():
    store = TraceStore()
    model = TraceProposalModel(store)
    goal = GoalSpec(name="close", want=[RAW])
    candidates = [_producer("clean-a")]

    before = model.propose(goal=goal, slot=RAW, selected=[], candidates=candidates)
    assert before.weights == {}
    _record(store, "close", ["clean-a"])  # one verified run later...
    after = model.propose(goal=goal, slot=RAW, selected=[], candidates=candidates)
    assert after.weights == {"lib.clean-a": pytest.approx(2 / 3)}
    store.close()


def test_history_flips_a_thin_tie_in_the_assembler():
    store = TraceStore()
    for _ in range(3):
        _record(store, "get-raw", ["exporter-b"])
    result = ContractAssembler(
        [_producer("exporter-a"), _producer("exporter-b")],
        proposal_model=TraceProposalModel(store),
    ).assemble(GoalSpec(name="get-raw", want=[RAW]))
    # The name tie-break would pick exporter-a; the caller's own verified
    # history for this goal says exporter-b belongs.
    assert result.selected == ["exporter-b"]
    assert result.planning_cost == 0.0  # local advice is free
    store.close()
