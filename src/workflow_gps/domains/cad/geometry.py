"""Exact mesh mathematics — the measurements CAD verification stands on.

Everything here is classical geometry with stated hypotheses, not
heuristics. The two load-bearing results:

VOLUME (divergence theorem).
    For a closed surface S, triangulated with consistent outward
    orientation and bounding a region Ω, apply the divergence theorem to
    the field F(x) = x/3 (div F = 1):

        vol(Ω) = ∫_Ω div F dV = ∮_S F · n dA
               = Σ_T (1/6) · v0 · (v1 × v2)

    — each triangle contributes the signed volume of the tetrahedron it
    spans with the origin. The hypotheses matter: *closed* makes the sum
    origin-independent (translating every vertex changes each term, but
    the changes telescope to zero over a closed surface — asserted
    numerically in the tests, not assumed), and *consistently oriented*
    makes interior cancellations exact. On a mesh violating them the
    number is meaningless, which is why ``ManifoldReport`` exists: the
    verifier refuses to trust volume on a mesh whose closedness it has
    not established combinatorially.

TOPOLOGY (Euler characteristic).
    A triangulation is a closed orientable 2-manifold exactly when every
    undirected edge is shared by exactly two triangles *with opposite
    directions* (consistent orientation), with no degenerate triangles.
    On such a mesh with V vertices, E edges, F faces and c connected
    components, the Euler characteristic χ = V − E + F satisfies
    χ = 2c − 2g, so the total genus g = c − χ/2 is a computable integer:
    a solid plate reports genus 0, a plate with a through-hole reports
    genus 1, and a mesh whose χ has the wrong parity is telling you it
    is not the surface you think it is.

Vertices are identified by exact coordinate equality — mesh producers
(OpenSCAD included) emit bit-identical coordinates for shared vertices.
Welding nearly-equal vertices is a repair operation, deliberately out of
scope for a *verifier*: repairing evidence before judging it would be
editorializing.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from math import sqrt
from typing import Iterable, Sequence

Vec = tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class Triangle:
    """One oriented facet; the winding (a, b, c) defines the normal side."""

    a: Vec
    b: Vec
    c: Vec

    def vertices(self) -> tuple[Vec, Vec, Vec]:
        return (self.a, self.b, self.c)

    def reversed(self) -> "Triangle":
        return Triangle(self.a, self.c, self.b)


def _sub(u: Vec, v: Vec) -> Vec:
    return (u[0] - v[0], u[1] - v[1], u[2] - v[2])


def _cross(u: Vec, v: Vec) -> Vec:
    return (
        u[1] * v[2] - u[2] * v[1],
        u[2] * v[0] - u[0] * v[2],
        u[0] * v[1] - u[1] * v[0],
    )


def _dot(u: Vec, v: Vec) -> float:
    return u[0] * v[0] + u[1] * v[1] + u[2] * v[2]


def _norm(u: Vec) -> float:
    return sqrt(_dot(u, u))


# --------------------------------------------------------------------------- #
# Measure.                                                                     #
# --------------------------------------------------------------------------- #
def signed_volume(triangles: Sequence[Triangle]) -> float:
    """Σ v0·(v1×v2)/6 — exact for closed, consistently outward-wound meshes.

    Positive when the winding is outward, negated by reversing it. Only
    meaningful under the closed-manifold hypotheses documented in the
    module docstring; check ``manifold_report(...).watertight`` first.
    """
    total = 0.0
    for tri in triangles:
        total += _dot(tri.a, _cross(tri.b, tri.c))
    return total / 6.0


def volume(triangles: Sequence[Triangle]) -> float:
    return abs(signed_volume(triangles))


def surface_area(triangles: Sequence[Triangle]) -> float:
    """Σ ‖(b−a)×(c−a)‖/2 — the cross product's length is twice the
    triangle's area, with no orientation or closedness hypothesis."""
    total = 0.0
    for tri in triangles:
        total += _norm(_cross(_sub(tri.b, tri.a), _sub(tri.c, tri.a)))
    return total / 2.0


def bounding_box(triangles: Sequence[Triangle]) -> tuple[Vec, Vec]:
    if not triangles:
        raise ValueError("an empty mesh has no bounding box")
    points = [p for tri in triangles for p in tri.vertices()]
    low = tuple(min(p[i] for p in points) for i in range(3))
    high = tuple(max(p[i] for p in points) for i in range(3))
    return low, high  # type: ignore[return-value]


def extents(triangles: Sequence[Triangle]) -> Vec:
    low, high = bounding_box(triangles)
    return (high[0] - low[0], high[1] - low[1], high[2] - low[2])


