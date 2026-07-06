"""Small persistent cursor store for polling adapters."""

from __future__ import annotations

import json
from pathlib import Path


class FileOffsetStore:
    def __init__(self, path: str | Path, *, identity: str):
        self._path = Path(path).expanduser()
        self._identity = identity

    def load(self) -> int | None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if data.get("identity") != self._identity:
            return None
        offset = data.get("offset")
        return offset if isinstance(offset, int) and offset >= 0 else None

    def save(self, offset: int | None) -> None:
        if offset is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"identity": self._identity, "offset": offset}),
            encoding="utf-8",
        )
        temporary.replace(self._path)
