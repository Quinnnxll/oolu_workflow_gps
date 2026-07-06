"""The CAD adapter — deterministic text-to-solid, verified by mathematics.

``OpenSCADExecutor`` is an ordinary ``ActionExecutor`` (adapter name
``cad``) with two operations:

- ``render_stl``: write the action's ``source`` (OpenSCAD text) into the
  work directory and render it to STL through the OpenSCAD binary. The
  render is deterministic — same source, same solid — which is what makes
  CAD an ideal first domain pack: no flaky externality between a node and
  its verification.
- ``verify_geometry``: check a rendered STL against a ``GeometrySpec``
  (see ``verify.py``) — pure Python, no binary needed. A failed predicate
  fails the ACTION, which fails the run, which means no verified success,
  no earnings, and an honest failure in the trace posterior: the whole
  economy's promise, enforced by geometry.

File names in parameters must be bare names (no separators, no dot-dot):
the executor owns its work directory and refuses to be steered outside it.
The binary is configurable as a string or argv prefix, so tests exercise
the real subprocess path through a stub renderer.
"""

from __future__ import annotations

import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

from ...skills.models import ActionEvent, ExecutionOutcome, ExecutionStatus
from .geometry import parse_stl
from .verify import GeometrySpec, verify_stl

_STDERR_TAIL = 400  # enough of a renderer error to act on, not a log dump


def _safe_name(name: str) -> bool:
    return (
        bool(name)
        and "/" not in name
        and "\\" not in name
        and name not in {".", ".."}
        and not name.startswith("~")
    )


class OpenSCADExecutor:
    """Renders and verifies solids inside one owned work directory."""

    name = "cad"

    def __init__(
        self,
        workdir: str | Path,
        *,
        binary: str | Sequence[str] = "openscad",
        timeout_s: float = 120.0,
    ):
        self._workdir = Path(workdir)
        self._workdir.mkdir(parents=True, exist_ok=True)
        self._binary = [binary] if isinstance(binary, str) else list(binary)
        self._timeout = timeout_s
        self._running: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()

    def capabilities(self) -> frozenset[str]:
        return frozenset({"render_stl", "verify_geometry"})

    def cancel(self, idempotency_key: str) -> None:
        with self._lock:
            process = self._running.get(idempotency_key)
        if process is not None:
            process.kill()

    # ------------------------------------------------------------------ #
    def execute(self, action: ActionEvent, *, idempotency_key: str) -> ExecutionOutcome:
        started = datetime.now(UTC)

        def done(
            status: ExecutionStatus, *, error: str | None = None, evidence=None
        ) -> ExecutionOutcome:
            return ExecutionOutcome(
                idempotency_key=idempotency_key,
                skill_id=str(action.parameters.get("skill_id", "cad")),
                status=status,
                error=error,
                evidence=evidence or {},
                started_at=started,
                completed_at=datetime.now(UTC),
            )

        if action.operation == "render_stl":
            return self._render(action, idempotency_key, done)
        if action.operation == "verify_geometry":
            return self._verify(action, done)
        return done(
            ExecutionStatus.FAILED, error=f"unknown cad operation: {action.operation}"
        )

    # ------------------------------------------------------------------ #
    def _render(self, action: ActionEvent, key: str, done) -> ExecutionOutcome:
        source = action.parameters.get("source")
        output = str(action.parameters.get("output", ""))
        if not isinstance(source, str) or not source.strip():
            return done(ExecutionStatus.FAILED, error="render_stl needs 'source' text")
        if not _safe_name(output) or not output.endswith(".stl"):
            return done(
                ExecutionStatus.FAILED,
                error="render_stl needs a bare 'output' file name ending in .stl",
            )
        scad_path = self._workdir / (output[: -len(".stl")] + ".scad")
        stl_path = self._workdir / output
        scad_path.write_text(source, encoding="utf-8")

        command = [*self._binary, "-o", str(stl_path), str(scad_path)]
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self._workdir,
            )
        except OSError as exc:
            return done(ExecutionStatus.FAILED, error=f"cannot start renderer: {exc}")
        with self._lock:
            self._running[key] = process
        try:
            _, stderr = process.communicate(timeout=self._timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            return done(
                ExecutionStatus.FAILED,
                error=f"render timed out after {self._timeout}s",
            )
        finally:
            with self._lock:
                self._running.pop(key, None)

        if process.returncode != 0:
            tail = stderr.decode("utf-8", errors="replace")[-_STDERR_TAIL:].strip()
            return done(
                ExecutionStatus.FAILED,
                error=f"renderer exited {process.returncode}: {tail}",
            )
        if not stl_path.exists():
            return done(ExecutionStatus.FAILED, error="renderer produced no STL file")
        try:
            triangles = parse_stl(stl_path.read_bytes())
        except ValueError as exc:
            return done(ExecutionStatus.FAILED, error=f"unparseable render: {exc}")
        if not triangles:
            return done(ExecutionStatus.FAILED, error="rendered mesh is empty")
        return done(
            ExecutionStatus.SUCCEEDED,
            evidence={"stl": output, "triangles": len(triangles)},
        )

    # ------------------------------------------------------------------ #
    def _verify(self, action: ActionEvent, done) -> ExecutionOutcome:
        stl_name = str(action.parameters.get("stl", ""))
        if not _safe_name(stl_name):
            return done(
                ExecutionStatus.FAILED,
                error="verify_geometry needs a bare 'stl' file name",
            )
        raw_spec = action.parameters.get("spec")
        try:
            spec = GeometrySpec.model_validate(raw_spec or {})
        except Exception as exc:
            return done(ExecutionStatus.FAILED, error=f"bad geometry spec: {exc}")
        stl_path = self._workdir / stl_name
        if not stl_path.exists():
            return done(
                ExecutionStatus.FAILED, error=f"no rendered file named {stl_name!r}"
            )
        report = verify_stl(stl_path.read_bytes(), spec)
        return done(
            ExecutionStatus.SUCCEEDED if report.passed else ExecutionStatus.FAILED,
            error=None if report.passed else "; ".join(report.reasons),
            evidence=report.model_dump(mode="json"),
        )
