"""The CAD starter pack — two marketplace nodes and their analytic spec.

A parametric mounting plate (40×20×4 mm with one Ø6 bolt hole) plus the
verification node that gates its success. The spec's bounds are not
guesses; they bracket closed-form values:

- OpenSCAD renders ``cylinder($fn = n)`` as the **inscribed** regular
  n-gon prism, whose cross-section area is A_n = (n/2)·r²·sin(2π/n).
  With n = 64, r = 3: A₆₄ = 288·sin(π/32) ≈ 28.2290 (the true disk is
  9π ≈ 28.2743 — the polygon removes slightly less).
- Expected volume: 40·20·4 − A₆₄·4 ≈ 3200 − 112.916 = 3087.08 mm³,
  bounded [3080, 3092] to absorb float32 STL coordinates, comfortably
  excluding both a hole-less plate (3200) and a double-holed one (~2974).
- Expected surface area: 2(40·20 + 40·4 + 20·4) − 2·A₆₄ + p₆₄·4 where
  p₆₄ = 2n·r·sin(π/n) ≈ 18.842 is the polygon perimeter:
  2080 − 56.458 + 75.368 ≈ 2098.91 mm², bounded [2090, 2105].
- Topology: exactly one through-hole means **genus 1** — a plate whose
  hole did not cut through (or a solid plate) fails on topology alone,
  before any measure is consulted.

The pack proves the platform claim: a CAD node is an ordinary marketplace
citizen — typed slots chain it, verified history prices it, and money
moves only when the geometry above checks out.
"""

from __future__ import annotations

from ...skills.contract import ActionsBody, NodeContract, Slot, ValueInput
from ...skills.models import ActionEvent
from .verify import GeometrySpec

BRACKET_SCAD = """\
$fn = 64;
difference() {
    cube([40, 20, 4]);
    translate([20, 10, -1]) cylinder(h = 6, r = 3);
}
"""

BRACKET_SPEC = GeometrySpec(
    require_watertight=True,
    min_volume=3080.0,
    max_volume=3092.0,
    min_surface_area=2090.0,
    max_surface_area=2105.0,
    fits_within=(40.01, 20.01, 4.01),
    at_least=(39.99, 19.99, 3.99),
    expected_genus=1,
)

BRACKET_STL = Slot(
    name="bracket_stl",
    value_type="path",
    role="stl",
    description="rendered mounting-plate solid",
)
BRACKET_REPORT = Slot(
    name="bracket_report",
    value_type="json",
    role="geometry-report",
    description="measured geometry with a pass/fail verdict",
)


def cad_starter_pack() -> list[NodeContract]:
    """The render node and the verification node, slot-chained."""
    render = NodeContract(
        id="cad.bracket.render",
        name="render mounting plate",
        description="renders the parametric mounting plate to STL (deterministic)",
        provenance="human",
        produces=[BRACKET_STL],
        body=ActionsBody(
            actions=[
                ActionEvent(
                    correlation_id="cad-bracket",
                    adapter="cad",
                    operation="render_stl",
                    parameters={"source": BRACKET_SCAD, "output": "bracket.stl"},
                )
            ]
        ),
    )
    verify = NodeContract(
        id="cad.bracket.verify",
        name="verify mounting plate geometry",
        description=(
            "checks the rendered plate against its analytic spec: volume, "
            "surface area, fit, and genus-1 topology (exactly one bolt hole)"
        ),
        provenance="human",
        consumes=[BRACKET_STL],
        produces=[BRACKET_REPORT],
        body=ActionsBody(
            actions=[
                ActionEvent(
                    correlation_id="cad-bracket",
                    adapter="cad",
                    operation="verify_geometry",
                    parameters={
                        "stl": "bracket.stl",
                        "spec": BRACKET_SPEC.model_dump(mode="json"),
                    },
                )
            ]
        ),
    )
    return [render, verify]


