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
2. **Pack once, reuse across runs.** Materializing a bundle produces a
   single deterministic tar of the whole tree; that tar is cached by
   `bundle_id` in a bounded, idle-evictable LRU (`PreparedBundleCache`).
   The first run of a node packs its tree once; every later run of the
   same unchanged node reuses the packed archive with no CAS read and no
   re-pack.
3. **Stage in one operation.** The sandbox extracts the whole tree in a
   single archive extraction — one container `exec`, not one per file.
   Staging cost no longer grows with the file count. This one-shot
   staging applies even to small inline trees: N per-file round-trips
   became one tar.

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
  (cache-first) to a `PreparedBundle` — the packed tar — once per run,
  and handed to every backend attempt (cached replay, provided, repaired,
  resynthesized) alongside the script.
- **Stage** happens in the backend (`LocalDockerBackend` /
  `SubprocessBackend`): the harness pair and any inline files pack into
  one tar and extract in one step; the bundle tar extracts in one more.
  Every member is re-checked against the sandbox root — belt-and-braces
  over the freeze-time path check.

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
- The durable truth is always the CAS. The prepared cache is a pure
  accelerator: an eviction costs the next run one re-pack, never
  correctness, so a busy host with thousands of nodes bounds its memory
  freely.

## Ceilings

A node's tree is programs and data, not a data lake:
`MAX_BUNDLE_FILES = 4096` and `MAX_BUNDLE_BYTES = 64 MiB` across the whole
tree, refused at freeze time. Large binary payloads belong in path-roled
slots and the blob door, not the code tree.
