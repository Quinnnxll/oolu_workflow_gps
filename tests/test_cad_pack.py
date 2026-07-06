"""The CAD pack end to end: render, verify, learn, and join the marketplace.

The renderer subprocess path is exercised through a stub binary (a Python
script speaking OpenSCAD's CLI convention), so everything but OpenSCAD
itself is proven in CI; a final test runs the real binary when present.
The verification predicate is the money gate: a failed geometry check
fails the action, the run, and the earnings — and the trace store records
the failure honestly.
"""

from __future__ import annotations

import shutil
import sys
from math import pi, sin

import pytest
from test_gateway_market import _build, _contribute_and_publish
from test_http_gateway import _req

from oolu.domains.cad import (
    BRACKET_SPEC,
    GeometrySpec,
    OpenSCADExecutor,
    cad_starter_pack,
    unit_cube,
    verify_mesh,
)
from oolu.knowledge import TraceStore
from oolu.orchestrator import (
    ContractAssembler,
    DagRouteRunner,
    GoalSpec,
    RoutePlan,
    contract_to_blueprint,
)
from oolu.skills import ActionsBody, NodeContract, Slot
from oolu.skills.models import ActionEvent, ExecutionStatus

# A stub renderer speaking OpenSCAD's CLI (`<binary> -o out.stl in.scad`):
# it renders every source as a 2x3x4 box, so specs can be exact.
_STUB = """\
import sys
from oolu.domains.cad import box, write_binary_stl

args = sys.argv[1:]
out = args[args.index("-o") + 1]
with open(out, "wb") as sink:
    sink.write(write_binary_stl(box((2.0, 3.0, 4.0))))
"""

BOX_SPEC = {
    "min_volume": 23.999,
    "max_volume": 24.001,
    "min_surface_area": 51.999,  # 2(2*3+3*4+4*2) = 52
    "max_surface_area": 52.001,
    "fits_within": [2.001, 3.001, 4.001],
    "at_least": [1.999, 2.999, 3.999],
    "expected_genus": 0,
}


def _stub_executor(tmp_path, *, script=_STUB, timeout_s=30.0):
    stub = tmp_path / "fake_openscad.py"
    stub.write_text(script, encoding="utf-8")
    return OpenSCADExecutor(
        tmp_path / "work", binary=[sys.executable, str(stub)], timeout_s=timeout_s
    )


def _action(operation, parameters):
    return ActionEvent(
        correlation_id="t", adapter="cad", operation=operation, parameters=parameters
    )


# --------------------------------------------------------------------------- #
# The verification predicate (pure math, no subprocess).                       #
# --------------------------------------------------------------------------- #
def test_verify_mesh_reports_every_failed_claim_with_numbers():
    spec = GeometrySpec(
        min_volume=2.0,  # the cube has 1
        fits_within=(0.5, 2.0, 2.0),  # too tall in x
        expected_genus=1,  # it has no hole
    )
    report = verify_mesh(unit_cube(), spec)
    assert not report.passed
    joined = " ".join(report.reasons)
    assert "volume 1 below minimum 2" in joined
    assert "extent x=1 exceeds allowed 0.5" in joined
    assert "genus 0 != expected 1" in joined
    assert report.watertight and report.volume == pytest.approx(1.0)


def test_verify_mesh_withholds_volume_on_open_meshes():
    report = verify_mesh(unit_cube()[:-1], GeometrySpec(min_volume=0.5))
    assert not report.passed
    assert report.volume is None  # the formula's hypothesis failed; no number
    assert any("boundary edges" in reason for reason in report.reasons)


# --------------------------------------------------------------------------- #
# The executor: the real subprocess path, deterministic via the stub.          #
# --------------------------------------------------------------------------- #
def test_render_then_verify_through_the_stub_binary(tmp_path):
    executor = _stub_executor(tmp_path)
    rendered = executor.execute(
        _action("render_stl", {"source": "cube([2,3,4]);", "output": "part.stl"}),
        idempotency_key="k1",
    )
    assert rendered.status is ExecutionStatus.SUCCEEDED, rendered.error
    assert rendered.evidence == {"stl": "part.stl", "triangles": 12}

    verified = executor.execute(
        _action("verify_geometry", {"stl": "part.stl", "spec": BOX_SPEC}),
        idempotency_key="k2",
    )
    assert verified.status is ExecutionStatus.SUCCEEDED, verified.error
    assert verified.evidence["volume"] == pytest.approx(24.0, rel=1e-6)
    assert verified.evidence["genus"] == 0


