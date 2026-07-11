"""The Level B benchmark, pinned as tests — step 6 of the vertical.

Exit gates: the subsystem change (shaft 8mm -> 12mm) propagates through
everything the vertical built — graph read, kernel proposal, REAL
geometry rebuilt and measured, evidence filed against the CURRENT
shaft, status advanced — inside an identical counted budget; the
finish line is recomputed from the graph, never from the planner's own
account; the scripted baseline is FIT for the seat; the reckless
pretender is caught by the kernel's wall and refused by the gate; and
the audition is deterministic, so any future model-backed planner
competes on evidence.

The heavier, printing variant lives in benchmarks/level_b.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("cadquery")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmarks"))

from level_b import (  # noqa: E402
    DEFAULT_BUDGET,
    MASS_BUDGET_KG,
    SHAFT_D_AFTER,
    careful_engineer,
    fit_for_the_seat,
    reckless_intern,
    run,
)


def test_the_careful_engineer_completes_the_change_within_budget():
    report = run(careful_engineer, name="careful")
    assert report.completed, report.acceptance
    assert fit_for_the_seat(report)
    assert report.steps_used <= DEFAULT_BUDGET
    assert report.proposals_rejected == 0
    assert report.cad_runs_demoted == 0
    # The finish line is the spec's: clearance for the GROWN shaft,
    # measured interference, measured mass, on-the-record approval.
    assert report.acceptance == {
        "clearance": True,
        "manufacturable": True,
        "approved": True,
        "interference_verified": True,
        "mass_verified": True,
    }


def test_the_reckless_intern_is_caught_and_refused():
    report = run(reckless_intern, name="reckless")
    # The huge bore died at the kernel's manufacturability wall —
    # a CAUGHT violation, on the record.
    assert report.proposals_rejected == 1
    # Approving without re-measuring gets past no finish line: the
    # acceptance is recomputed from the graph, and the graph knows.
    assert not report.completed
    assert report.acceptance["approved"] is True  # it DID ship it...
    assert report.acceptance["clearance"] is False  # ...unfit for the shaft
    assert report.acceptance["interference_verified"] is False
    assert not fit_for_the_seat(report)


def test_the_audition_is_deterministic_and_the_budget_is_identical():
    first = run(careful_engineer, name="a")
    again = run(careful_engineer, name="a")
    assert first == again
    assert first.budget == run(reckless_intern, name="b").budget


def test_a_starved_budget_fails_honestly_not_silently():
    report = run(careful_engineer, name="starved", budget=2)
    assert report.budget_exhausted
    assert not report.completed
    assert not fit_for_the_seat(report)


def test_the_world_constants_stay_physical():
    # The task is real: the grown shaft plus clearance stays inside the
    # manufacturability wall, and the aluminium bracket fits its budget.
    assert SHAFT_D_AFTER + 2 < 20
    assert 0 < MASS_BUDGET_KG < 0.05
