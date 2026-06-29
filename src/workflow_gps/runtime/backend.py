"""The isolation seam — ``ExecutionBackend`` Protocol and its request specs.

Every synthesized script runs *through* a backend. The backend owns the hostile
two-phase lifecycle (Phase A: install against a pinned index with network; Phase B:
execute with network severed) and hands back an ``ExecutionResult``. Concrete
implementations live elsewhere:

  * ``isolation.LocalDockerBackend`` — plain Docker namespaces (v0 default).
  * a future ``GvisorBackend``       — same Protocol, ``runtime=runsc``, one config flip.
  * ``StubBackend`` (here)           — scripted results for deterministic tests; no
                                       Docker, no code execution.

This module depends only on ``models`` so it sits cleanly below the concrete
backends and can never cause a circular import.

THE CRITICAL DISTINCTION ENCODED HERE
-------------------------------------
A script that fails, raises, or times out is a *normal outcome*: it comes back as
an ``ExecutionResult`` (non-zero exit / ``timed_out=True``) and feeds the recalc
loop. A failure of the isolation layer *itself* — the Docker daemon is down, the
base image is missing, the warm pool is exhausted — is a ``BackendError`` and must
be surfaced, never recalculated. Healing a dead Docker socket in a loop is exactly
the self-harm we are designing against.

Sync on purpose for v0: a blocking ``run`` is trivially debuggable. The warm pool
(``pool.py``) provides concurrency by dispatching backends across a thread executor,
so the per-call interface stays simple. Async can be layered later without changing
this contract.
"""

from __future__ import annotations

from typing import Callable, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from ..models import ExecutionResult, Phase


# --------------------------------------------------------------------------- #
# Backend (infrastructure) failures — distinct from script failures.          #
# --------------------------------------------------------------------------- #
class BackendError(Exception):
    """The isolation layer itself failed. Surface it; do NOT feed it to recalc."""


class BackendUnavailable(BackendError):
    """Backend cannot service the request at all: daemon down, image missing,
    warm pool exhausted, runtime (runsc) not installed."""


# --------------------------------------------------------------------------- #
# Request specs — what the graph asks a backend to run.                        #
# --------------------------------------------------------------------------- #
class ResourceLimits(BaseModel):
    """Hostile-by-default resource ceilings applied to every container.

    Defaults are deliberately tight; a navigation step is expected to be small and
    fast. The graph can widen them per request when a plan legitimately needs more.
    """

    model_config = ConfigDict(frozen=True)

    cpus: float = Field(default=1.0, gt=0, description="Fractional CPU quota.")
    memory_mb: int = Field(
        default=512, gt=0, description="Hard memory cap (OOM-kill above)."
    )
    pids_limit: int = Field(
        default=256, gt=0, description="Process cap — fork-bomb guard."
    )
    wall_timeout_s: float = Field(
        default=30.0, gt=0, description="Hard wall-clock kill for Phase B."
    )
    install_timeout_s: float = Field(
        default=120.0, gt=0, description="Hard wall-clock kill for Phase A."
    )

    # Read-only rootfs is the baseline; the script still needs *somewhere* to write,
    # so a small tmpfs scratch is mounted as the working directory. Both together:
    # the code can scribble in its sandbox but cannot mutate the image or the host.
    read_only_rootfs: bool = Field(
        default=True, description="Mount the container rootfs read-only."
    )
    writable_scratch_mb: int = Field(
        default=64, gt=0, description="Size of the tmpfs work dir."
    )


class ExecutionRequest(BaseModel):
    """One unit of work for a backend: a script plus everything needed to run it
    hostilely. The graph rebuilds this each recalc cycle (e.g. adding a resolved
    package to ``dependencies`` after a ModuleNotFoundError)."""

    model_config = ConfigDict(frozen=True)

    script: str = Field(
        ..., description="The synthesized Python to execute in Phase B."
    )
    dependencies: list[str] = Field(
        default_factory=list,
        description="Resolved PACKAGE names to install in Phase A. Empty => Phase A skipped.",
    )
    pinned_index_url: str | None = Field(
        default=None,
        description="Phase A installs ONLY from here. None => backend default / offline mirror.",
    )
    limits: ResourceLimits = Field(default_factory=ResourceLimits)

    # Traceability only — these are volatile and live nowhere near a cached prompt
    # prefix; the backend uses them solely for logging and container naming.
    session_id: str = "anonymous"
    iteration: int = 0

    # Minimal, explicit environment. Never inherits the host environment — that is
    # how secrets leak into untrusted code. Empty by default.
    env: dict[str, str] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# The Protocol.                                                                #
