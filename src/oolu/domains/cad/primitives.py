"""Exact reference solids — analytic ground truth for tests and specs.

Each generator's winding is outward by construction (right-hand rule:
counter-clockwise seen from outside), so ``signed_volume`` is positive
and equals the closed-form value exactly up to float summation. The test
suite treats these as theorems to check, not fixtures to trust.
"""

from __future__ import annotations

import math

from .geometry import Triangle, Vec


def box(size: Vec = (1.0, 1.0, 1.0), origin: Vec = (0.0, 0.0, 0.0)) -> list[Triangle]:
    """An axis-aligned box: volume = sx·sy·sz, area = 2(sx·sy + sy·sz + sz·sx)."""
    ox, oy, oz = origin
    sx, sy, sz = size
    if min(sx, sy, sz) <= 0:
        raise ValueError("box dimensions must be positive")

    def p(dx: float, dy: float, dz: float) -> Vec:
        return (ox + dx * sx, oy + dy * sy, oz + dz * sz)

    quads = [
        # (a, b, c, d) counter-clockwise viewed from outside the face.
        (p(0, 0, 0), p(0, 1, 0), p(1, 1, 0), p(1, 0, 0)),  # bottom, -z
        (p(0, 0, 1), p(1, 0, 1), p(1, 1, 1), p(0, 1, 1)),  # top, +z
        (p(0, 0, 0), p(1, 0, 0), p(1, 0, 1), p(0, 0, 1)),  # front, -y
        (p(1, 1, 0), p(0, 1, 0), p(0, 1, 1), p(1, 1, 1)),  # back, +y
        (p(0, 0, 0), p(0, 0, 1), p(0, 1, 1), p(0, 1, 0)),  # left, -x
        (p(1, 0, 0), p(1, 1, 0), p(1, 1, 1), p(1, 0, 1)),  # right, +x
    ]
    triangles: list[Triangle] = []
    for a, b, c, d in quads:
        triangles.append(Triangle(a, b, c))
        triangles.append(Triangle(a, c, d))
    return triangles


def unit_cube() -> list[Triangle]:
    return box()


def rect_plate_with_hole(
    width: float,
    depth: float,
    thickness: float,
    hole_radius: float,
    *,
    segments: int = 64,
) -> list[Triangle]:
    """A w×d×t plate with a centered through-hole — the genus-1 reference.

    The hole is the **inscribed** regular n-gon prism (OpenSCAD's
    ``cylinder($fn = n)`` convention), so the closed forms hold exactly:

        volume = t · (w·d − A_n(r)),   A_n(r) = (n/2)·r²·sin(2π/n)

    Construction: both rims are sampled at the same n ray angles from the
    plate center (the rectangle rim additionally keeps its four exact
    corners), and the top/bottom annuli are triangulated by an angular
    two-pointer march between the rims. Every 2-D rim point is computed
    once and lifted to both z-levels, so shared vertices are bitwise
    identical — the exact-equality welding in ``manifold_report`` sees a
    single closed surface with χ = 0: genus 1, by construction.
    """
    if min(width, depth, thickness) <= 0 or hole_radius <= 0:
        raise ValueError("plate dimensions and hole radius must be positive")
    if 2.0 * hole_radius >= min(width, depth):
        raise ValueError("hole must not breach the plate edge")
    tau = 2.0 * math.pi
    cx, cy = width / 2.0, depth / 2.0

    # The rectangle rim: n ray hits plus the four exact corners, by angle.
    corner_by_angle = {
        math.atan2(y - cy, x - cx) % tau: (x, y)
        for x, y in ((width, depth), (0.0, depth), (0.0, 0.0), (width, 0.0))
    }
    ray_angles = [tau * k / segments for k in range(segments)]
    outer: list[tuple[float, tuple[float, float]]] = []
    for angle in sorted(set(ray_angles) | set(corner_by_angle)):
        if outer and angle - outer[-1][0] < 1e-9:
            if angle in corner_by_angle:  # the corner wins a near-tie
                outer[-1] = (outer[-1][0], corner_by_angle[angle])
            continue
        point = corner_by_angle.get(angle)
        if point is None:
            cos_a, sin_a = math.cos(angle), math.sin(angle)
            reach = min(
                cx / abs(cos_a) if cos_a else math.inf,
                cy / abs(sin_a) if sin_a else math.inf,
            )
            point = (cx + reach * cos_a, cy + reach * sin_a)
        outer.append((angle, point))
    inner = [
        (a, (cx + hole_radius * math.cos(a), cy + hole_radius * math.sin(a)))
        for a in ray_angles
    ]

    def lift(rim: list[tuple[float, tuple[float, float]]], z: float) -> list[Vec]:
        return [(x, y, z) for _, (x, y) in rim]

    o_top, o_bot = lift(outer, thickness), lift(outer, 0.0)
    i_top, i_bot = lift(inner, thickness), lift(inner, 0.0)
    m, n = len(outer), len(inner)

    triangles: list[Triangle] = []
    # Top and bottom annuli: march both rims by angle; each step emits one
    # top triangle (CCW seen from +z) and its bottom mirror.
    i = j = 0
    while i < m or j < n:
        next_outer = outer[i + 1][0] if i + 1 < m else (tau if i < m else math.inf)
        next_inner = inner[j + 1][0] if j + 1 < n else (tau if j < n else math.inf)
        if next_outer <= next_inner:
            a, b, c = i % m, (i + 1) % m, j % n
            triangles.append(Triangle(o_top[a], o_top[b], i_top[c]))
            triangles.append(Triangle(o_bot[a], i_bot[c], o_bot[b]))
            i += 1
        else:
            a, b, c = i % m, (j + 1) % n, j % n
            triangles.append(Triangle(o_top[a], i_top[b], i_top[c]))
            triangles.append(Triangle(o_bot[a], i_bot[c], i_bot[b]))
            j += 1
    # Outer wall (normal away from the center) and hole wall (toward it).
    for k in range(m):
        a, b = k, (k + 1) % m
        triangles.append(Triangle(o_bot[a], o_bot[b], o_top[b]))
        triangles.append(Triangle(o_bot[a], o_top[b], o_top[a]))
    for k in range(n):
        a, b = k, (k + 1) % n
        triangles.append(Triangle(i_bot[b], i_bot[a], i_top[a]))
        triangles.append(Triangle(i_bot[b], i_top[a], i_top[b]))
    return triangles