# --------------------------------------------------------------------------- #
# Topology.                                                                    #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ManifoldReport:
    """The combinatorial facts a verifier needs before trusting geometry."""

    triangle_count: int
    vertex_count: int
    edge_count: int
    components: int
    euler_characteristic: int
    boundary_edges: int  # undirected edges used by exactly one triangle
    non_manifold_edges: int  # used by three or more
    misoriented_edges: int  # used twice in the SAME direction
    degenerate_triangles: int  # zero area: no orientation to speak of

    @property
    def watertight(self) -> bool:
        """Closed orientable 2-manifold, combinatorially certified."""
        return (
            self.triangle_count > 0
            and self.degenerate_triangles == 0
            and self.boundary_edges == 0
            and self.non_manifold_edges == 0
            and self.misoriented_edges == 0
        )

    @property
    def genus(self) -> int | None:
        """Total genus from χ = 2c − 2g; None when the mesh is not a
        closed orientable surface (the formula's hypothesis) or χ has
        the wrong parity (the mesh is lying about being one)."""
        if not self.watertight:
            return None
        doubled = 2 * self.components - self.euler_characteristic
        if doubled < 0 or doubled % 2:
            return None
        return doubled // 2


def manifold_report(triangles: Sequence[Triangle]) -> ManifoldReport:
    degenerate = 0
    directed: dict[tuple[Vec, Vec], int] = {}
    vertices: dict[Vec, int] = {}

    def vertex_id(p: Vec) -> int:
        return vertices.setdefault(p, len(vertices))

    parent: list[int] = []

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for tri in triangles:
        area2 = _norm(_cross(_sub(tri.b, tri.a), _sub(tri.c, tri.a)))
        if area2 == 0.0:
            degenerate += 1
            continue
        ids = []
        for p in tri.vertices():
            i = vertex_id(p)
            while len(parent) < len(vertices):
                parent.append(len(parent))
            ids.append(i)
        union(ids[0], ids[1])
        union(ids[1], ids[2])
        for u, v in ((tri.a, tri.b), (tri.b, tri.c), (tri.c, tri.a)):
            directed[(u, v)] = directed.get((u, v), 0) + 1

    boundary = non_manifold = misoriented = 0
    undirected: set[tuple[Vec, Vec]] = set()
    for (u, v), forward in directed.items():
        key = (u, v) if (u, v) <= (v, u) else (v, u)
        if key in undirected:
            continue
        undirected.add(key)
        backward = directed.get((v, u), 0)
        total = forward + backward
        if total == 1:
            boundary += 1
        elif total > 2:
            non_manifold += 1
        elif forward == 2 or backward == 2:
            misoriented += 1

    components = len({find(i) for i in range(len(vertices))})
    face_count = sum(1 for _ in triangles) - degenerate
    return ManifoldReport(
        triangle_count=face_count,
        vertex_count=len(vertices),
        edge_count=len(undirected),
        components=components,
        euler_characteristic=len(vertices) - len(undirected) + face_count,
        boundary_edges=boundary,
        non_manifold_edges=non_manifold,
        misoriented_edges=misoriented,
        degenerate_triangles=degenerate,
    )


# --------------------------------------------------------------------------- #
# STL: the interchange format both directions.                                 #
# --------------------------------------------------------------------------- #
_ASCII_VERTEX_RE = re.compile(r"vertex\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)")


def parse_stl(data: bytes) -> list[Triangle]:
    """Parse binary or ASCII STL.

    Binary is detected by its exact length equation (84 + 50·n bytes) —
    NOT by the header, because real-world binary files often begin with
    the bytes ``solid`` and header-sniffing misparses them.
    """
    if len(data) >= 84:
        (count,) = struct.unpack_from("<I", data, 80)
        if len(data) == 84 + 50 * count:
            triangles = []
            offset = 84
            for _ in range(count):
                values = struct.unpack_from("<12f", data, offset)
                triangles.append(Triangle(values[3:6], values[6:9], values[9:12]))
                offset += 50
            return triangles
    text = data.decode("utf-8", errors="replace")
    if "facet" in text or text.lstrip().startswith("solid"):
        coordinates = [
            (float(x), float(y), float(z)) for x, y, z in _ASCII_VERTEX_RE.findall(text)
        ]
        if len(coordinates) % 3:
            raise ValueError("ASCII STL vertex count is not a multiple of 3")
        return [
            Triangle(coordinates[i], coordinates[i + 1], coordinates[i + 2])
            for i in range(0, len(coordinates), 3)
        ]
    raise ValueError("not a recognizable STL (neither binary-sized nor ASCII)")


def write_binary_stl(triangles: Iterable[Triangle], *, header: str = "wfgps") -> bytes:
    """Serialize to binary STL with true unit normals (zero for degenerate)."""
    body = bytearray()
    count = 0
    for tri in triangles:
        normal = _cross(_sub(tri.b, tri.a), _sub(tri.c, tri.a))
        length = _norm(normal)
        unit = (
            (0.0, 0.0, 0.0)
            if length == 0.0
            else (
                normal[0] / length,
                normal[1] / length,
                normal[2] / length,
            )
        )
        body += struct.pack("<12fH", *unit, *tri.a, *tri.b, *tri.c, 0)
        count += 1
    head = header.encode("utf-8")[:80].ljust(80, b"\0")
    return head + struct.pack("<I", count) + bytes(body)