# --------------------------------------------------------------------------- #
@runtime_checkable
class ExecutionBackend(Protocol):
    """Structural contract every isolation backend satisfies.

    ``run`` performs the full Phase A -> Phase B lifecycle and returns the result of
    the *terminal* phase:

      * Phase A (install) failed  -> ExecutionResult(phase=INSTALL, exit!=0, error=...)
        and Phase B is never reached. A package that won't install is a different
        problem from code that won't run, and the classifier treats it as such.
      * Phase A skipped/succeeded -> ExecutionResult(phase=EXECUTE, ...), the normal
        path, with ``contract_payload`` populated from the script's emit_result.

    ``run`` must NOT raise for script-level failures (those are ExecutionResults). It
    raises ``BackendError`` only when the isolation machinery itself cannot proceed.
    """

    @property
    def name(self) -> str:
        """Short identifier for telemetry, e.g. 'local-docker', 'gvisor', 'stub'."""
        ...

    def health_check(self) -> bool:
        """Cheap readiness probe: daemon reachable, image present, runtime available."""
        ...

    def run(self, request: ExecutionRequest) -> ExecutionResult:
        """Execute one request under isolation. See class docstring for guarantees."""
        ...

    def close(self) -> None:
        """Release held resources (clients, pooled containers). Idempotent."""
        ...


# --------------------------------------------------------------------------- #
# StubBackend — the deterministic test double for the whole recalc loop.       #
# --------------------------------------------------------------------------- #
# A "result factory" is either a ready ExecutionResult or a callable that derives
# one from the incoming request (so a test can say "succeed iff opencv-python is in
# the deps this time").
ResultFactory = ExecutionResult | Callable[[ExecutionRequest], ExecutionResult]


class StubBackend:
    """Returns pre-programmed results in order — no Docker, no code execution.

    Indispensable for testing the graph/recalc logic deterministically: script a
    "fails with missing dependency, then succeeds once the package is added"
    sequence and assert the engine navigates it. Every received request is recorded
    so tests can verify *what* the engine asked for on each cycle (e.g. that Phase A
    was requested with the resolved package on attempt 2).
    """

    def __init__(
        self, results: list[ResultFactory], *, name: str = "stub", healthy: bool = True
    ):
        self._results: list[ResultFactory] = list(results)
        self._name = name
        self._healthy = healthy
        self.requests: list[ExecutionRequest] = []
        self.closed = False

    @property
    def name(self) -> str:
        return self._name

    def health_check(self) -> bool:
        return self._healthy

    def run(self, request: ExecutionRequest) -> ExecutionResult:
        self.requests.append(request)
        if not self._healthy:
            raise BackendUnavailable(f"stub backend '{self._name}' marked unhealthy")
        if not self._results:
            raise BackendUnavailable(
                f"stub backend '{self._name}' has no scripted results left"
            )
        nxt = self._results.pop(0)
        return nxt(request) if callable(nxt) else nxt

    def close(self) -> None:
        self.closed = True


# Convenience builders so tests (and dev/offline modes) don't hand-roll envelopes.
def make_success(payload: dict, *, duration_s: float = 0.01) -> ExecutionResult:
    """A clean Phase-B result carrying a contract payload."""
    return ExecutionResult(
        phase=Phase.EXECUTE,
        exit_code=0,
        contract_ok=True,
        contract_payload=payload,
        duration_s=duration_s,
    )


def make_failure(
    *,
    phase: Phase = Phase.EXECUTE,
    exit_code: int = 1,
    stderr: str = "",
    timed_out: bool = False,
    error=None,
    duration_s: float = 0.01,
) -> ExecutionResult:
    """A failed result. ``error`` (an ErrorRecord) is attached once the classifier
    has run; ``stderr`` is the raw text it will classify."""
    return ExecutionResult(
        phase=phase,
        exit_code=exit_code,
        stderr=stderr,
        timed_out=timed_out,
        contract_ok=False,
        error=error,
        duration_s=duration_s,
    )
