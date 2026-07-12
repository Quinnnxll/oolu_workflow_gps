"""A model in the Level B seat — proposing, never committing.

Exit gates: a model speaking the step protocol drives the WHOLE
vertical to a FIT verdict (kernel patches, real geometry, filed
evidence, advancement) with nothing but ``model.reply``; a rejection
comes back in words and the model REPAIRS (fail -> diagnose -> repair
-> verify, the spec's most valuable trajectory, live); babble is told
the protocol once and then cut off — an honest "not fit", never a
crash; and an out-of-protocol verb changes nothing at all.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("cadquery")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmarks"))

from level_b import fit_for_the_seat, model_planner, run  # noqa: E402

from oolu.projectgraph import parse_step  # noqa: E402


class ScriptedModel:
    """A stand-in brain: canned replies, and it keeps what it was told —
    the transcript is how we prove the feedback loop is honest."""

    def __init__(self, replies: list[str]):
        self._replies = list(replies)
        self.calls: list[list[dict]] = []

    def reply(self, messages: list[dict]) -> str:
        self.calls.append([dict(m) for m in messages])
        if not self._replies:
            return json.dumps({"verb": "done"})
        return self._replies.pop(0)


def _step(payload: dict) -> str:
    return "Thinking it through.\n```json\n" + json.dumps(payload) + "\n```"


def _grow_bore(base: int) -> dict:
    return {
        "verb": "propose",
        "reason": "grow the bore for the 12mm shaft with radial clearance",
        "patch": [
            {
                "op": "set",
                "object_id": "bracket-1",
                "base_revision": base,
                "pointer": "parameters/bore_d_mm",
                "old_value": 10.0,
                "new_value": 14.0,
            }
        ],
    }


_BUILD = {
    "verb": "run_cad",
    "operation": "build",
    "parameters": {
        "features": [
            {"kind": "box", "x_mm": 40.0, "y_mm": 30.0, "z_mm": 10.0},
            {"kind": "hole", "d_mm": 14.0},
        ],
        "density_kg_m3": 2700.0,
        "name": "bracket",
    },
    "postconditions": [
        {"name": "mass", "pointer": "mass_kg", "op": "<=", "value": 0.035},
        {"name": "solid", "pointer": "solid_ok", "op": "==", "value": True},
    ],
}

_ASSEMBLE = {
    "verb": "run_cad",
    "operation": "assemble",
    "parameters": {
        "parts": [
            {
                "name": "bracket",
                "features": _BUILD["parameters"]["features"],
                "position": [0, 0, 0],
            },
            {
                "name": "shaft",
                "features": [{"kind": "cylinder", "d_mm": 12.0, "h_mm": 40.0}],
                "position": [0, 0, 0],
            },
        ]
    },
    "postconditions": [
        {"name": "fit", "pointer": "interference_count", "op": "==", "value": 0}
    ],
}


def _file_evidence(base: int) -> dict:
    return {
        "verb": "propose",
        "reason": "file the verified measurements against the current shaft",
        "patch": [
            {
                "op": "append",
                "object_id": "bracket-1",
                "base_revision": base,
                "pointer": "evidence",
                "new_value": {
                    "kind": "cad-verification",
                    "shaft_d_mm": 12.0,
                    "bore_d_mm": 14.0,
                    "mass_kg": 0.0295,
                    "interference_count": 0,
                },
            }
        ],
    }


def _approve(base: int) -> dict:
    return {
        "verb": "propose",
        "reason": "verified against the grown shaft — advance",
        "patch": [
            {
                "op": "set",
                "object_id": "bracket-1",
                "base_revision": base,
                "pointer": "status",
                "old_value": "draft",
                "new_value": "approved",
            }
        ],
    }


def test_a_protocol_speaking_model_earns_the_seat():
    model = ScriptedModel(
        [
            _step(_grow_bore(base=1)),
            _step(_BUILD),
            _step(_ASSEMBLE),
            _step(_file_evidence(base=2)),
            _step(_approve(base=3)),
            _step({"verb": "done"}),
        ]
    )
    report = run(model_planner(model), name="scripted-brain")
    assert report.completed, report.acceptance
    assert fit_for_the_seat(report)
    assert report.steps_used == 5
    # The first turn already saw the world's current truth.
    first = model.calls[0][-1]["content"]
    assert "shaft-1" in first and '"d_mm": 12.0' in first
    # And the CAD results the model saw were MEASURED, not asserted.
    build_result = model.calls[2][-1]["content"]
    assert "mass_kg" in build_result and "solid_ok" in build_result


def test_a_rejection_returns_in_words_and_the_model_repairs():
    model = ScriptedModel(
        [
            _step(_grow_bore(base=99)),  # a stale idea of the world
            _step(_grow_bore(base=1)),  # ...diagnosed and repaired
            _step(_BUILD),
            _step(_ASSEMBLE),
            _step(_file_evidence(base=2)),
            _step(_approve(base=3)),
            _step({"verb": "done"}),
        ]
    )
    report = run(model_planner(model), name="repairing-brain")
    assert report.proposals_rejected == 1
    assert report.completed and fit_for_the_seat(report)
    # The kernel's reason reached the model before its repair.
    told = model.calls[1][-1]["content"]
    assert "stale" in told and "rebase" in told


def test_babble_is_cut_off_honestly_never_a_crash():
    model = ScriptedModel(
        ["I think we should consider the holistic implications."] * 6
    )
    report = run(model_planner(model), name="babbler")
    assert not report.completed
    assert not fit_for_the_seat(report)
    assert report.steps_used == 0  # babble spends nothing
    # The protocol was restated exactly as promised, then the seat closed.
    correction = model.calls[1][-1]["content"]
    assert "unreadable step" in correction
    assert len(model.calls) == 3  # MAX_JUNK corrections, then cut off


def test_an_out_of_protocol_verb_changes_nothing():
    model = ScriptedModel(
        [_step({"verb": "erase_the_audit_log"}), _step({"verb": "done"})]
    )
    report = run(model_planner(model), name="escape-artist")
    assert report.steps_used == 0
    assert not report.completed
    told = model.calls[1][-1]["content"]
    assert "unknown verb" in told


def test_parse_step_tolerates_prose_and_junk():
    assert parse_step('```json\n{"verb": "done"}\n```')["verb"] == "done"
    assert parse_step('prefix {"verb": "read", "object_id": "x"} suffix')[
        "object_id"
    ] == "x"
    assert parse_step("no json here") is None
    assert parse_step('{"not_a_verb": 1}') is None
    assert parse_step(None) is None