def test_a_failed_predicate_fails_the_action_with_the_reasons(tmp_path):
    executor = _stub_executor(tmp_path)
    executor.execute(
        _action("render_stl", {"source": "x", "output": "part.stl"}),
        idempotency_key="k1",
    )
    wrong = dict(BOX_SPEC, min_volume=30.0, expected_genus=1)
    outcome = executor.execute(
        _action("verify_geometry", {"stl": "part.stl", "spec": wrong}),
        idempotency_key="k2",
    )
    assert outcome.status is ExecutionStatus.FAILED
    assert "below minimum 30" in outcome.error and "genus" in outcome.error
    assert outcome.evidence["passed"] is False  # the full report rides along


def test_renderer_failures_surface_stderr_not_a_shrug(tmp_path):
    angry = 'import sys; sys.stderr.write("CGAL error: bad polyhedron"); sys.exit(3)'
    executor = _stub_executor(tmp_path, script=angry)
    outcome = executor.execute(
        _action("render_stl", {"source": "x", "output": "part.stl"}),
        idempotency_key="k",
    )
    assert outcome.status is ExecutionStatus.FAILED
    assert "exited 3" in outcome.error and "CGAL error" in outcome.error


def test_executor_refuses_to_be_steered_outside_its_workdir(tmp_path):
    executor = _stub_executor(tmp_path)
    for params in (
        {"source": "x", "output": "../evil.stl"},
        {"source": "x", "output": "sub/dir.stl"},
        {"source": "x", "output": "part.scad"},  # wrong extension
    ):
        outcome = executor.execute(_action("render_stl", params), idempotency_key="k")
        assert outcome.status is ExecutionStatus.FAILED
    missing = executor.execute(
        _action("verify_geometry", {"stl": "never-rendered.stl", "spec": {}}),
        idempotency_key="k",
    )
    assert missing.status is ExecutionStatus.FAILED
    assert "never-rendered" in missing.error


def test_unknown_operation_and_bad_spec_fail_loudly(tmp_path):
    executor = _stub_executor(tmp_path)
    assert (
        executor.execute(_action("mill_aluminium", {}), idempotency_key="k").status
        is ExecutionStatus.FAILED
    )
    executor.execute(
        _action("render_stl", {"source": "x", "output": "p.stl"}), idempotency_key="k"
    )
    bad = executor.execute(
        _action("verify_geometry", {"stl": "p.stl", "spec": {"min_volume": "big"}}),
        idempotency_key="k",
    )
    assert bad.status is ExecutionStatus.FAILED and "spec" in bad.error


# --------------------------------------------------------------------------- #
# The loop: assemble by slots, execute as a DAG, learn from the verdict.       #
# --------------------------------------------------------------------------- #
STL_SLOT = Slot(name="part_stl", value_type="path", role="stl")
REPORT_SLOT = Slot(name="part_report", value_type="json", role="geometry-report")


def _test_pack(spec):
    render = NodeContract(
        id="cad.part.render",
        name="render part",
        produces=[STL_SLOT],
        body=ActionsBody(
            actions=[_action("render_stl", {"source": "cube!", "output": "part.stl"})]
        ),
    )
    verify = NodeContract(
        id="cad.part.verify",
        name="verify part",
        consumes=[STL_SLOT],
        produces=[REPORT_SLOT],
        body=ActionsBody(
            actions=[_action("verify_geometry", {"stl": "part.stl", "spec": spec})]
        ),
    )
    return [render, verify]


def _run_assembled(tmp_path, spec, store):
    result = ContractAssembler(_test_pack(spec)).assemble(
        GoalSpec(name="make-part", want=[REPORT_SLOT])
    )
    assert result.complete
    runner = DagRouteRunner({"cad": _stub_executor(tmp_path)}, trace_store=store)
    return runner.execute(
        RoutePlan(
            chosen=contract_to_blueprint(result.contract),
            alternatives=[],
            total_cost=0.0,
        ),
        idempotency_key="run-1",
        attempt=1,
    )


def test_geometry_gates_verified_success_and_the_trace_learns(tmp_path):
    store = TraceStore()
    record = _run_assembled(tmp_path, BOX_SPEC, store)
    assert record.status is ExecutionStatus.SUCCEEDED
    (run,) = store.runs()
    assert run.success and len(run.steps) == 2  # render before verify

    bad_spec = dict(BOX_SPEC, expected_genus=3)  # demand a triple-holed part
    record = _run_assembled(tmp_path, bad_spec, store)
    assert record.status is ExecutionStatus.FAILED  # no verified success, no pay
    newest = store.runs()[0]
    assert not newest.success  # ...and the planner learns the honest failure
    store.close()


