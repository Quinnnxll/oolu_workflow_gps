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

from ...skills.contract import ActionsBody, NodeContract, Slot
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
