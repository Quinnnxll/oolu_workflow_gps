"""Exact reference solids — analytic ground truth for tests and specs.

Each generator's winding is outward by construction (right-hand rule:
counter-clockwise seen from outside), so ``signed_volume`` is positive
and equals the closed-form value exactly up to float summation. The test
suite treats these as theorems to check, not fixtures to trust.
"""

from __future__ import annotations

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