# --------------------------------------------------------------------------- #
# The parametric plate: the creative step as declared inputs.                  #
# --------------------------------------------------------------------------- #
# The scaffolding of a mechanical-design workflow is deterministic; the
# creative step is VALUES. This node declares them — with defaults and hard
# bounds — and its verification spec is derived from the bounds so that
# EVERY admissible fill verifies:
#
#   volume = t·(w·d − A₆₄(r)) with A₆₄(r) = 32·r²·sin(π/32)  (inscribed 64-gon)
#     min over the box: t=3, w·d=450, r=4 → 3·(450 − 50.19) ≈ 1199.4
#     max over the box: t=6, w·d=1800, r→0 → < 10800
#   genus = 1 for every fill: the hole is centered and r ≤ 4 < depth_min/2,
#     so it never breaches an edge, and the cutting cylinder spans
#     [-1, t+1] so it always passes through.
#
# A value patcher (LLM, user, default) chooses WITHIN the box the spec
# already covers — creativity inside verified walls.
PARAMETRIC_PLATE_SCAD = """\
$fn = 64;
difference() {
    cube([{width}, {depth}, {thickness}]);
    translate([{width}/2, {depth}/2, -1])
        cylinder(h = {thickness} + 2, r = {hole_radius});
}
"""

PARAMETRIC_PLATE_INPUTS = [
    ValueInput(
        name="width",
        description="plate width in mm",
        default=40.0,
        minimum=30.0,
        maximum=60.0,
    ),
    ValueInput(
        name="depth",
        description="plate depth in mm",
        default=20.0,
        minimum=15.0,
        maximum=30.0,
    ),
    ValueInput(
        name="thickness",
        description="plate thickness in mm",
        default=4.0,
        minimum=3.0,
        maximum=6.0,
    ),
    ValueInput(
        name="hole_radius",
        description="central bolt-hole radius in mm",
        default=3.0,
        minimum=2.0,
        maximum=4.0,
    ),
]

PARAMETRIC_PLATE_SPEC = GeometrySpec(
    require_watertight=True,
    min_volume=1150.0,  # below the bounds-minimum 1199.4, above nonsense
    max_volume=10801.0,  # the bounds-maximum box, hole only subtracts
    fits_within=(60.01, 30.01, 6.01),
    at_least=(29.99, 14.99, 2.99),
    expected_genus=1,  # exactly one through-hole, for EVERY admissible fill
)

PLATE_STL = Slot(
    name="plate_stl",
    value_type="path",
    role="stl",
    description="rendered parametric plate",
)
PLATE_REPORT = Slot(
    name="plate_report",
    value_type="json",
    role="geometry-report",
    description="measured geometry with a pass/fail verdict",
)


def parametric_plate_pack() -> list[NodeContract]:
    """Render + verify for the parametric plate — creative values enter
    through declared inputs, verification stays sovereign."""
    render = NodeContract(
        id="cad.plate.render",
        name="model parametric plate",
        description=(
            "renders a plate whose width/depth/thickness/hole are declared "
            "inputs a value patcher fills within hard bounds"
        ),
        provenance="human",
        inputs=list(PARAMETRIC_PLATE_INPUTS),
        produces=[PLATE_STL],
        body=ActionsBody(
            actions=[
                ActionEvent(
                    correlation_id="cad-plate",
                    adapter="cad",
                    operation="render_stl",
                    parameters={
                        "source": {"$template": PARAMETRIC_PLATE_SCAD},
                        "output": "plate.stl",
                    },
                )
            ]
        ),
    )
    verify = NodeContract(
        id="cad.plate.verify",
        name="verify parametric plate geometry",
        description=(
            "checks the rendered plate against bounds-derived spec: every "
            "admissible fill verifies, everything else fails the run"
        ),
        provenance="human",
        consumes=[PLATE_STL],
        produces=[PLATE_REPORT],
        body=ActionsBody(
            actions=[
                ActionEvent(
                    correlation_id="cad-plate",
                    adapter="cad",
                    operation="verify_geometry",
                    parameters={
                        "stl": "plate.stl",
                        "spec": PARAMETRIC_PLATE_SPEC.model_dump(mode="json"),
                    },
                )
            ]
        ),
    )
    return [render, verify]
