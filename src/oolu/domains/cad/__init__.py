"""The CAD domain pack: exact mesh mathematics, a deterministic renderer
adapter, geometric verification predicates, and starter marketplace nodes."""

from .executor import OpenSCADExecutor
from .geometry import (
    ManifoldReport,
    Triangle,
    bounding_box,
    extents,
    manifold_report,
    parse_stl,
    signed_volume,
    surface_area,
    volume,
    write_binary_stl,
)
from .pack import (
    BRACKET_SCAD,
    BRACKET_SPEC,
    PARAMETRIC_PLATE_INPUTS,
    PARAMETRIC_PLATE_SCAD,
    PARAMETRIC_PLATE_SPEC,
    cad_starter_pack,
    parametric_plate_pack,
)
from .primitives import box, rect_plate_with_hole, unit_cube
from .verify import GeometryReport, GeometrySpec, verify_mesh, verify_stl

__all__ = [
    "BRACKET_SCAD",
    "BRACKET_SPEC",
    "GeometryReport",
    "GeometrySpec",
    "ManifoldReport",
    "OpenSCADExecutor",
    "PARAMETRIC_PLATE_INPUTS",
    "PARAMETRIC_PLATE_SCAD",
    "PARAMETRIC_PLATE_SPEC",
    "Triangle",
    "bounding_box",
    "box",
    "cad_starter_pack",
    "extents",
    "manifold_report",
    "parametric_plate_pack",
    "parse_stl",
    "rect_plate_with_hole",
    "signed_volume",
    "surface_area",
    "unit_cube",
    "verify_mesh",
    "verify_stl",
    "volume",
    "write_binary_stl",
]
