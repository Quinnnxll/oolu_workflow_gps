"""The CAD ActionExecutor — the engine's first ENGINEERING hand.

Step 5 of the industrial vertical: semantic CAD actions (never UI
clicks) executed through CadQuery/OpenCascade — the deterministic
geometry kernel the spec says a model must never replace. Two
operations:

* ``build`` — one parametric part from a compact feature list (box,
  cylinder, hole, fillet, shell), measured honestly: B-rep validity,
  exact volume, mass under a declared density, center of mass, and
  bounding box — the spec's CAD observation, as structured evidence
  the postcondition evaluator can judge ("mass_kg <= 3.5",
  "bbox_mm/x <= 210"). STEP and STL exports land in the
  content-addressed artifact store, self-verifying by sha256.
* ``assemble`` — parts placed at positions, with REAL interference:
  every pair is boolean-intersected and any shared volume beyond
  tolerance counts — "interference_count == 0" is a measurement here,
  not a hope.

CadQuery is an optional heavy dependency (install the ``cad`` extra).
A host without it refuses in words — a missing kernel is a failed
action with a reason, never a crash. The hand is deterministic and
side-effect-free beyond the artifact store: same features in, same
geometry out, nothing to roll back.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any

from .models import ActionEvent, ExecutionOutcome, ExecutionStatus

# Two solids sharing less volume than this (mm^3) are touching, not
# interfering — the tolerance floor for boolean noise.
INTERFERENCE_TOLERANCE_MM3 = 1e-6

_FEATURE_KINDS = ("box", "cylinder", "hole", "fillet", "shell")


def _require_cadquery():
    import cadquery  # noqa: PLC0415 - the heavy kernel loads on demand

    return cadquery


def build_solid(features: list[dict[str, Any]]):
    """One part from a compact feature list, in declaration order.

    The first feature must create stock (box or cylinder); the rest
    refine it. Dimensions are millimetres. Raises ``ValueError`` in
    words for anything unbuildable — the executor turns that into an
    honest failure."""
    cq = _require_cadquery()
    if not features:
        raise ValueError("no features — a part is at least its stock")
    part = None
    for index, feature in enumerate(features):
        kind = str(feature.get("kind", ""))
        where = f"feature {index + 1} ({kind or 'unnamed'})"
        if kind not in _FEATURE_KINDS:
            raise ValueError(
                f"{where}: unknown kind — one of {', '.join(_FEATURE_KINDS)}"
            )
        try:
            if kind == "box":
                stock = cq.Workplane("XY").box(
                    float(feature["x_mm"]),
                    float(feature["y_mm"]),
                    float(feature["z_mm"]),
                )
                part = stock if part is None else part.union(stock)
            elif kind == "cylinder":
                stock = cq.Workplane("XY").cylinder(
                    float(feature["h_mm"]), float(feature["d_mm"]) / 2.0
                )
                part = stock if part is None else part.union(stock)
            elif part is None:
                raise ValueError(f"{where}: refine what? create stock first")
            elif kind == "hole":
                face = str(feature.get("face", ">Z"))
                selected = part.faces(face).workplane()
                depth = feature.get("depth_mm")
                part = (
                    selected.hole(float(feature["d_mm"]), float(depth))
                    if depth is not None
                    else selected.hole(float(feature["d_mm"]))
                )
            elif kind == "fillet":
                part = part.edges(str(feature.get("edges", "|Z"))).fillet(
                    float(feature["r_mm"])
                )
            elif kind == "shell":
                part = part.faces(str(feature.get("face", ">Z"))).shell(
                    -abs(float(feature["t_mm"]))
                )
        except ValueError:
            raise
        except KeyError as exc:
            raise ValueError(f"{where}: missing dimension {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - geometry kernels throw C++
            raise ValueError(f"{where}: the kernel refused — {exc}") from exc
    return part


def measure(part, density_kg_m3: float | None) -> dict[str, Any]:
    """The observed state — the spec's CAD observation, exactly: what
    the geometry kernel measured, never what anyone hoped."""
    solid = part.val()
    valid = bool(solid.isValid())
    volume_mm3 = float(solid.Volume())
    bb = solid.BoundingBox()
    com = solid.Center()
    observed: dict[str, Any] = {
        "rebuild_successful": valid,
        "solid_ok": valid,
        "volume_cm3": round(volume_mm3 / 1000.0, 6),
        "bbox_mm": {
            "x": round(float(bb.xlen), 6),
            "y": round(float(bb.ylen), 6),
            "z": round(float(bb.zlen), 6),
        },
        "center_of_mass_mm": {
            "x": round(float(com.x), 6),
            "y": round(float(com.y), 6),
            "z": round(float(com.z), 6),
        },
    }
    if density_kg_m3 is not None:
        observed["mass_kg"] = round(
            volume_mm3 * 1e-9 * float(density_kg_m3), 9
        )
    return observed


class CadActionExecutor:
    """``build`` and ``assemble`` for the ``cad`` adapter."""

    name = "cad"

    def __init__(self, *, artifacts=None):
        # durable.FilesystemArtifactStore (or None): where STEP/STL
        # exports land, content-addressed — the same store the file
        # drawer's blobs live in, so surfacing a part later is a row,
        # not a copy.
        self._artifacts = artifacts
        self._completed: dict[str, ExecutionOutcome] = {}
        self._lock = threading.RLock()

    def capabilities(self) -> frozenset[str]:
        return frozenset({"build", "assemble"})

    def cancel(self, idempotency_key: str) -> None:
        return None  # geometry is synchronous and bounded

    # ------------------------------------------------------------------ #
    def execute(self, action: ActionEvent, *, idempotency_key: str) -> ExecutionOutcome:
        with self._lock:
            if idempotency_key in self._completed:
                return self._completed[idempotency_key]
        if action.adapter != self.name or action.operation not in (
            "build",
            "assemble",
        ):
            return self._done(
                action, idempotency_key, ExecutionStatus.BLOCKED,
                error="unsupported CAD action",
            )
        try:
            _require_cadquery()
        except ImportError:
            return self._done(
                action, idempotency_key, ExecutionStatus.FAILED,
                error="cadquery is not installed on this host — install "
                "the 'cad' extra to give the engine its geometry kernel",
            )
        try:
            if action.operation == "build":
                evidence = self._build(action)
            else:
                evidence = self._assemble(action)
        except ValueError as exc:
            return self._done(
                action, idempotency_key, ExecutionStatus.FAILED,
                error=str(exc),
            )
        return self._done(
            action, idempotency_key, ExecutionStatus.SUCCEEDED,
            evidence=evidence,
        )

    # ------------------------------------------------------------------ #
    def _build(self, action: ActionEvent) -> dict[str, Any]:
        params = action.parameters
        part = build_solid(list(params.get("features") or []))
        observed = measure(part, params.get("density_kg_m3"))
        name = str(params.get("name") or "part")
        observed["artifacts"] = self._export(
            part, name, [str(f).lower() for f in (params.get("export") or [])]
        )
        return observed

    def _assemble(self, action: ActionEvent) -> dict[str, Any]:
        params = action.parameters
        specs = list(params.get("parts") or [])
        if len(specs) < 2:
            raise ValueError("an assembly is at least two placed parts")
        density = params.get("density_kg_m3")
        placed: list[tuple[str, Any]] = []
        totals = {"volume_cm3": 0.0, "mass_kg": 0.0}
        for index, spec in enumerate(specs):
            name = str(spec.get("name") or f"part-{index + 1}")
            part = build_solid(list(spec.get("features") or []))
            x, y, z = (float(v) for v in (spec.get("position") or (0, 0, 0)))
            solid = part.val().translate((x, y, z))
            placed.append((name, solid))
            totals["volume_cm3"] += float(solid.Volume()) / 1000.0
            if density is not None:
                totals["mass_kg"] += (
                    float(solid.Volume()) * 1e-9 * float(density)
                )
        interferences: list[dict[str, Any]] = []
        for i in range(len(placed)):
            for j in range(i + 1, len(placed)):
                shared = placed[i][1].intersect(placed[j][1])
                overlap = float(shared.Volume()) if shared is not None else 0.0
                if overlap > INTERFERENCE_TOLERANCE_MM3:
                    interferences.append(
                        {
                            "a": placed[i][0],
                            "b": placed[j][0],
                            "shared_volume_mm3": round(overlap, 6),
                        }
                    )
        observed: dict[str, Any] = {
            "rebuild_successful": True,
            "part_count": len(placed),
            "volume_cm3": round(totals["volume_cm3"], 6),
            "interference_count": len(interferences),
            "interferences": interferences,
        }
        if density is not None:
            observed["mass_kg"] = round(totals["mass_kg"], 9)
        return observed

    # ------------------------------------------------------------------ #
    def _export(
        self, part, name: str, formats: list[str]
    ) -> dict[str, dict[str, Any]]:
        """STEP/STL into the content-addressed store — a build's outputs
        are native, editable artifacts with self-verifying references."""
        if not formats:
            return {}
        if self._artifacts is None:
            raise ValueError(
                "exports need an artifact store and this hand has none"
            )
        import os
        import tempfile

        from cadquery import exporters  # noqa: PLC0415

        media = {"step": "application/step", "stl": "model/stl"}
        out: dict[str, dict[str, Any]] = {}
        stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        with tempfile.TemporaryDirectory() as workdir:
            for fmt in formats:
                if fmt not in media:
                    raise ValueError(
                        f"unknown export format '{fmt}' — step or stl"
                    )
                path = os.path.join(workdir, f"{name}.{fmt}")
                exporters.export(part, path)
                with open(path, "rb") as handle:
                    content = handle.read()
                ref = self._artifacts.put(
                    f"cad/{stamp}/{name}.{fmt}",
                    content,
                    media_type=media[fmt],
                )
                out[fmt] = {"ref": ref, "bytes": len(content)}
        return out

    def _done(
        self,
        action: ActionEvent,
        key: str,
        status: ExecutionStatus,
        *,
        error: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> ExecutionOutcome:
        outcome = ExecutionOutcome(
            idempotency_key=key,
            skill_id=str(action.parameters.get("skill_id", "cad")),
            status=status,
            error=error,
            evidence=evidence or {},
            completed_at=datetime.now(UTC),
        )
        with self._lock:
            self._completed[key] = outcome
        return outcome
