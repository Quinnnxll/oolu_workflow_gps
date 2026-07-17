"""Content-addressed object storage for large evidence and artifacts.

Large blobs do not belong in the run-state row or the audit payload; they live
here and are referenced by a ``sha256:...`` id. Content addressing makes ``put``
idempotent (identical bytes deduplicate) and makes a reference self-verifying.
This filesystem store is the local object-storage adapter; an S3/GCS adapter
implements the same ``ArtifactStore`` port in production. Implements the skill
core's ``ArtifactStore`` protocol.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path


class FilesystemArtifactStore:
    def __init__(self, root: str | Path):
        self._root = Path(root).expanduser()
        self._root.mkdir(parents=True, exist_ok=True)

    def put(self, artifact_id: str, content: bytes, *, media_type: str) -> str:
        digest = hashlib.sha256(content).hexdigest()
        target = self._path(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            # Write atomically so a crash mid-write cannot leave a partial blob
            # under a content-addressed name.
            tmp = target.with_suffix(".tmp")
            tmp.write_bytes(content)
            tmp.replace(target)
            target.with_suffix(".meta").write_text(
                json.dumps(
                    {
                        "artifact_id": artifact_id,
                        "media_type": media_type,
                        "size": len(content),
                    }
                ),
                encoding="utf-8",
            )
        return f"sha256:{digest}"

    def get(self, artifact_ref: str) -> bytes:
        digest = self._digest(artifact_ref)
        return self._path(digest).read_bytes()

    def exists(self, artifact_ref: str) -> bool:
        return self._path(self._digest(artifact_ref)).exists()

    def delete(self, artifact_ref: str) -> bool:
        path = self._path(self._digest(artifact_ref))
        meta = path.with_suffix(".meta")
        existed = path.exists()
        path.unlink(missing_ok=True)
        meta.unlink(missing_ok=True)
        return existed

    def refs(self):
        """Every stored blob as ``(ref, size_bytes, mtime)`` — the walk a
        reachability sweep needs. Filesystem adapter only; an object-store
        adapter without cheap enumeration simply doesn't offer this, and
        the sweep reports blobs as unsupported there instead of guessing."""
        for blob in sorted(self._root.rglob("*")):
            if blob.is_file() and blob.suffix not in {".meta", ".tmp"}:
                stat = blob.stat()
                yield (f"sha256:{blob.name}", stat.st_size, stat.st_mtime)

    def prune(self, *, older_than_seconds: float) -> int:
        """Delete blobs whose last modification is older than the cutoff."""
        cutoff = time.time() - older_than_seconds
        removed = 0
        for blob in self._root.rglob("*"):
            if blob.is_file() and blob.suffix not in {".meta", ".tmp"}:
                if blob.stat().st_mtime < cutoff:
                    blob.with_suffix(".meta").unlink(missing_ok=True)
                    blob.unlink(missing_ok=True)
                    removed += 1
        return removed

    def _path(self, digest: str) -> Path:
        # Shard by the first two hex chars to avoid one huge directory.
        return self._root / digest[:2] / digest

    @staticmethod
    def _digest(artifact_ref: str) -> str:
        if not artifact_ref.startswith("sha256:"):
            raise ValueError(f"unsupported artifact reference: {artifact_ref}")
        return artifact_ref.split(":", 1)[1]
