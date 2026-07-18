"""Node bundles — content-addressed, frozen source trees for fast boots.

A node can grow from one ``src/main.py`` into a whole tree: a cloned
professional library, hundreds of modules and data files. Staging that
tree the naive way — read every drawer row inline on every run, serialize
the bytes into the run state, and copy them into the sandbox one file at a
time — makes boot cost grow with the codebase and re-pays the whole price
on every idle re-run. This module is the architecture that flattens both
curves.

The unit is a **bundle**: a node's ``src/`` tree frozen once into an
immutable, content-addressed object.

* **Freeze once, address by content.** Each file's bytes are hashed and
  stored in the content-addressed object store (the same CAS that backs
  the file drawer's blobs); the *manifest* — the sorted list of
  ``(path, sha256, size)`` — is itself hashed to a ``bundle_id``. Two
  nodes cloned from the same software share every identical blob, and two
  identical trees are the same bundle: dedup is free and automatic.

* **Ship the reference, not the bytes.** A run carries the ``bundle_id``
  and its small manifest, never the tree's contents, so the durable run
  state stays tiny no matter how large the node is.

* **Pack once, reuse across runs.** Materializing a bundle for the sandbox
  produces a single tar of the whole tree; that tar is cached by
  ``bundle_id`` (a bounded, idle-evictable LRU), so an unchanged node is
  packed exactly once and every later run reuses it. Staging then costs
  ONE archive extraction in the container, not one round-trip per file.

The security posture is unchanged: bundle bytes are the same ``src/``
files that already passed the drawer's walls, they are still extracted
into the network-severed sandbox's writable scratch, and the script that
imports them is still screened and verified by execution. A bundle is a
faster shape for trusted-by-the-same-rules code, not a new trust path.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import PurePosixPath

# A node's tree is programs and data, not a data lake. These ceilings keep a
# runaway clone from turning a bundle into a denial-of-service; large binary
# payloads belong in path-roled slots and the blob door, not the code tree.
MAX_BUNDLE_FILES = 4096
MAX_BUNDLE_BYTES = 64 * 1024 * 1024  # 64 MiB of source across the whole tree


class BundleError(ValueError):
    """A tree that cannot be a bundle: too big, or an unsafe path."""


@dataclass(frozen=True, slots=True)
class BundleEntry:
    """One file in a frozen tree: where it sits, and what it is."""

    path: str  # drawer-relative POSIX path, e.g. "pkg/util.py"
    sha256: str  # of the file's bytes — the CAS key
    size: int


@dataclass(frozen=True, slots=True)
class BundleManifest:
    """The whole frozen tree, by reference — small enough to ship anywhere.

    ``bundle_id`` is the sha256 of the canonical manifest, so it changes iff
    any path or any file's content changes: the identity a run ships and the
    prepared cache keys on.
    """

    bundle_id: str
    entries: tuple[BundleEntry, ...]
    total_bytes: int
    file_count: int

    def to_json(self) -> str:
        return json.dumps(
            {
                "bundle_id": self.bundle_id,
                "entries": [
                    {"path": e.path, "sha256": e.sha256, "size": e.size}
                    for e in self.entries
                ],
                "total_bytes": self.total_bytes,
                "file_count": self.file_count,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, blob: str) -> "BundleManifest":
        data = json.loads(blob)
        return cls(
            bundle_id=data["bundle_id"],
            entries=tuple(
                BundleEntry(path=e["path"], sha256=e["sha256"], size=e["size"])
                for e in data["entries"]
            ),
            total_bytes=int(data["total_bytes"]),
            file_count=int(data["file_count"]),
        )


def _safe_path(raw: str) -> str:
    """A drawer-relative POSIX path that cannot escape the tree — the same
    discipline the sandbox staging enforces, applied at freeze time so an
    unsafe path never even becomes a bundle."""
    name = str(raw).replace("\\", "/")
    pure = PurePosixPath(name)
    if pure.is_absolute() or any(part in ("..", "", ".") for part in pure.parts):
        raise BundleError(f"unsafe bundle path: {raw!r}")
    if ":" in name:
        raise BundleError(f"unsafe bundle path: {raw!r}")
    return str(pure)


def _canonical_manifest(entries: list[BundleEntry]) -> str:
    """The bytes the bundle_id hashes — sorted, so tree identity is order-
    independent (the same files always freeze to the same id)."""
    ordered = sorted(entries, key=lambda e: e.path)
    return json.dumps(
        [[e.path, e.sha256, e.size] for e in ordered],
        separators=(",", ":"),
        ensure_ascii=False,
    )


def freeze_tree(files: dict[str, str | bytes]) -> tuple[BundleManifest, dict[str, bytes]]:
    """Freeze a ``{path: content}`` tree into a manifest plus the blobs to
    store, keyed by digest (so identical files are stored once).

    Raises :class:`BundleError` for an unsafe path or a tree over the
    ceilings — the freeze is where a bad tree is refused, before any run
    ever carries it."""
    entries: list[BundleEntry] = []
    blobs: dict[str, bytes] = {}
    total = 0
    for raw_path, content in files.items():
        path = _safe_path(raw_path)
        data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        digest = hashlib.sha256(data).hexdigest()
        total += len(data)
        entries.append(BundleEntry(path=path, sha256=digest, size=len(data)))
        blobs[digest] = data
    if len(entries) > MAX_BUNDLE_FILES:
        raise BundleError(
            f"a bundle holds at most {MAX_BUNDLE_FILES} files ({len(entries)} given)"
        )
    if total > MAX_BUNDLE_BYTES:
        raise BundleError(
            f"a bundle holds at most {MAX_BUNDLE_BYTES} bytes ({total} given)"
        )
    bundle_id = hashlib.sha256(_canonical_manifest(entries).encode("utf-8")).hexdigest()
    manifest = BundleManifest(
        bundle_id=bundle_id,
        entries=tuple(sorted(entries, key=lambda e: e.path)),
        total_bytes=total,
        file_count=len(entries),
    )
    return manifest, blobs


# --------------------------------------------------------------------------- #
# Packing — the whole tree as one deterministic archive.                       #
# --------------------------------------------------------------------------- #
def pack_tar(tree: dict[str, bytes]) -> bytes:
    """One tar of a ``{path: bytes}`` tree — deterministic (sorted, fixed
    mtime/mode), so the same tree always packs to identical bytes. This is
    the single object the sandbox extracts in one step."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for path in sorted(tree):
            data = tree[path]
            info = tarfile.TarInfo(name=path)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            info.uid = info.gid = 0
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def unpack_tar(archive: bytes) -> dict[str, bytes]:
    """The inverse of :func:`pack_tar` — used by the subprocess backend and
    by tests to read a packed tree back."""
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            _safe_path(member.name)  # a tar must not carry an escaping path
            extracted = tar.extractfile(member)
            out[member.name] = extracted.read() if extracted else b""
    return out