# --------------------------------------------------------------------------- #
# The starter pack: analytic bounds and marketplace citizenship.                #
# --------------------------------------------------------------------------- #
def test_bracket_spec_bounds_bracket_the_closed_forms():
    """The pack's constants against the paper math, recomputed here.

    OpenSCAD's cylinder($fn=n) is the inscribed n-gon prism:
    cross-section A_n = (n/2) r² sin(2π/n), perimeter p_n = 2 n r sin(π/n).
    """
    n, r, thickness = 64, 3.0, 4.0
    hole_area = (n / 2.0) * r * r * sin(2.0 * pi / n)
    hole_perimeter = 2.0 * n * r * sin(pi / n)
    expected_volume = 40.0 * 20.0 * 4.0 - hole_area * thickness
    expected_area = (
        2.0 * (40.0 * 20.0 + 40.0 * 4.0 + 20.0 * 4.0)
        - 2.0 * hole_area
        + hole_perimeter * thickness
    )
    assert BRACKET_SPEC.min_volume < expected_volume < BRACKET_SPEC.max_volume
    assert BRACKET_SPEC.min_surface_area < expected_area < BRACKET_SPEC.max_surface_area
    # The bounds are tight enough to refute the wrong part outright.
    assert BRACKET_SPEC.max_volume < 3200.0  # a plate whose hole failed to cut
    assert BRACKET_SPEC.min_volume > 3200.0 - 2 * hole_area * thickness  # two holes
    assert BRACKET_SPEC.expected_genus == 1  # exactly one through-hole


def test_the_pack_assembles_by_slots_alone():
    result = ContractAssembler(cad_starter_pack()).assemble(
        GoalSpec(
            name="mounting-plate",
            want=[
                Slot.model_validate(
                    {
                        "name": "bracket_report",
                        "value_type": "json",
                        "role": "geometry-report",
                    }
                )
            ],
        )
    )
    assert result.complete
    assert result.selected == [
        "verify mounting plate geometry",
        "render mounting plate",
    ]
    blueprint = contract_to_blueprint(result.contract)
    assert [a.action.operation for a in blueprint.actions].count("render_stl") == 1


def test_cad_nodes_are_ordinary_marketplace_citizens(tmp_path):
    """Contributed, published, and goal-assembled like any office node."""
    app, conn, ident, registry, *_rest = _build(tmp_path)
    render, verify = cad_starter_pack()
    for contract, noder, price in (
        (render, "noder-cad", 0.50),
        (verify, "noder-qa", 0.10),
    ):
        _contribute_and_publish(
            app,
            ident,
            registry,
            name=contract.name,
            noder=noder,
            price=price,
            consumes=[s.model_dump(mode="json") for s in contract.consumes],
            produces=[s.model_dump(mode="json") for s in contract.produces],
        )
    response = app.handle(
        _req(
            "POST",
            "/v1/market/assemble",
            token=ident.token("consumer", "t2"),
            body={
                "goal": {
                    "name": "mounting-plate",
                    "want": [
                        {
                            "name": "bracket_report",
                            "value_type": "json",
                            "role": "geometry-report",
                        }
                    ],
                }
            },
        )
    )
    assert response.status == 200, response.body
    assert response.body["complete"] is True
    assert set(response.body["selected"]) == {
        "render mounting plate",
        "verify mounting plate geometry",
    }
    assert response.body["estimated_gross_total"] > 0  # priced like any node
    conn.close()


# --------------------------------------------------------------------------- #
# The real thing, when available.                                              #
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(shutil.which("openscad") is None, reason="OpenSCAD not installed")
def test_real_openscad_renders_the_bracket_to_spec(tmp_path):
    executor = OpenSCADExecutor(tmp_path)
    render, verify = cad_starter_pack()
    rendered = executor.execute(render.body.actions[0], idempotency_key="real-render")
    assert rendered.status is ExecutionStatus.SUCCEEDED, rendered.error
    verified = executor.execute(verify.body.actions[0], idempotency_key="real-verify")
    assert verified.status is ExecutionStatus.SUCCEEDED, verified.error
    assert verified.evidence["genus"] == 1
