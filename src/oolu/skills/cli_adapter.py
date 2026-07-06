"""Constrained CLI observation and execution without shell interpretation."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .models import ActionEvent, ExecutionOutcome, ExecutionStatus

# A deliberately small allow-list: enough for real CLI tools to run, but no
# user-specific secrets (no *_TOKEN, *_KEY, APPDATA, USERPROFILE, ...).
# SYSTEMDRIVE and the PROGRAM* / PROCESSOR* vars matter on Windows: a child
# process launched without SYSTEMDRIVE cannot resolve %SystemDrive% and Win32
# shell APIs then materialise a stray ``%SystemDrive%\\ProgramData\\...\\Caches``
# tree *inside the current working directory* — which here is the sandbox
# workspace, silently polluting the deterministic state fingerprint.
_SAFE_ENV_NAMES = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "COMMONPROGRAMFILES",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class CliExecutionPolicy:
    workspace: Path
    allowed_executables: frozenset[str]
    timeout_s: float = 30.0
    environment: dict[str, str] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        workspace: str | Path,
        allowed_executables: list[str],
        timeout_s: float = 30.0,
    ) -> "CliExecutionPolicy":
        root = Path(workspace).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"workspace is not a directory: {root}")
        if not allowed_executables:
            raise ValueError("at least one executable must be explicitly allowed")
        environment = {
            name: os.environ[name] for name in _SAFE_ENV_NAMES if name in os.environ
        }
        return cls(
            workspace=root,
            allowed_executables=frozenset(allowed_executables),
            timeout_s=timeout_s,
            environment=environment,
        )

    def validate_argv(self, argv: list[str]) -> list[str]:
        if not argv:
            raise ValueError("command must not be empty")
        if any("\x00" in value for value in argv):
            raise ValueError("command contains a null byte")
        executable = shutil.which(argv[0], path=self.environment.get("PATH"))
        if executable is None and Path(argv[0]).is_file():
            executable = str(Path(argv[0]))
        if executable is None:
            raise ValueError(f"executable not found: {argv[0]}")
        resolved = Path(executable).resolve()
        allowed = any(
            self._matches_allowed(item, resolved) for item in self.allowed_executables
        )
        if not allowed:
            raise ValueError(f"executable is not allow-listed: {resolved.name}")
        for argument in argv[1:]:
            self._validate_path_argument(argument)
        return [str(resolved), *argv[1:]]

    @staticmethod
    def _matches_allowed(allowed: str, executable: Path) -> bool:
        candidate = Path(allowed).expanduser()
        if candidate.is_absolute():
            return candidate.resolve() == executable
        return candidate.name.casefold() == executable.name.casefold()

    def _validate_path_argument(self, argument: str) -> None:
        if not argument or argument.startswith("-"):
            return
        path = Path(argument)
        looks_like_path = path.is_absolute() or "/" in argument or "\\" in argument
        if not looks_like_path:
            return
        resolved = (
            path.resolve() if path.is_absolute() else (self.workspace / path).resolve()
        )
        if not _inside(resolved, self.workspace):
            raise ValueError(f"path argument escapes workspace: {argument}")


class CliActionExecutor:
    name = "cli"

    def __init__(self, policy: CliExecutionPolicy):
        self._policy = policy
        self._completed: dict[str, ExecutionOutcome] = {}
        self._running: dict[str, subprocess.Popen] = {}
        self._lock = threading.RLock()

    def capabilities(self) -> frozenset[str]:
        return frozenset({"run"})

    def execute(self, action: ActionEvent, *, idempotency_key: str) -> ExecutionOutcome:
        with self._lock:
            if idempotency_key in self._completed:
                return self._completed[idempotency_key]
        if action.adapter != self.name or action.operation != "run":
            return self._outcome(
                action,
                idempotency_key,
                ExecutionStatus.BLOCKED,
                "unsupported CLI action",
            )
        argv = self._policy.validate_argv(list(action.parameters.get("argv", [])))
        started = datetime.now(UTC)
        process = subprocess.Popen(
            argv,
            cwd=self._policy.workspace,
            env=dict(self._policy.environment),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
        )
        with self._lock:
            self._running[idempotency_key] = process
        try:
            stdout, stderr = process.communicate(timeout=self._policy.timeout_s)
            status = (
                ExecutionStatus.SUCCEEDED
                if process.returncode == 0
                else ExecutionStatus.FAILED
            )
            error = (
                None
                if process.returncode == 0
                else f"command exited {process.returncode}"
            )
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            status = ExecutionStatus.FAILED
            error = f"command timed out after {self._policy.timeout_s}s"
        finally:
            with self._lock:
                self._running.pop(idempotency_key, None)
        outcome = ExecutionOutcome(
            idempotency_key=idempotency_key,
            skill_id=str(action.parameters.get("skill_id", "uncompiled")),
            status=status,
            evidence={
                "exit_code": process.returncode,
                "stdout_sha256": hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
                "stderr_sha256": hashlib.sha256(stderr.encode("utf-8")).hexdigest(),
                "stdout_bytes": len(stdout.encode("utf-8")),
                "stderr_bytes": len(stderr.encode("utf-8")),
            },
            error=error,
            started_at=started,
            completed_at=datetime.now(UTC),
        )
        with self._lock:
            self._completed[idempotency_key] = outcome
        return outcome

    def cancel(self, idempotency_key: str) -> None:
        with self._lock:
            process = self._running.get(idempotency_key)
        if process is not None:
            process.kill()

    @staticmethod
    def _outcome(
        action: ActionEvent,
        idempotency_key: str,
        status: ExecutionStatus,
        error: str,
    ) -> ExecutionOutcome:
        now = datetime.now(UTC)
        return ExecutionOutcome(
            idempotency_key=idempotency_key,
            skill_id=str(action.parameters.get("skill_id", "uncompiled")),
            status=status,
            error=error,
            started_at=now,
            completed_at=now,
        )
