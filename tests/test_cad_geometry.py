"""The mesh mathematics, checked against closed forms — proofs, not fixtures.

Every measured quantity is compared to an analytic value derived on paper:
box volumes and areas, the octahedron and corner tetrahedron, torus
topology (χ = 0, genus 1) with volume converging to 2π²Rr² under
refinement, translation invariance of the divergence-theorem volume on
closed meshes, exact sign flip under orientation reversal, and the s³/s²
scaling laws. The topology side is exercised with deliberately broken
meshes: a punctured cube, a flipped facet, a non-manifold fin.
"""

from __future__ import annotations

import struct
from math import cos, pi, sin, sqrt

import pytest

from workflow_gps.domains.cad import (
    Triangle,
    box,
    extents,
    manifold_report,
    parse_stl,
    signed_volume,
    surface_area,
    unit_cube,
    volume,
    write_binary_stl,
)


def _translate(triangles, offset):
    ox, oy, oz = offset

    def move(p):
        return (p[0] + ox, p[1] + oy, p[2] + oz)

    return [Triangle(move(t.a), move(t.b), move(t.c)) for t in triangles]


def _scale(triangles, s):
    def grow(p):
        return (p[0] * s, p[1] * s, p[2] * s)

    return [Triangle(grow(t.a), grow(t.b), grow(t.c)) for t in triangles]


def _octahedron():
    """Vertices ±e_i. V = 4/3 (two unit-height pyramids over a √2 square);
    A = 4√3 (eight equilateral triangles of side √2, each √3/2)."""
    top, bottom = (0.0, 0.0, 1.0), (0.0, 0.0, -1.0)
    ring = [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (-1.0, 0.0, 0.0), (0.0, -1.0, 0.0)]
    triangles = []
    for i in range(4):
        a, b = ring[i], ring[(i + 1) % 4]
        triangles.append(Triangle(a, b, top))  # upper fan, outward
        triangles.append(Triangle(b, a, bottom))  # lower fan, outward
    return triangles


def _corner_tetrahedron():
    """(0, e1, e2, e3): V = 1/6, outward-wound."""
    o, x, y, z = (0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)
    return [
        Triangle(o, y, x),  # bottom (z=0), normal -z
        Triangle(o, x, z),  # side y=0, normal -y
        Triangle(o, z, y),  # side x=0, normal -x
        Triangle(x, y, z),  # slanted face, normal (1,1,1)/√3
    ]


def _torus(major=2.0, minor=0.5, n_major=48, n_minor=24):
    """Parametric torus grid, consistently outward-wound."""

    def point(i, j):
        # Wrap the INDICES, not the angles: sin(2π) != sin(0) in floats,
        # and the seam must reuse bit-identical vertices to weld.
        i, j = i % n_major, j % n_minor
        u = 2.0 * pi * i / n_major
        v = 2.0 * pi * j / n_minor
        w = major + minor * cos(v)
        return (w * cos(u), w * sin(u), minor * sin(v))

    triangles = []
    for i in range(n_major):
        for j in range(n_minor):
            p00 = point(i, j)
            p10 = point(i + 1, j)
            p01 = point(i, j + 1)
            p11 = point(i + 1, j + 1)
            triangles.append(Triangle(p00, p10, p11))
            triangles.append(Triangle(p00, p11, p01))
    return triangles


# --------------------------------------------------------------------------- #
# Measure against closed forms.                                                #
# --------------------------------------------------------------------------- #
def test_box_volume_and_area_match_the_closed_forms():
    a, b, c = 3.0, 5.0, 7.0
    mesh = box((a, b, c))
    assert signed_volume(mesh) == pytest.approx(a * b * c, rel=1e-12)
    assert surface_area(mesh) == pytest.approx(2 * (a * b + b * c + c * a), rel=1e-12)
    assert extents(mesh) == pytest.approx((a, b, c))


def test_octahedron_and_tetrahedron_closed_forms():
    assert signed_volume(_octahedron()) == pytest.approx(4.0 / 3.0, rel=1e-12)
    assert surface_area(_octahedron()) == pytest.approx(4.0 * sqrt(3.0), rel=1e-12)
    assert signed_volume(_corner_tetrahedron()) == pytest.approx(1.0 / 6.0, rel=1e-12)


def test_volume_is_translation_invariant_on_closed_meshes():
    """The divergence-theorem sum is origin-dependent per-triangle; on a
    CLOSED mesh the dependence telescopes to zero. Asserted, not assumed."""
    mesh = box((3.0, 5.0, 7.0))
    moved = _translate(mesh, (17.0, -3.0, 42.0))
    assert signed_volume(moved) == pytest.approx(signed_volume(mesh), rel=1e-9)


def test_orientation_reversal_flips_the_sign_exactly():
    mesh = unit_cube()
    reversed_mesh = [t.reversed() for t in mesh]
    assert signed_volume(reversed_mesh) == pytest.approx(-signed_volume(mesh))
    # ... and area, which carries no orientation, is untouched.
    assert surface_area(reversed_mesh) == pytest.approx(surface_area(mesh))


