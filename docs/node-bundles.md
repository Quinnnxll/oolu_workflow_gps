# Node bundles — boot speed and idle efficiency at codebase scale

## The problem

A node started life as one `src/main.py`. It doesn't stay there: a node
can clone a professional library, carry a whole package tree, accumulate
hundreds of modules and data files. The original staging path did not
scale to that:

- **Boot cost grew with the codebase.** Every run re-read *every* `src/`
  row from the drawer inline (`_node_function_extras`), carried those
  bytes through the durable run state, and copied them into the sandbox
  **one file at a time** — one container `exec` round-trip per file
  (`_put_files`). A 300-file node paid 300 round-trips on every run.
- **Idle cost was paid again and again.** Nothing was reused between
  runs: the same tree was re-read, re-serialized, and re-staged from
  scratch each time, and each file's bytes lived inline in its own DB
  row with no dedup — two nodes cloned from the same software stored two
  full copies.

## The unit: a content-addressed bundle

A node's `src/` tree (minus `main.py`, which is the entry script) freezes
**once** into an immutable **bundle** (`oolu/runtime/bundle.py`):

- Each file's bytes are hashed and stored in the content-addressed object
  store — the same CAS that backs the file drawer's blobs. Identical
  files across any nodes or versions store once.
- The **manifest** — the sorted list of `(path, sha256, size)` — is
  itself hashed to a `bundle_id`. Two identical trees are the same
  bundle; a one-byte change is a new bundle. Freezing is idempotent.

Three properties fall straight out:

1. **Ship the reference, not the bytes.** A run carries the 64-char
   `bundle_id` and its small manifest, never the tree's contents, so the
   durable `RunState` stays tiny no matter how large the node is.
2. **Pack once, reuse across runs — even across restarts.** Materializing
   a bundle produces a single deterministic tar of the whole tree, cached
   by `bundle_id` in a two-tier `PreparedBundleCache`: a bounded in-memory
   LRU, and — behind it — a bounded on-disk **warm tier**
   (`WarmBundleTier`) of packed tars that outlive the process. The first
   run of a node packs its tree once; every later run of the same
   unchanged node reuses the packed archive with no CAS read and no
   re-pack, and a node that ran before a deploy or restart stages warm on
   its very first run *after* it, instead of re-reading its whole tree
   from the CAS. Each warm tar is self-verifying — its `bundle_id` fixes
   the tree it must contain, so a truncated or tampered file is caught on
   read and simply re-prepared.
3. **Stage in one operation.** The sandbox extracts the whole tree in a
   single archive extraction — one container `exec`, not one per file.
   Staging cost no longer grows with the file count. This one-shot
   staging applies even to small inline trees: N per-file round-trips
   became one tar.
4. **Or don't extract at all — mount it.** With the optional **mounted
   tier** (`MaterializedBundleDir`, opt-in via `OOLU_BUNDLE_MOUNT`), a
   bundle is extracted ONCE to a read-only host directory keyed by
   `bundle_id` and then staged by *reference*: a read-only bind-mount in
   Docker, a symlink in the dev backend. A run copies no bytes at all,
   and the OS page cache keeps a hot bundle resident across ephemeral
   containers — the boot cost of a professional-library node stops being
   paid per run entirely.

## The path end to end

```text
build / repair  ──►  drawer  ──►  freeze  ──►  ship id  ──►  resolve  ──►  stage
 (writes src/*.py)   (source     (BundleStore   (run state    (Prepared    (one tar,
                      of truth)   → CAS +         carries        BundleCache  one exec)
                                  manifest)       bundle_id)     by id)
```

- **Freeze** happens at the gateway boundary (`_finalize_function`): when
  a resolved function has a tree of files beyond `main.py` and a
  `BundleStore` is wired, the tree is frozen and the function carries
  `{"bundle": bundle_id}` instead of `{"files": {...}}`. A single-file
  node (just `main.py`) ships nothing extra; an install with no store
  keeps the tree inline — same bytes, same walls, just not deduplicated
  or cached.
- **Resolve** happens in the script runner (`NodeScriptRunner`): an
  action carrying a `bundle` id is resolved through a `BundleResolver`
  (cache-first: memory → disk → CAS) to a `PreparedBundle` — the packed
  tar — once per run, and handed to every backend attempt (cached replay,
  provided, repaired, resynthesized) alongside the script. The warm tier's
  disk budget is `OOLU_BUNDLE_CACHE_MB` (default 1024 MiB; `0` disables the
  disk tier and keeps memory only).
- **Stage** happens in the backend (`LocalDockerBackend` /
  `SubprocessBackend`): the harness pair and any inline files pack into
  one tar and extract in one step; the bundle tar extracts in one more.
  Every member is re-checked against the sandbox root — belt-and-braces
  over the freeze-time path check. With the mounted tier on, the bundle
  is instead materialized once and staged by reference (mount + symlink),
  extracting nothing per run.

