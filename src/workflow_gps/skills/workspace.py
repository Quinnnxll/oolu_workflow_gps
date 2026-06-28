"""Deterministic workspace state probing for local CLI skills."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .models import StateSnapshot

_IGNORED_PARTS = frozenset({".git", "__pycache__", ".pytest_cache"})


def workspace_files(root: Path) -> dict[str, dict[str, int | str]]:
    files: dict[str, dict[str, int | str]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if not path.is_file() or any(part in _IGNORED_PARTS for part in relative.parts):
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        files[relative.as_posix()] = {"sha256": digest, "size": path.stat().st_size}
    return files


class WorkspaceProbe:
    name = "workspace"

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise ValueError(f"workspace is not a directory: {self.root}")

    def capture(self) -> StateSnapshot:
        files = workspace_files(self.root)
        payload = json.dumps(files, sort_keys=True, separators=(",", ":"))
        return StateSnapshot(
            fingerprint=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            state={"workspace": str(self.root), "files": files},
        )


def changed_artifacts(
    before: StateSnapshot, after: StateSnapshot
) -> dict[str, dict[str, int | str]]:
    before_files = before.state.get("files", {})
    after_files = after.state.get("files", {})
    return {
        path: details
        for path, details in after_files.items()
        if before_files.get(path) != details
    }
