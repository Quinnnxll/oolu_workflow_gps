"""Geometric verification — the predicate CAD money moves on.

A ``GeometrySpec`` is a falsifiable claim about a solid: closed-manifold
certification first (no measurement is trusted on a mesh whose closedness
has not been established combinatorially — the volume formula's own
hypothesis), then interval bounds on divergence-theorem volume and surface
area, box-fit bounds on extents, and an exact topological demand on genus.
The report either passes or says precisely which claims failed with the
measured numbers, so a failed verification is evidence, not a shrug.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .geometry import (
    Triangle,
    extents,
    manifold_report,
    parse_stl,
    surface_area,
    volume,
)


class GeometrySpec(BaseModel):
    """What the artifact must be, stated as checkable mathematics."""

    model_config = ConfigDict(frozen=True)

    require_watertight: bool = True
    min_volume: float | None = None
    max_volume: float | None = None
    min_surface_area: float | None = None
    max_surface_area: float | None = None
    # Axis-aligned extent bounds (sizes, not positions): the part must
    # fit within `fits_within` and measure at least `at_least`.
    fits_within: tuple[float, float, float] | None = None
    at_least: tuple[float, float, float] | None = None
    # Exact topology: 0 = a solid without through-holes, 1 = one handle
    # (a plate with a bolt hole), and so on. Implies watertightness —
    # genus is undefined on an open mesh.
    expected_genus: int | None = None


class GeometryReport(BaseModel):
    """Measured facts plus the verdict; rides ExecutionOutcome.evidence."""

    model_config = ConfigDict(frozen=True)

    passed: bool
    reasons: list[str] = Field(default_factory=list)
    triangle_count: int = 0
    watertight: bool = False
    volume: float | None = None  # only reported when the mesh is closed
    surface_area: float = 0.0
    extents: tuple[float, float, float] | None = None
    euler_characteristic: int | None = None
    components: int = 0
    genus: int | None = None


_AXES = "xyz"


def verify_mesh(triangles: list[Triangle], spec: GeometrySpec) -> GeometryReport:
    reasons: list[str] = []
    if not triangles:
        return GeometryReport(passed=False, reasons=["mesh is empty"])

    topology = manifold_report(triangles)
    needs_closed = (
        spec.require_watertight
        or spec.expected_genus is not None
        or spec.min_volume is not None
        or spec.max_volume is not None
    )
    if needs_closed and not topology.watertight:
        for count, what in (
            (topology.degenerate_triangles, "degenerate (zero-area) triangles"),
            (topology.boundary_edges, "boundary edges (holes in the surface)"),
            (topology.non_manifold_edges, "non-manifold edges"),
            (topology.misoriented_edges, "inconsistently oriented edge pairs"),
        ):
            if count:
                reasons.append(f"not watertight: {count} {what}")

    # Volume is only mathematics on a closed mesh; on an open one the
    # divergence-theorem sum is an origin-dependent artifact, so it is
    # withheld rather than reported wrong.
    measured_volume = volume(triangles) if topology.watertight else None
    if measured_volume is not None:
        if spec.min_volume is not None and measured_volume < spec.min_volume:
            reasons.append(
                f"volume {measured_volume:.6g} below minimum {spec.min_volume:.6g}"
            )
        if spec.max_volume is not None and measured_volume > spec.max_volume:
            reasons.append(
                f"volume {measured_volume:.6g} above maximum {spec.max_volume:.6g}"
            )

    area = surface_area(triangles)
    if spec.min_surface_area is not None and area < spec.min_surface_area:
        reasons.append(
            f"surface area {area:.6g} below minimum {spec.min_surface_area:.6g}"
        )
    if spec.max_surface_area is not None and area > spec.max_surface_area:
        reasons.append(
            f"surface area {area:.6g} above maximum {spec.max_surface_area:.6g}"
        )

    size = extents(triangles)
    if spec.fits_within is not None:
        for axis in range(3):
            if size[axis] > spec.fits_within[axis]:
                reasons.append(
                    f"extent {_AXES[axis]}={size[axis]:.6g} exceeds "
                    f"allowed {spec.fits_within[axis]:.6g}"
                )
    if spec.at_least is not None:
        for axis in range(3):
            if size[axis] < spec.at_least[axis]:
                reasons.append(
                    f"extent {_AXES[axis]}={size[axis]:.6g} below "
                    f"required {spec.at_least[axis]:.6g}"
                )

    if spec.expected_genus is not None and topology.watertight:
        if topology.genus != spec.expected_genus:
            reasons.append(
                f"genus {topology.genus} != expected {spec.expected_genus} "
                "(through-hole count is wrong)"
            )

    return GeometryReport(
        passed=not reasons,
        reasons=reasons,
        triangle_count=topology.triangle_count,
        watertight=topology.watertight,
        volume=measured_volume,
        surface_area=area,
        extents=size,
        euler_characteristic=topology.euler_characteristic,
        components=topology.components,
        genus=topology.genus,
    )


def verify_stl(data: bytes, spec: GeometrySpec) -> GeometryReport:
    try:
        triangles = parse_stl(data)
    except ValueError as exc:
        return GeometryReport(passed=False, reasons=[f"unparseable STL: {exc}"])
    return verify_mesh(triangles, spec)