## The mounted tier — mount the tree, don't re-extract it

`OOLU_BUNDLE_MOUNT` (truthy; default off) turns on `MaterializedBundleDir`:
each bundle is extracted exactly once into `<data>/bundle-mounted/<bundle_id>/`
— read-only (`0444` files, `0555` dirs), content-addressed, published by an
atomic rename so a run never sees a half-materialized tree. From then on:

- **Docker** bind-mounts that directory **read-only** at
  `/opt/oolu/bundles/<bundle_id>` and, in one `exec`, symlinks its
  top-level entries into `/sandbox` (the CWD) — so `import pkg` and
  `open('data.csv')` resolve transparently into the mount with no byte
  copy. The read-only bind mount is kernel-enforced, so even a root
  process in the container cannot write back through a symlink; the tree
  is immutable, and severance is untouched (a directory is not a
  network).
- **The subprocess dev backend** symlinks the same way from the
  materialized dir. This is a convenience for the dev/fallback path;
  its `0444` files bind a non-root writer, but — like everything about
  the subprocess backend — it is **not** an isolation boundary. The
  kernel-enforced guarantee is the Docker read-only mount.

The materialized dir is bounded (`OOLU_BUNDLE_MOUNT_MB`, default 2048 MiB)
and evicted least-recently-used, with a grace window (default 900 s, well
above the install + execute ceilings) that never evicts a directory that
may back a live run. The CAS remains the durable truth: an eviction costs
one re-extract, never correctness.

## What does not change

The bundle is a faster *shape* for the same trusted-by-the-same-rules
code, never a new trust path:

- The bytes are the same `src/` files that already passed the drawer's
  walls; freezing re-checks every path and refuses an unsafe one before
  it can ever become a bundle.
- The tree is still extracted into the **network-severed** sandbox's
  writable scratch; the script that imports it is still screened and
  verified by execution.
- The `bundle_id` joins the script cache key, so an edited tree (a new
  id) re-verifies rather than replaying against a tree it never ran with.
- The durable truth is always the CAS. Both tiers of the prepared cache
  are pure accelerators: an eviction (or a corrupt warm tar) costs the
  next run one re-pack, never correctness, so a busy host with thousands
  of nodes bounds both its memory and its disk cache freely.

## The sweep — reclaiming dead trees, safely

The tiers made boot fast; the sweep makes idle lean. Every edited node
re-freezes to a NEW bundle and leaves the old manifest behind, so without
a sweep the store grows forever. `oolu.runtime.sweep.CasSweep` reclaims
the dead trees — and its whole design is dominated by one hazard: the CAS
is **shared**. Bundle blobs, the file drawer's blobs, and the CAD hand's
exports are all one content-addressed store, so identical bytes are ONE
object. A node whose file happens to equal a drawer upload shares that
blob; deleting it because the bundle died would corrupt the drawer.

Two rules keep the sweep safe:

1. **Authority is limited to what a dead bundle introduced.** The only
   deletion candidates are blobs a now-dead bundle's manifest referenced.
   A blob no dead bundle introduced — a CAD export, a durable artifact, a
   drawer-only upload — is never a candidate, so the sweep cannot touch
   what it did not create.
2. **A candidate is deleted only if NO source references it.** Every
   subsystem that puts bytes in the store declares its live refs through a
   `ReferenceSource`; the sweep marks their union (the live bundles' blobs
   plus the drawer's `all_blob_refs()`) and deletes only candidates
   outside it. A blob whose age it cannot read, or that is younger than
   the grace window (a freeze or run may be in flight), is kept.

Live-ness is recomputed, not remembered: `_bundle_live_ids` re-freezes
each node's CURRENT `src/` tree (freezing is idempotent and self-heals a
missing blob), so a bundle absent from that set is referenced by nothing.
The CAS remains the durable truth — a deletion only ever costs a
re-freeze, which is why the rule errs toward keeping.

The sweep is **dry-run first**. `GET /v1/work/bundles/sweep` returns the
exact plan (`dead_manifests`, `orphan_blobs`, `reclaimed_bytes`,
`kept_blobs`) touching nothing; `POST` applies it under approve authority
— the same platform gate the hygiene sweep uses — and records
`bundles.swept` on the audit log. There is deliberately no CLI: a
destructive store operation stays behind the approval flow. A store
adapter that cannot cheaply enumerate its objects (a future S3 backend
without a listing) is reported unsweepable rather than swept on a guess.

## Ceilings

A node's tree is programs and data, not a data lake:
`MAX_BUNDLE_FILES = 4096` and `MAX_BUNDLE_BYTES = 64 MiB` across the whole
tree, refused at freeze time. Large binary payloads belong in path-roled
slots and the blob door, not the code tree.