def test_scaling_laws_volume_cubes_area_squares():
    mesh, s = _octahedron(), 2.5
    grown = _scale(mesh, s)
    assert volume(grown) == pytest.approx(volume(mesh) * s**3, rel=1e-12)
    assert surface_area(grown) == pytest.approx(surface_area(mesh) * s**2, rel=1e-12)


def test_torus_volume_converges_to_two_pi_squared_R_r_squared():
    exact = 2.0 * pi**2 * 2.0 * 0.5**2  # 2π²Rr², R=2, r=1/2
    coarse = volume(_torus(n_major=24, n_minor=12))
    fine = volume(_torus(n_major=96, n_minor=48))
    # Inscribed polyhedra: strictly below the smooth value, and refinement
    # strictly improves — convergence, observed.
    assert coarse < fine < exact
    assert abs(fine - exact) < abs(coarse - exact) / 4  # ~quadratic in h
    assert fine == pytest.approx(exact, rel=0.01)


# --------------------------------------------------------------------------- #
# Topology: certification before trust.                                        #
# --------------------------------------------------------------------------- #
def test_cube_is_a_certified_sphere():
    report = manifold_report(unit_cube())
    assert report.watertight
    assert (report.vertex_count, report.edge_count, report.triangle_count) == (
        8,
        18,
        12,
    )
    assert report.euler_characteristic == 2  # χ of the sphere
    assert report.genus == 0 and report.components == 1


def test_torus_has_euler_characteristic_zero_and_genus_one():
    report = manifold_report(_torus(n_major=12, n_minor=8))
    assert report.watertight
    assert report.euler_characteristic == 0
    assert report.genus == 1


def test_two_disjoint_cubes_chi_is_four_genus_zero():
    mesh = unit_cube() + box(origin=(5.0, 0.0, 0.0))
    report = manifold_report(mesh)
    assert report.watertight and report.components == 2
    assert report.euler_characteristic == 4  # 2c - 2g with c=2, g=0
    assert report.genus == 0


def test_a_punctured_cube_is_not_watertight():
    mesh = unit_cube()[:-1]  # remove one facet: a triangular hole
    report = manifold_report(mesh)
    assert not report.watertight
    assert report.boundary_edges == 3
    assert report.genus is None  # genus is undefined on an open surface


def test_a_flipped_facet_is_caught_as_misorientation():
    mesh = unit_cube()
    mesh[0] = mesh[0].reversed()
    report = manifold_report(mesh)
    assert not report.watertight
    assert report.misoriented_edges == 3


def test_a_fin_is_caught_as_non_manifold():
    mesh = unit_cube()
    corner_a, corner_b = (0.0, 0.0, 0.0), (0.0, 1.0, 0.0)
    mesh.append(Triangle(corner_a, corner_b, (-1.0, 0.5, 0.5)))  # a flap off one edge
    report = manifold_report(mesh)
    assert not report.watertight
    assert report.non_manifold_edges == 1


def test_degenerate_triangles_are_counted_not_trusted():
    mesh = unit_cube()
    mesh.append(Triangle((0.0, 0.0, 0.0), (1.0, 1.0, 1.0), (1.0, 1.0, 1.0)))
    report = manifold_report(mesh)
    assert report.degenerate_triangles == 1
    assert not report.watertight


# --------------------------------------------------------------------------- #
# STL round-trips and format detection.                                        #
# --------------------------------------------------------------------------- #
def test_binary_stl_round_trips_exactly_in_float32():
    mesh = box((2.0, 3.0, 4.0), origin=(1.0, 2.0, 3.0))  # float32-exact values
    parsed = parse_stl(write_binary_stl(mesh))
    assert [t.vertices() for t in parsed] == [t.vertices() for t in mesh]
    assert volume(parsed) == pytest.approx(24.0, rel=1e-6)


def test_ascii_stl_parses():
    text_mesh = "\n".join(
        ["solid demo"]
        + [
            "facet normal 0 0 0\nouter loop\n"
            + "\n".join(f"vertex {v[0]} {v[1]} {v[2]}" for v in tri.vertices())
            + "\nendloop\nendfacet"
            for tri in unit_cube()
        ]
        + ["endsolid demo"]
    )
    parsed = parse_stl(text_mesh.encode())
    assert len(parsed) == 12
    assert volume(parsed) == pytest.approx(1.0, rel=1e-9)


def test_binary_detection_survives_a_header_that_says_solid():
    """The classic trap: binary files whose 80-byte header begins with
    'solid'. Detection is by the exact length equation, never the header."""
    mesh = unit_cube()
    data = bytearray(write_binary_stl(mesh, header="solid trap"))
    assert bytes(data[:5]) == b"solid"
    parsed = parse_stl(bytes(data))
    assert len(parsed) == 12 and volume(parsed) == pytest.approx(1.0, rel=1e-6)


def test_garbage_is_refused_loudly():
    with pytest.raises(ValueError):
        parse_stl(b"\x00\x01\x02 definitely not a mesh")
    with pytest.raises(ValueError):
        parse_stl(struct.pack("<80sI", b"binary header", 99))  # length lies
