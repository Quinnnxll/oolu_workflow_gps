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
from .pack import BRACKET_SCAD, BRACKET_SPEC, cad_starter_pack
from .primitives import box, unit_cube
from .verify import GeometryReport, GeometrySpec, verify_mesh, verify_stl

__all__ = [
    "BRACKET_SCAD",
    "BRACKET_SPEC",
    "GeometryReport",
    "GeometrySpec",
    "ManifoldReport",
    "OpenSCADExecutor",
    "Triangle",
    "bounding_box",
    "box",
    "cad_starter_pack",
    "extents",
    "manifold_report",
    "parse_stl",
    "signed_volume",
    "surface_area",
    "unit_cube",
    "verify_mesh",
    "verify_stl",
    "volume",
    "write_binary_stl",
]