@dataclass(frozen=True, slots=True)
class PreparedBundle:
    """A bundle ready to stage: its id and the single packed tar of the tree.

    The heavy, reusable artifact — built once per ``bundle_id`` and handed
    to the backend, which extracts it in one operation."""

    bundle_id: str
    tar: bytes
    file_count: int
    total_bytes: int


# --------------------------------------------------------------------------- #
# The store — bytes in the CAS, manifests in a small table.                    #
# --------------------------------------------------------------------------- #
_SCHEMA = """CREATE TABLE IF NOT EXISTS node_bundles (
    bundle_id TEXT PRIMARY KEY,
    manifest_json TEXT NOT NULL,
    created_at TEXT NOT NULL
)"""


class BundleStore:
    """Freezes trees into the content-addressed store and reads them back.

    Blobs live in the ``artifacts`` CAS (dedup across every node and
    version); manifests live in one small table keyed by ``bundle_id``.
    ``freeze`` is idempotent: an unchanged tree re-freezes to the same id
    and stores nothing new.
    """

    def __init__(self, conn, artifacts) -> None:
        self._conn = conn
        self._artifacts = artifacts  # durable.FilesystemArtifactStore (CAS)
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    @property
    def artifacts(self):
        """The content-addressed store this bundle store's blobs live in —
        what a sweep walks to reclaim dead bytes."""
        return self._artifacts

    def freeze(self, files: dict[str, str | bytes]) -> BundleManifest:
        """Freeze a tree, storing any not-yet-stored blobs and the manifest."""
        manifest, blobs = freeze_tree(files)
        by_path = {e.path: e for e in manifest.entries}
        for path, entry in by_path.items():
            ref = f"sha256:{entry.sha256}"
            if not self._artifacts.exists(ref):
                self._artifacts.put(
                    entry.sha256, blobs[entry.sha256], media_type="application/octet-stream"
                )
        from datetime import UTC, datetime

        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO node_bundles (bundle_id, manifest_json, created_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(bundle_id) DO NOTHING""",
                (manifest.bundle_id, manifest.to_json(), datetime.now(UTC).isoformat()),
            )
        return manifest

    def manifest(self, bundle_id: str) -> BundleManifest | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT manifest_json FROM node_bundles WHERE bundle_id = ?",
                (bundle_id,),
            ).fetchone()
        return BundleManifest.from_json(row["manifest_json"]) if row else None

    def manifests(self):
        """Every stored manifest with its ``created_at`` string, oldest
        first — the sweep's worklist for deciding which frozen trees are
        still reachable."""
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT manifest_json, created_at FROM node_bundles"
                " ORDER BY created_at"
            ).fetchall()
        out = []
        for row in rows:
            out.append(
                (BundleManifest.from_json(row["manifest_json"]), row["created_at"])
            )
        return out

    def blob_refs_of(self, bundle_ids) -> set[str]:
        """Every CAS ref (``sha256:...``) the named bundles' manifests
        reference — the sweep marks these reachable so a live bundle's
        bytes are never swept."""
        wanted = set(bundle_ids)
        refs: set[str] = set()
        for manifest, _created in self.manifests():
            if manifest.bundle_id in wanted:
                for entry in manifest.entries:
                    refs.add(f"sha256:{entry.sha256}")
        return refs

    def forget(self, bundle_ids) -> int:
        """Drop dead manifest rows (the blobs are the CAS sweep's job).
        Returns how many manifests were removed."""
        removed = 0
        with self._conn.transaction() as db:
            for bundle_id in set(bundle_ids):
                cursor = db.execute(
                    "DELETE FROM node_bundles WHERE bundle_id = ?", (bundle_id,)
                )
                removed += int(getattr(cursor, "rowcount", 0) or 0)
        return removed

    def materialize(self, manifest: BundleManifest) -> dict[str, bytes]:
        """The tree's bytes, read from the CAS — the slow path a prepared
        cache exists to spare repeated runs."""
        return {
            e.path: self._artifacts.get(f"sha256:{e.sha256}")
            for e in manifest.entries
        }

    def prepare(self, bundle_id: str) -> PreparedBundle | None:
        """Read a bundle from storage and pack it — the cache-miss path."""
        manifest = self.manifest(bundle_id)
        if manifest is None:
            return None
        tree = self.materialize(manifest)
        return PreparedBundle(
            bundle_id=bundle_id,
            tar=pack_tar(tree),
            file_count=manifest.file_count,
            total_bytes=manifest.total_bytes,
        )


# --------------------------------------------------------------------------- #
# The warm tier - packed tars on disk, surviving restart.                       #
# --------------------------------------------------------------------------- #
class WarmBundleTier:
    """A bounded, on-disk store of packed bundle tars, keyed by ``bundle_id``.

    The in-memory tier is fast but forgetful: a restart or a deploy loses
    every packed bundle, so the first run of each node after a bounce
    re-reads the whole tree from the CAS and re-packs it. This tier is the
    memory the process does not have - packed tars land in a content-
    addressed directory and outlive the process, so a node that ran before
    the restart stages warm on its very first run after it.

    Bounded by total bytes and evicted least-recently-used (by file mtime,
    which every read touches), so the directory never grows without limit.
    Each tar is self-verifying: its ``bundle_id`` fixes the tree it must
    contain, so a truncated or tampered file is detected on read and simply
    re-prepared - the CAS is always the durable truth, the disk tier only
    an accelerator that can be wrong without ever being unsafe.
    """

    def __init__(self, root, *, max_bytes: int = 1024 * 1024 * 1024) -> None:
        from pathlib import Path

        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._max = max_bytes

    def _path(self, bundle_id: str):
        # Shard by the first two hex chars so one directory never holds a
        # host's every bundle.
        return self._root / bundle_id[:2] / f"{bundle_id}.tar"

    def get(self, bundle_id: str) -> bytes | None:
        path = self._path(bundle_id)
        try:
            tar = path.read_bytes()
        except (FileNotFoundError, NotADirectoryError):
            return None
        # Self-verify: the tar must contain exactly the tree the id names.
        if not _tar_matches_bundle(tar, bundle_id):
            path.unlink(missing_ok=True)
            return None
        _touch(path)  # LRU-on-read
        return tar

    def put(self, bundle_id: str, tar: bytes) -> None:
        if len(tar) > self._max:
            return  # one tar larger than the whole budget is never warmed
        path = self._path(bundle_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tar.tmp")
        tmp.write_bytes(tar)
        tmp.replace(path)  # atomic: a reader never sees a partial tar
        self._evict_to_budget()

    def discard(self, bundle_id: str) -> bool:
        """Drop one bundle's warm tar — the sweep's hand: when a bundle's
        manifest dies, its accelerator copies should not linger until
        budget pressure happens to reach them. Missing is fine (another
        host, or eviction, got there first)."""
        path = self._path(bundle_id)
        existed = path.is_file()
        path.unlink(missing_ok=True)
        return existed

    def _evict_to_budget(self) -> None:
        files = [p for p in self._root.glob("*/*.tar") if p.is_file()]
        total = sum(p.stat().st_size for p in files)
        if total <= self._max:
            return
        for path in sorted(files, key=lambda p: p.stat().st_mtime):
            if total <= self._max:
                break
            size = path.stat().st_size
            path.unlink(missing_ok=True)
            total -= size

    @property
    def resident_bytes(self) -> int:
        return sum(
            p.stat().st_size for p in self._root.glob("*/*.tar") if p.is_file()
        )


def _touch(path) -> None:
    import os
    import time

    try:
        os.utime(path, (time.time(), time.time()))
    except OSError:
        pass


def _tar_matches_bundle(tar: bytes, bundle_id: str) -> bool:
    """True iff ``tar`` unpacks to exactly the tree ``bundle_id`` names -
    the disk tier's integrity check, so a corrupt warm file is caught and
    re-prepared instead of trusted."""
    try:
        tree = unpack_tar(tar)
    except Exception:  # noqa: BLE001 - an unreadable tar is simply a miss
        return False
    entries = [
        BundleEntry(path=path, sha256=hashlib.sha256(data).hexdigest(), size=len(data))
        for path, data in tree.items()
    ]
    recomputed = hashlib.sha256(
        _canonical_manifest(entries).encode("utf-8")
    ).hexdigest()
    return recomputed == bundle_id


# --------------------------------------------------------------------------- #
# The prepared cache - pack once, reuse across runs, evict when idle.          #
# --------------------------------------------------------------------------- #
class PreparedBundleCache:
    """A two-tier cache of packed bundles, keyed by ``bundle_id``.

    The boot-speed win for repeated runs, made durable:

    - **Memory** - a bounded in-process LRU: the fastest path, where a hot
      node stages with no I/O at all.
    - **Disk** (optional ``warm``) - packed tars that outlive the process,
      so the first run of a node AFTER a restart still stages warm instead
      of re-reading its whole tree from the CAS.
    - **CAS** - the durable truth. A miss in both tiers reads the manifest
      and blobs and packs the tar once, then writes it back UP both tiers.

    Every tier is bounded and freely evictable: an eviction only costs the
    next run one re-pack, never correctness.
    """

    def __init__(
        self,
        store: BundleStore,
        *,
        max_bytes: int = 256 * 1024 * 1024,
        warm: "WarmBundleTier | None" = None,
    ) -> None:
        self._store = store
        self._max = max_bytes
        self._warm = warm
        self._entries: OrderedDict[str, PreparedBundle] = OrderedDict()
        self._bytes = 0
        self.hits = 0  # memory hits
        self.warm_hits = 0  # disk hits (a restart-surviving reuse)
        self.misses = 0  # neither tier had it - packed from the CAS

    def get(self, bundle_id: str) -> PreparedBundle | None:
        cached = self._entries.get(bundle_id)
        if cached is not None:
            self._entries.move_to_end(bundle_id)
            self.hits += 1
            return cached
        # The disk tier: a packed tar that survived the process.
        if self._warm is not None:
            tar = self._warm.get(bundle_id)
            if tar is not None:
                prepared = self._from_tar(bundle_id, tar)
                self.warm_hits += 1
                self._admit(prepared, warm=False)  # already on disk
                return prepared
        prepared = self._store.prepare(bundle_id)
        self.misses += 1
        if prepared is not None:
            self._admit(prepared, warm=True)
        return prepared

    @staticmethod
    def _from_tar(bundle_id: str, tar: bytes) -> PreparedBundle:
        tree = unpack_tar(tar)
        return PreparedBundle(
            bundle_id=bundle_id,
            tar=tar,
            file_count=len(tree),
            total_bytes=sum(len(v) for v in tree.values()),
        )

    def _admit(self, prepared: PreparedBundle, *, warm: bool) -> None:
        if warm and self._warm is not None:
            self._warm.put(prepared.bundle_id, prepared.tar)
        # A single bundle larger than the whole budget is served but never
        # cached in memory - it would evict everything and still not fit.
        if len(prepared.tar) > self._max:
            return
        self._entries[prepared.bundle_id] = prepared
        self._entries.move_to_end(prepared.bundle_id)
        self._bytes += len(prepared.tar)
        while self._bytes > self._max and len(self._entries) > 1:
            _, evicted = self._entries.popitem(last=False)
            self._bytes -= len(evicted.tar)

    @property
    def resident_bytes(self) -> int:
        return self._bytes


# --------------------------------------------------------------------------- #
# The mounted tier — an extracted, read-only tree the sandbox mounts.          #
# --------------------------------------------------------------------------- #
# Where the sandbox symlinks a mounted bundle's entries from (Docker mount).
CONTAINER_BUNDLE_ROOT = "/opt/oolu/bundles"


class MaterializedBundleDir:
    """A bundle extracted ONCE to a read-only directory, keyed by id.

    The warm tier saves the pack; this saves the *unpack*. A large tree
    still costs one archive extraction per run when a backend unpacks its
    tar into the workspace. Materializing extracts the tree exactly once
    into ``<root>/<bundle_id>/`` — read-only, content-addressed, atomic —
    and every later run STAGES it by symlink (subprocess) or read-only
    bind-mount (Docker), touching no bytes. The OS page cache then keeps a
    hot bundle resident across ephemeral containers: the boot cost of a
    professional-library node stops being paid per run at all.

    Immutable by construction: files land ``0444`` and directories
    ``0555``, so a sandboxed script that writes THROUGH a staged symlink
    hits a read-only file and fails — a bundle is an input, never a
    scratchpad. Presence is readiness: a tree only appears at its final
    path via an atomic rename after full extraction, so a run never sees a
    half-materialized dir. Bounded and evicted least-recently-used, and a
    dir touched within a short grace window is spared (it may back a live
    run); the CAS is the durable truth, so an eviction only costs one
    re-extract.
    """

    def __init__(
        self,
        root,
        *,
        max_bytes: int = 2 * 1024 * 1024 * 1024,
        # Comfortably above the install + execute wall ceilings so a dir
        # backing a live run is never evicted out from under it (eviction
        # only fires on a new materialize under budget pressure anyway).
        grace_seconds: float = 900.0,
        # FLEET mode: the root is a network share several hosts mount (the
        # same trees serve every host, extracted once fleet-wide). A host
        # must then never evict on its own budget judgement — it cannot see
        # the fleet's usage, and deleting a dir another host has mounted
        # read-only would pull a running container's tree out from under
        # it. Removal belongs to the SWEEP alone (grace-checked, approve-
        # gated, audited), so shared mode turns per-host eviction off.
        shared: bool = False,
    ) -> None:
        from pathlib import Path

        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._max = max_bytes
        self._grace = grace_seconds
        self._shared = shared

    @property
    def shared(self) -> bool:
        return self._shared

    def path_for(self, bundle_id: str):
        return self._root / bundle_id

    def ensure(self, bundle_id: str, tar: bytes):
        """The tree's read-only directory, extracting it once if needed."""
        import tempfile

        final = self._root / bundle_id
        if final.is_dir():
            _touch(final)  # LRU-on-use
            return final
        staging = tempfile.mkdtemp(prefix=".materialize-", dir=self._root)
        try:
            _extract_readonly(tar, staging)
            try:
                import os

                os.rename(staging, final)  # atomic publish
            except OSError:
                # A concurrent materialize won the race: use the winner,
                # discard ours.
                _rmtree_readonly(staging)
                if not final.is_dir():
                    raise
        except BaseException:
            _rmtree_readonly(staging)
            raise
        _touch(final)
        self._evict_to_budget(keep=bundle_id)
        return final

    def top_level(self, bundle_id: str) -> list[str]:
        """The tree's top-level entries — what a backend symlinks in."""
        import os

        target = self._root / bundle_id
        return sorted(os.listdir(target)) if target.is_dir() else []

    def discard(self, bundle_id: str) -> bool:
        """Remove one bundle's materialized tree — the sweep's hand.

        Grace-checked even here: ``ensure`` touches a dir on every use
        (on every host of a shared root), so a tree touched within the
        grace window may back a run in flight somewhere in the fleet and
        is left alone; the sweep's next pass collects it once it has
        truly gone quiet. Missing is fine (another sweep, or a per-host
        eviction on an unshared root, got there first)."""
        import time

        target = self._root / bundle_id
        if not target.is_dir():
            return False
        if (time.time() - target.stat().st_mtime) < self._grace:
            return False  # possibly backing a live run; next pass gets it
        _rmtree_readonly(target)
        return True

    def _evict_to_budget(self, *, keep: str) -> None:
        import time

        if self._shared:
            return  # fleet roots are the sweep's to clean, never a host's
        dirs = [p for p in self._root.iterdir() if p.is_dir()]
        sized = [(p, _dir_size(p), p.stat().st_mtime) for p in dirs]
        total = sum(size for _, size, _ in sized)
        if total <= self._max:
            return
        now = time.time()
        for path, size, mtime in sorted(sized, key=lambda t: t[2]):
            if total <= self._max:
                break
            if path.name == keep or (now - mtime) < self._grace:
                continue  # never the just-made one, nor a possibly-live one
            _rmtree_readonly(path)
            total -= size

    @property
    def resident_bytes(self) -> int:
        return sum(_dir_size(p) for p in self._root.iterdir() if p.is_dir())


def _extract_readonly(tar: bytes, dest: str) -> None:
    """Extract a bundle tar under ``dest`` (escape-checked), then lock the
    tree read-only so a staged symlink into it can never be written back."""
    import os
    from pathlib import Path

    base = Path(dest).resolve()
    for path, data in unpack_tar(tar).items():
        target = (base / path).resolve()
        if base not in target.parents:
            raise BundleError(f"bundle path escapes the materialized dir: {path!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    # Files 0444, dirs 0555 — read + traverse, never write.
    for dirpath, dirnames, filenames in os.walk(base):
        for name in filenames:
            os.chmod(os.path.join(dirpath, name), 0o444)
        for name in dirnames:
            os.chmod(os.path.join(dirpath, name), 0o555)
    os.chmod(base, 0o555)


def _rmtree_readonly(path) -> None:
    import shutil
    import stat

    def _chmod_and_retry(func, p, _exc):
        import os

        os.chmod(p, stat.S_IWUSR | stat.S_IRUSR | stat.S_IXUSR)
        func(p)

    shutil.rmtree(path, onerror=_chmod_and_retry)


def _dir_size(path) -> int:
    import os

    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return total


def symlink_stage_cmd(entries: list[str], mount_path: str) -> list[str]:
    """The single container exec that symlinks a mounted bundle's top-level
    entries into ``/sandbox`` (the CWD) — so imports and relative reads
    resolve transparently into the read-only mount, with no byte copy.

    A pure function of its inputs so the Docker wiring it drives is unit-
    testable without a daemon."""
    lines = []
    for name in sorted(entries):
        # Each name is a bundle top-level entry — already escape-checked at
        # freeze and extraction; single-quote it against shell surprises.
        safe = "'" + name.replace("'", "'\\''") + "'"
        lines.append(f"ln -sfn {mount_path}/{safe} /sandbox/{safe}")
    return ["sh", "-c", "; ".join(lines) if lines else "true"]


@dataclass(frozen=True, slots=True)
class BundleResolver:
    """The seam the script runner holds: id -> PreparedBundle, cache-first.

    A tiny wrapper so the runner depends on one callable, not on the store
    and cache directly — and so an install without a bundle store simply
    has no resolver and falls back to inline files."""

    cache: PreparedBundleCache = field()

    def __call__(self, bundle_id: str) -> PreparedBundle | None:
        return self.cache.get(bundle_id)
