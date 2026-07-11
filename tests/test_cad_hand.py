"""The CadQuery hand — step 5: the vertical gets physical.

Exit gates: semantic features become a REAL B-rep solid measured by
the geometry kernel (validity, exact volume, mass under a density,
bounding box); STEP and STL land in the content-addressed store,
byte-retrievable by their self-verifying refs; interference is a
boolean-intersection MEASUREMENT; the postcondition evaluator judges
the hand like any other ("mass_kg <= 0.5" demotes an overweight
build); and a marketplace NODE whose action is a cad build runs end to
end through the contract path — the adapter, generalized as a node.
"""

from __future__ import annotations

import math

import pytest

cadquery = pytest.importorskip("cadquery")

from oolu.durable import FilesystemArtifactStore  # noqa: E402
from oolu.orchestrator import ActionExecutorRouteRunner  # noqa: E402
from oolu.orchestrator.state import (  # noqa: E402
    Blueprint,
    ReservedAction,
    RoutePlan,
)
from oolu.skills.cad_adapter import CadActionExecutor  # noqa: E402
from oolu.skills.models import (  # noqa: E402
    ActionEvent,
    ExecutionStatus,
    Postcondition,
)

ALUMINIUM = 2700  # kg/m^3

BRACKET = [
    {"kind": "box", "x_mm": 40, "y_mm": 30, "z_mm": 10},
    {"kind": "hole", "d_mm": 8},
]


def _action(operation="build", postconditions=(), **params) -> ActionEvent:
    return ActionEvent(
        correlation_id="c1",
        adapter="cad",
        operation=operation,
        parameters=params,
        postconditions=list(postconditions),
    )


def _hand(tmp_path) -> CadActionExecutor:
    return CadActionExecutor(
        artifacts=FilesystemArtifactStore(tmp_path / "blobs")
    )


# --------------------------------------------------------------------------- #
# Build: measured geometry, never hoped-for geometry.                          #
# --------------------------------------------------------------------------- #
def test_a_bracket_is_built_measured_and_exported(tmp_path):
    store = FilesystemArtifactStore(tmp_path / "blobs")
    hand = CadActionExecutor(artifacts=store)
    outcome = hand.execute(
        _action(
            features=BRACKET,
            density_kg_m3=ALUMINIUM,
            name="bracket",
            export=["step", "stl"],
        ),
        idempotency_key="k1",
    )
    assert outcome.status is ExecutionStatus.SUCCEEDED, outcome.error
    observed = outcome.evidence
    assert observed["solid_ok"] is True and observed["rebuild_successful"]

    # The kernel's numbers match exact math: 40*30*10 minus an 8mm bore.
    expected_mm3 = 40 * 30 * 10 - math.pi * 4 * 4 * 10
    assert observed["volume_cm3"] == pytest.approx(expected_mm3 / 1000, rel=1e-4)
    assert observed["mass_kg"] == pytest.approx(
        expected_mm3 * 1e-9 * ALUMINIUM, rel=1e-4
    )
    assert observed["bbox_mm"] == {"x": 40.0, "y": 30.0, "z": 10.0}
    assert abs(observed["center_of_mass_mm"]["x"]) < 1e-6

    # Native artifacts, content-addressed and byte-retrievable.
    for fmt in ("step", "stl"):
        entry = observed["artifacts"][fmt]
        assert entry["bytes"] > 0
        assert len(store.get(entry["ref"])) == entry["bytes"]


def test_unbuildable_features_fail_in_words(tmp_path):
    hand = _hand(tmp_path)
    refined_nothing = hand.execute(
        _action(features=[{"kind": "hole", "d_mm": 8}]), idempotency_key="k1"
    )
    assert refined_nothing.status is ExecutionStatus.FAILED
    assert "create stock first" in refined_nothing.error
    missing = hand.execute(
        _action(features=[{"kind": "box", "x_mm": 40}]), idempotency_key="k2"
    )
    assert missing.status is ExecutionStatus.FAILED
    assert "missing dimension" in missing.error


def test_a_missing_kernel_refuses_in_words(tmp_path, monkeypatch):
    import oolu.skills.cad_adapter as module

    def gone():
        raise ImportError("no cadquery")

    monkeypatch.setattr(module, "_require_cadquery", gone)
    outcome = _hand(tmp_path).execute(
        _action(features=BRACKET), idempotency_key="k1"
    )
    assert outcome.status is ExecutionStatus.FAILED
    assert "cadquery is not installed" in outcome.error


