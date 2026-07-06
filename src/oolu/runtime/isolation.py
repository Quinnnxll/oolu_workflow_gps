"""Concrete execution backends — the real two-phase lifecycle.

Two implementations of the ``ExecutionBackend`` Protocol, sharing one sequencing
template:

  * ``LocalDockerBackend``  — the v0 isolation target. Ephemeral container, read-only
    rootfs + tmpfs scratch, dropped capabilities, resource ceilings, and a hard
    network split: network is attached ONLY when Phase A has packages to install,
    then severed before any synthesized code runs in Phase B. If severance cannot be
    proven, the run is aborted — untrusted code never executes with network access.

  * ``SubprocessBackend`` — DEV / FALLBACK ONLY. Runs the *real* orchestration (copy
    shim, ``uv pip install --target``, execute, parse contract) via local
    subprocesses so the whole pipeline can be validated without a Docker daemon. It
    is **not** an isolation boundary: it cannot sever the network and shares the host
    kernel and filesystem. Never expose it to untrusted intents in production.

The two-phase sequencing (install -> sever -> execute, short-circuit on install
failure) lives once in ``_TwoPhaseBackend``. Backends implement only the four hooks
that genuinely differ.

Error CLASSIFICATION is deliberately NOT done here. These backends return raw
``ExecutionResult``s (exit code, stdout, stderr, contract parse) with ``error=None``.
Turning stderr into an ``ErrorRecord`` is the classifier's job (next layer), so the
taxonomy lives in exactly one place.
"""

from __future__ import annotations

import base64
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from ..models import ExecutionResult, Phase
from .backend import BackendError, BackendUnavailable, ExecutionRequest
from .contract import SHIM_MODULE_NAME, parse_stdout, shim_source_path

logger = logging.getLogger(__name__)

# Sentinel exit code GNU `timeout` uses when it has to kill the command.
_TIMEOUT_EXIT_CODE = 124

# Path of the in-container runner baked into the sandbox image (docker/entrypoint.py).
_CONTAINER_ENTRYPOINT = "/opt/oolu/entrypoint.py"


@dataclass(frozen=True)
class _RawRun:
    """Backend-agnostic result of running one command (install or execute)."""

    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool


# --------------------------------------------------------------------------- #
# Shared two-phase sequencing.                                                 #
# --------------------------------------------------------------------------- #
class _TwoPhaseBackend(ABC):
    """Template for Phase A (install) -> sever -> Phase B (execute)."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    def health_check(self) -> bool:  # default; overridden where it can really fail
        return True

    def close(self) -> None:  # default no-op
        return None

    # --- hooks each backend fills in ---------------------------------- #
    @abstractmethod
    def _workspace(self, request: ExecutionRequest) -> Iterator[object]:
        """Context manager yielding a workspace handle (tempdir path or container)."""

    @abstractmethod
    def _install(self, ws: object, request: ExecutionRequest) -> _RawRun: ...

    @abstractmethod
    def _sever_network(self, ws: object) -> None:
        """Guarantee no network before Phase B. MUST raise BackendError if it cannot."""

    @abstractmethod
    def _execute(self, ws: object, request: ExecutionRequest) -> _RawRun: ...

    # --- the template ------------------------------------------------- #
    def run(self, request: ExecutionRequest) -> ExecutionResult:
        try:
            with self._workspace(request) as ws:
                if request.dependencies:
                    install = self._install(ws, request)
                    if install.timed_out or install.exit_code != 0:
                        # A package that won't install is a distinct, classifiable
                        # outcome — Phase B is never reached.
                        return self._install_result(install)
                    # Network was only attached for the install; cut it now.
                    self._sever_network(ws)
                execute = self._execute(ws, request)
                return self._execute_result(execute)
        except BackendError:
            raise  # infrastructure failure — surface, never recalc
        except Exception as exc:  # noqa: BLE001 - convert anything unexpected to BackendError
            raise BackendError(
                f"{self.name}: unexpected isolation failure: {exc}"
            ) from exc

    # --- result assembly ---------------------------------------------- #
    @staticmethod
    def _install_result(raw: _RawRun) -> ExecutionResult:
        return ExecutionResult(
            phase=Phase.INSTALL,
            exit_code=raw.exit_code,
            stdout=raw.stdout,
            stderr=raw.stderr,
            duration_s=raw.duration_s,
            timed_out=raw.timed_out,
            contract_ok=False,
        )

    @staticmethod
    def _execute_result(raw: _RawRun) -> ExecutionResult:
        contract = parse_stdout(raw.stdout)
        return ExecutionResult(
            phase=Phase.EXECUTE,
            exit_code=raw.exit_code,
            stdout=raw.stdout,
            stderr=raw.stderr,
            duration_s=raw.duration_s,
            timed_out=raw.timed_out,
            contract_ok=contract.ok,
            contract_payload=contract.payload if contract.ok else None,
        )


# --------------------------------------------------------------------------- #
# SubprocessBackend — dev / fallback, NOT isolation.                          #
# --------------------------------------------------------------------------- #
def _run_command(
    cmd: list[str], *, cwd: str, env: dict[str, str], timeout: float
) -> _RawRun:
    """Run a command in its own process group; on timeout, kill the whole group.

    ``start_new_session=True`` puts the child in a fresh process group so a hung
    grandchild (a stray thread spawning a subprocess, a blocking socket) is killed
    too, not just the immediate child.
    """
    start = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, err = proc.communicate()
    duration = time.monotonic() - start
    exit_code = _TIMEOUT_EXIT_CODE if timed_out else (proc.returncode or 0)
    return _RawRun(
        exit_code=exit_code,
        stdout=out or "",
        stderr=err or "",
        duration_s=duration,
        timed_out=timed_out,
    )


class SubprocessBackend(_TwoPhaseBackend):
    """Runs the real pipeline locally. Dev/fallback only — provides NO isolation."""

    def __init__(
        self,
        *,
        python_executable: str = sys.executable,
        default_index_url: str | None = None,
    ):
        self._python = python_executable
        self._default_index_url = default_index_url
        self._uv = shutil.which("uv")

    @property
    def name(self) -> str:
        return "subprocess"

    def health_check(self) -> bool:
        return bool(self._python) and (
            self._uv is not None or shutil.which("pip") is not None
        )

    @contextmanager
    def _workspace(self, request: ExecutionRequest) -> Iterator[str]:
        d = tempfile.mkdtemp(prefix="oolu-")
        try:
            shutil.copy(shim_source_path(), os.path.join(d, f"{SHIM_MODULE_NAME}.py"))
            Path(d, "user_script.py").write_text(request.script)
            os.makedirs(os.path.join(d, "site-packages"), exist_ok=True)
            yield d
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def _base_env(self, request: ExecutionRequest) -> dict[str, str]:
        # Dev backend: inherits host env for robustness (it is explicitly not a
        # security boundary). The Docker backend, by contrast, inherits nothing.
        env = dict(os.environ)
        env.update(request.env)
        return env

    def _install(self, ws: object, request: ExecutionRequest) -> _RawRun:
        target = os.path.join(str(ws), "site-packages")
        index = request.pinned_index_url or self._default_index_url
        if self._uv:
            cmd = [self._uv, "pip", "install", "--target", target]
        else:
            cmd = [
                self._python,
                "-m",
                "pip",
                "install",
                "--target",
                target,
                "--no-warn-script-location",
            ]
        if index:
            cmd += ["--index-url", index]
        cmd += list(request.dependencies)
        logger.info("subprocess Phase A install: %s", " ".join(request.dependencies))
        return _run_command(
            cmd,
            cwd=str(ws),
            env=self._base_env(request),
            timeout=request.limits.install_timeout_s,
        )

    def _sever_network(self, ws: object) -> None:
        # Cannot sever network for a plain subprocess. This is the entire reason the
        # backend is dev-only; we log loudly rather than pretend otherwise.
        logger.warning(
            "SubprocessBackend cannot sever network — Phase B runs WITHOUT isolation"
        )

    def _execute(self, ws: object, request: ExecutionRequest) -> _RawRun:
        env = self._base_env(request)
        env["PYTHONPATH"] = os.path.join(str(ws), "site-packages")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        logger.info(
            "subprocess Phase B execute (timeout=%.0fs)", request.limits.wall_timeout_s
        )
        return _run_command(
            [self._python, "user_script.py"],
            cwd=str(ws),
            env=env,
            timeout=request.limits.wall_timeout_s,
        )


# --------------------------------------------------------------------------- #
# LocalDockerBackend — the real isolation boundary (v0 default).              #
# --------------------------------------------------------------------------- #
class LocalDockerBackend(_TwoPhaseBackend):
    """Ephemeral hostile containers via the Docker SDK.

    Network policy is the heart of this backend:
      * no dependencies  -> container created with networking fully DISABLED.
      * dependencies      -> created WITH network (Phase A installs from a pinned
        index), then the network is DISCONNECTED and the disconnect VERIFIED before
        Phase B. A failure to sever aborts the run with BackendError.
    """

    def __init__(
        self,
        *,
        image: str = "oolu-sandbox:latest",
        network_name: str = "bridge",
        uv_cache_dir: str | None = None,
        default_index_url: str | None = None,
        run_as_user: str | None = None,
    ):
        try:
            import docker  # lazy: the package must not hard-require docker to import
        except ImportError as exc:
            raise BackendUnavailable(
                "docker SDK not installed (`pip install docker`)"
            ) from exc
        try:
            self._client = docker.from_env()
        except Exception as exc:  # noqa: BLE001
            raise BackendUnavailable(f"cannot reach Docker daemon: {exc}") from exc
        self._docker = docker
        self._image = image
        self._network_name = network_name
        self._uv_cache_dir = uv_cache_dir
        self._default_index_url = default_index_url
        self._run_as_user = run_as_user

    @property
    def name(self) -> str:
        return "local-docker"

    def health_check(self) -> bool:
        try:
            self._client.ping()
            self._client.images.get(self._image)
            return True
        except Exception:  # noqa: BLE001 - any failure means "not ready"
            return False

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass

    # --- workspace = a live, locked-down container -------------------- #
    @contextmanager
    def _workspace(self, request: ExecutionRequest) -> Iterator[object]:
        limits = request.limits
        needs_network = bool(request.dependencies)
        keepalive = limits.install_timeout_s + limits.wall_timeout_s + 60

        volumes = {}
        if self._uv_cache_dir:
            # Shared warm cache across ephemeral containers. NOTE: uv writes to its
            # cache, so a strictly read-only mount can fail installs; mount read-write
            # and rely on uv's content-addressed, hash-verified entries to bound
            # poisoning risk. Leave uv_cache_dir unset to use the per-container tmpfs
            # cache (cold but fully isolated) — the safe default.
            volumes[self._uv_cache_dir] = {"bind": "/uvcache", "mode": "rw"}

        try:
            container = self._client.containers.run(
                image=self._image,
                command=["sleep", str(int(keepalive))],
                detach=True,
                read_only=limits.read_only_rootfs,
                tmpfs={
                    "/sandbox": f"size={limits.writable_scratch_mb}m,exec,mode=1777"
                },
                mem_limit=f"{limits.memory_mb}m",
                nano_cpus=int(limits.cpus * 1_000_000_000),
                pids_limit=limits.pids_limit,
                network_disabled=not needs_network,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                user=self._run_as_user or "",
                working_dir="/sandbox",
                environment={},  # NOTHING inherited — this is how secrets stay out
                labels={"app": "oolu", "session": request.session_id},
            )
        except self._docker.errors.ImageNotFound as exc:
            raise BackendUnavailable(
                f"sandbox image '{self._image}' not found — build docker/sandbox.Dockerfile"
            ) from exc
        except self._docker.errors.DockerException as exc:
            raise BackendError(f"failed to start sandbox container: {exc}") from exc

        try:
            self._put_files(container, request)
            yield container
        finally:
            try:
                container.remove(force=True)  # ephemeral: one container, one intent
            except Exception:  # noqa: BLE001
                logger.warning(
                    "failed to remove sandbox container %s", container.id[:12]
                )

    def _put_files(self, container: object, request: ExecutionRequest) -> None:
        # NOTE: we cannot use Docker's put_archive here. With a read-only rootfs the
        # daemon refuses the copy API ("container rootfs is marked read-only") even
        # when the destination is the writable /sandbox tmpfs. So we stage files
        # through `exec` instead: the tmpfs (mode 1777) is writable from inside the
        # container regardless of the read-only rootfs.
        #
        # Writable working dirs on the tmpfs — the ONLY places uv/python can write:
        # install target, uv cache, temp. They mirror UV_CACHE_DIR / TMPDIR.
        self._stage_exec(
            container,
            [
                "mkdir",
                "-p",
                "/sandbox/site-packages",
                "/sandbox/tmp",
                "/sandbox/.cache/uv",
            ],
        )
        files = {
            f"{SHIM_MODULE_NAME}.py": shim_source_path().read_bytes(),
            "user_script.py": request.script.encode("utf-8"),
        }
        for name, data in files.items():
            b64 = base64.b64encode(data).decode("ascii")
            code = (
                "import base64,pathlib;"
                f"pathlib.Path('/sandbox/{name}').write_bytes(base64.b64decode('{b64}'))"
            )
            self._stage_exec(container, ["python", "-c", code])

    def _stage_exec(self, container: object, cmd: list[str]) -> None:
        """Run a staging command inside the container, raising on failure."""
        exit_code, output = container.exec_run(  # type: ignore[attr-defined]
            cmd,
            demux=True,
            workdir="/sandbox",
        )
        if exit_code not in (0, None):
            out_b, err_b = output if output else (b"", b"")
            detail = (err_b or out_b or b"").decode("utf-8", "replace").strip()
            raise BackendError(
                f"failed to stage sandbox files (`{cmd[0]}`): {detail[:300]}"
            )

    # --- phases ------------------------------------------------------- #
    def _exec(
        self, container: object, cmd: list[str], env: dict[str, str], timeout: float
    ) -> _RawRun:
        wrapped = ["timeout", "-s", "KILL", str(int(timeout)), *cmd]
        start = time.monotonic()
        exit_code, output = container.exec_run(  # type: ignore[attr-defined]
            wrapped,
            demux=True,
            environment=env,
            workdir="/sandbox",
        )
        duration = time.monotonic() - start
        out_b, err_b = output if output else (b"", b"")
        timed_out = exit_code == _TIMEOUT_EXIT_CODE
        return _RawRun(
            exit_code=exit_code if exit_code is not None else -1,
            stdout=(out_b or b"").decode("utf-8", "replace"),
            stderr=(err_b or b"").decode("utf-8", "replace"),
            duration_s=duration,
            timed_out=timed_out,
        )

    def _install(self, ws: object, request: ExecutionRequest) -> _RawRun:
        index = request.pinned_index_url or self._default_index_url
        cmd = ["uv", "pip", "install", "--target", "/sandbox/site-packages"]
        if index:
            cmd += ["--index-url", index]
        cmd += list(request.dependencies)
        env = {"UV_CACHE_DIR": "/uvcache"} if self._uv_cache_dir else {}
        return self._exec(ws, cmd, env, request.limits.install_timeout_s)

    def _sever_network(self, ws: object) -> None:
        """Disconnect every network, then VERIFY none remain. Safety-critical."""
        container = ws
        try:
            network = self._client.networks.get(self._network_name)
            network.disconnect(container, force=True)
        except self._docker.errors.NotFound:
            pass  # not connected to that network — fine
        except Exception as exc:  # noqa: BLE001
            raise BackendError(
                f"could not sever network before Phase B: {exc}"
            ) from exc

        container.reload()  # type: ignore[attr-defined]
        attached = container.attrs.get("NetworkSettings", {}).get("Networks", {})  # type: ignore[attr-defined]
        if attached:
            # We refuse to run untrusted code while any network remains attached.
            raise BackendError(
                f"network still attached after sever attempt: {list(attached)}"
            )

    def _execute(self, ws: object, request: ExecutionRequest) -> _RawRun:
        env = {
            "PYTHONPATH": "/sandbox/site-packages",
            "PYTHONDONTWRITEBYTECODE": "1",
            **request.env,
        }
        # Run via the baked entrypoint: it makes the shim importable and converts
        # uncaught exceptions into structured error envelopes for the classifier.
        cmd = ["python", _CONTAINER_ENTRYPOINT, "/sandbox/user_script.py"]
        return self._exec(ws, cmd, env, request.limits.wall_timeout_s)