# --------------------------------------------------------------------------- #
# Assemble: interference is a measurement.                                     #
# --------------------------------------------------------------------------- #
def test_interference_is_boolean_intersection_not_hope(tmp_path):
    hand = _hand(tmp_path)
    cube = [{"kind": "box", "x_mm": 20, "y_mm": 20, "z_mm": 20}]
    clashing = hand.execute(
        _action(
            "assemble",
            parts=[
                {"name": "a", "features": cube, "position": [0, 0, 0]},
                {"name": "b", "features": cube, "position": [10, 0, 0]},
            ],
        ),
        idempotency_key="k1",
    )
    assert clashing.status is ExecutionStatus.SUCCEEDED
    assert clashing.evidence["interference_count"] == 1
    [clash] = clashing.evidence["interferences"]
    assert clash["shared_volume_mm3"] == pytest.approx(10 * 20 * 20, rel=1e-6)

    clear = hand.execute(
        _action(
            "assemble",
            parts=[
                {"name": "a", "features": cube, "position": [0, 0, 0]},
                {"name": "b", "features": cube, "position": [40, 0, 0]},
            ],
        ),
        idempotency_key="k2",
    )
    assert clear.evidence["interference_count"] == 0


# --------------------------------------------------------------------------- #
# The evaluator judges the engineering hand like any other.                    #
# --------------------------------------------------------------------------- #
def test_an_overweight_build_is_demoted_by_its_own_promise(tmp_path):
    runner = ActionExecutorRouteRunner({"cad": _hand(tmp_path)})
    action = _action(
        features=BRACKET,
        density_kg_m3=ALUMINIUM,
        postconditions=[
            Postcondition(
                name="mass-budget", pointer="mass_kg", op="<=", value=0.01
            ),
            Postcondition(
                name="fits-envelope", pointer="bbox_mm/x", op="<=", value=210
            ),
        ],
    )
    record = runner.execute(
        RoutePlan(
            chosen=Blueprint(
                name="cad-build", actions=[ReservedAction(action=action)]
            ),
            alternatives=[],
        ),
        idempotency_key="run-1",
        attempt=1,
    )
    # ~0.031 kg of aluminium against a 10 g budget: the kernel said
    # succeeded, the evaluator says no — and names only the broken half.
    assert record.status is ExecutionStatus.FAILED
    assert "mass-budget" in record.error
    assert "fits-envelope" not in record.error


# --------------------------------------------------------------------------- #
# The adapter, generalized as a NODE: a cad build runs the contract path.      #
# --------------------------------------------------------------------------- #
def test_a_cad_node_runs_end_to_end_through_the_contract_path(tmp_path):
    from test_contract_run import _assembled_contract, _build
    from test_gateway_market import _contribute_and_publish
    from test_http_gateway import _req
    from test_market_assemble import TIDY

    hand = _hand(tmp_path)
    app, conn, ident, registry, *_rest = _build(
        tmp_path, executors={"cad": hand}
    )
    try:
        _contribute_and_publish(
            app,
            ident,
            registry,
            name="bracket forge",
            noder="noder-cad",
            price=0.25,
            consumes=[],
            produces=[TIDY],
            actions=[
                {
                    "correlation_id": "c",
                    "adapter": "cad",
                    "operation": "build",
                    "parameters": {
                        "features": BRACKET,
                        "density_kg_m3": ALUMINIUM,
                    },
                    "postconditions": [
                        {
                            "name": "solid",
                            "pointer": "solid_ok",
                            "op": "==",
                            "value": True,
                        }
                    ],
                }
            ],
        )
        contract = _assembled_contract(app, ident)
        response = app.handle(
            _req(
                "POST",
                "/v1/runs/contract",
                token=ident.token("consumer", "t2"),
                body={"contract": contract},
            )
        )
        assert response.status == 200, response.body
        assert response.body["status"] == "succeeded"
        [outcome] = response.body["outcomes"]
        # The run's record carries the kernel's measurements — evidence,
        # postcondition verdict included, ready to be FILED on the graph.
        assert outcome["evidence"]["solid_ok"] is True
        assert outcome["evidence"]["postconditions"]["verified"] is True
        assert outcome["evidence"]["mass_kg"] > 0
    finally:
        conn.close()
