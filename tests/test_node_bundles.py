"""Node bundles: freeze a src tree once, boot it fast forever after.

The scaling problem: a node that clones a professional library carries
hundreds of files. Staging them the naive way — read every drawer row
inline on every run, serialize the bytes into the run state, copy them
into the sandbox one file at a time — makes boot cost grow with the
codebase and re-pays it on every idle re-run. Bundles flatten both:

- freeze the tree ONCE into a content-addressed object (dedup across
  nodes and versions is free);
- ship the 64-char id, never the bytes, so the run state stays tiny;
- pack once and cache the archive by id, so an unchanged node re-stages
  from a warm cache with no CAS read and no re-pack, in ONE extraction.
"""

from __future__ import annotations

import pytest

from oolu.durable import DurableConnection
from oolu.durable.artifacts import FilesystemArtifactStore
from oolu.runtime.backend import ExecutionRequest, make_success
from oolu.runtime.bundle import (
    MAX_BUNDLE_BYTES,
    MAX_BUNDLE_FILES,
    BundleError,
    BundleResolver,
    BundleStore,
    PreparedBundleCache,
    freeze_tree,
    pack_tar,
    unpack_tar,
)
from oolu.runtime.isolation import SubprocessBackend


# --------------------------------------------------------------------------- #
# Freeze: content-addressed identity, dedup, deterministic packing.            #
# --------------------------------------------------------------------------- #
def test_identity_is_content_addressed_and_order_independent():
    a, _ = freeze_tree({"main.py": "X=1\n", "pkg/util.py": "Y=2\n"})
    b, _ = freeze_tree({"pkg/util.py": "Y=2\n", "main.py": "X=1\n"})
    assert a.bundle_id == b.bundle_id  # order cannot change identity
    c, _ = freeze_tree({"main.py": "X=1\n", "pkg/util.py": "Y=3\n"})
    assert c.bundle_id != a.bundle_id  # a byte change does


def test_identical_files_deduplicate_to_one_blob():
    manifest, blobs = freeze_tree(
        {"a.py": "SAME\n", "b.py": "SAME\n", "c.py": "DIFFERENT\n"}
    )
    assert manifest.file_count == 3
    assert len(blobs) == 2  # two distinct contents, three files


def test_packing_is_deterministic_and_roundtrips():
    _, blobs = freeze_tree({"a.py": "1\n", "b/c.py": "2\n"})
    m, _ = freeze_tree({"a.py": "1\n", "b/c.py": "2\n"})
    tree = {e.path: blobs[e.sha256] for e in m.entries}
    assert pack_tar(tree) == pack_tar(tree)  # same bytes, every time
    assert unpack_tar(pack_tar(tree)) == tree


@pytest.mark.parametrize("bad", ["../escape.py", "/abs.py", "a/../../b.py", "c:\\x"])
def test_an_unsafe_path_is_refused_at_freeze(bad):
    with pytest.raises(BundleError):
        freeze_tree({bad: "x"})


def test_the_ceilings_hold():
    with pytest.raises(BundleError, match="at most"):
        freeze_tree({f"f{i}.py": "x" for i in range(MAX_BUNDLE_FILES + 1)})
    with pytest.raises(BundleError, match="at most"):
        freeze_tree({"big.py": "x" * (MAX_BUNDLE_BYTES + 1)})


# --------------------------------------------------------------------------- #
# The store: bytes in the CAS, manifest in the table, idempotent.              #
# --------------------------------------------------------------------------- #
def _store(tmp_path):
    conn = DurableConnection(tmp_path / "bundles.db")
    cas = FilesystemArtifactStore(tmp_path / "cas")
    return conn, BundleStore(conn, cas)


def test_freeze_then_materialize_roundtrips(tmp_path):
    conn, store = _store(tmp_path)
    try:
        tree = {"main.py": "X=1\n", "pkg/lib.py": "def f():\n    return 42\n"}
        manifest = store.freeze(tree)
        assert store.manifest(manifest.bundle_id).bundle_id == manifest.bundle_id
        materialized = store.materialize(manifest)
        assert {k: v.decode() for k, v in materialized.items()} == tree
    finally:
        conn.close()


def test_freeze_is_idempotent_and_dedups_across_nodes(tmp_path):
    conn, store = _store(tmp_path)
    try:
        # Two "nodes" cloned from the same library freeze to the same
        # bundle; the shared file is stored once.
        m1 = store.freeze({"lib.py": "SHARED\n", "a.py": "A\n"})
        m2 = store.freeze({"lib.py": "SHARED\n", "a.py": "A\n"})
        assert m1.bundle_id == m2.bundle_id
        m3 = store.freeze({"lib.py": "SHARED\n", "b.py": "B\n"})
        # lib.py's blob is shared between m1 and m3 (same digest).
        shared = next(e.sha256 for e in m1.entries if e.path == "lib.py")
        assert any(e.sha256 == shared for e in m3.entries)
        assert store._artifacts.exists(f"sha256:{shared}")
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The prepared cache: pack once, reuse, evict when idle.                       #
# --------------------------------------------------------------------------- #
def test_the_cache_packs_once_and_reuses(tmp_path):
    conn, store = _store(tmp_path)
    try:
        manifest = store.freeze({"a.py": "A\n", "pkg/b.py": "B\n"})
        cache = PreparedBundleCache(store)
        first = cache.get(manifest.bundle_id)
        second = cache.get(manifest.bundle_id)
        assert first is second  # the same packed object, reused
        assert cache.hits == 1 and cache.misses == 1
        # The packed tar really is the tree.
        assert set(unpack_tar(first.tar)) == {"a.py", "pkg/b.py"}
    finally:
        conn.close()


def test_the_cache_evicts_least_recently_used_when_over_budget(tmp_path):
    conn, store = _store(tmp_path)
    try:
        ids = [store.freeze({f"f{i}.py": "x" * 5000}).bundle_id for i in range(3)]
        # A budget sized to exactly one packed bundle forces the oldest out
        # as each new one loads (the LRU always keeps at least the newest).
        unit = len(store.prepare(ids[0]).tar)
        cache = PreparedBundleCache(store, max_bytes=unit)
        for bundle_id in ids:
            assert cache.get(bundle_id) is not None
        assert cache.resident_bytes <= unit
        # The most recent is warm (a hit); the first is cold (re-prepared).
        misses_before = cache.misses
        cache.get(ids[-1])
        assert cache.misses == misses_before  # still resident
        cache.get(ids[0])
        assert cache.misses == misses_before + 1  # evicted, re-prepared
    finally:
        conn.close()


def test_an_unknown_bundle_resolves_to_none(tmp_path):
    conn, store = _store(tmp_path)
    try:
        resolver = BundleResolver(PreparedBundleCache(store))
        assert resolver("deadbeef" * 8) is None
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# End to end: a bundled package is importable inside the sandbox.              #
# --------------------------------------------------------------------------- #
def test_a_bundled_package_is_staged_and_importable(tmp_path):
    conn, store = _store(tmp_path)
    try:
        manifest = store.freeze(
            {
                "pkg/__init__.py": "",
                "pkg/calc.py": "def add(a, b):\n    return a + b\n",
            }
        )
        prepared = BundleResolver(PreparedBundleCache(store))(manifest.bundle_id)
        script = (
            "import sys; sys.path.insert(0, '.')\n"
            "from pkg.calc import add\n"
            "from _oolu_runtime import emit_result\n"
            "emit_result({'sum': add(2, 3)})\n"
        )
        result = SubprocessBackend().run(
            ExecutionRequest(script=script, bundle=prepared)
        )
        assert result.contract_ok, result.stderr
        assert result.contract_payload == {"sum": 5}
    finally:
        conn.close()


def test_inline_files_and_a_bundle_coexist(tmp_path):
    # The webhook path stages an inline payload file alongside the bundle;
    # both must reach the sandbox.
    conn, store = _store(tmp_path)
    try:
        manifest = store.freeze({"pkg/__init__.py": "", "pkg/v.py": "V = 7\n"})
        prepared = BundleResolver(PreparedBundleCache(store))(manifest.bundle_id)
        script = (
            "import sys, json; sys.path.insert(0, '.')\n"
            "from pkg.v import V\n"
            "payload = json.load(open('webhook_payload.json'))\n"
            "from _oolu_runtime import emit_result\n"
            "emit_result({'v': V, 'event': payload['event']})\n"
        )
        result = SubprocessBackend().run(
            ExecutionRequest(
                script=script,
                bundle=prepared,
                files={"webhook_payload.json": '{"event": "ping"}'},
            )
        )
        assert result.contract_ok, result.stderr
        assert result.contract_payload == {"v": 7, "event": "ping"}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# One-shot staging: even without a bundle, files stage in one archive.         #
# --------------------------------------------------------------------------- #
def test_many_inline_files_stage_in_one_pass(tmp_path):
    files = {f"pkg/m{i}.py": f"N{i} = {i}\n" for i in range(20)}
    files["pkg/__init__.py"] = ""
    script = (
        "import sys; sys.path.insert(0, '.')\n"
        "from pkg.m19 import N19\n"
        "from _oolu_runtime import emit_result\n"
        "emit_result({'n': N19})\n"
    )
    result = SubprocessBackend().run(ExecutionRequest(script=script, files=files))
    assert result.contract_ok, result.stderr
    assert result.contract_payload == {"n": 19}


def test_make_success_helper_still_shapes_a_result():
    # Guards the StubBackend contract the bundle tests lean on elsewhere.
    assert make_success({"ok": 1}).contract_payload == {"ok": 1}


# --------------------------------------------------------------------------- #
# The gateway ships the reference, not the bytes.                              #
# --------------------------------------------------------------------------- #
def test_the_gateway_freezes_a_tree_and_ships_its_id(tmp_path):
    from types import SimpleNamespace

    from test_node_hands import _grown_web_node

    from oolu.durable import UserFile

    app, conn, ident, desk, script_exec, agreed = _grown_web_node(tmp_path)
    try:
        node_id = desk.overview(principal="user-1", tenant="t1")[0].node_id
        # Give the node a real codebase beyond main.py.
        for i in range(5):
            app._files.save(
                UserFile(
                    tenant_id="t1",
                    node_id=node_id,
                    folder="src/pkg",
                    name=f"m{i}.py",
                    content=f"N{i} = {i}\n",
                )
            )
        # Wire a bundle store (the host runtime does this in assembly).
        cas = FilesystemArtifactStore(tmp_path / "cas")
        app._bundle_store = BundleStore(conn, cas)

        session = SimpleNamespace(tenant_id="t1", principal_id="user-1")
        function = app._function_for_node(session, node_id)
        # The heavy tree ships as ONE id, and the inline bytes are gone.
        assert "bundle" in function
        assert "files" not in function
        assert len(function["bundle"]) == 64  # a sha256 hex id
        # main.py still promoted to the script, never into the bundle.
        assert "http_request" in function["script"]
        # The bundle really holds the extra tree (dedup: 5 files here).
        manifest = app._bundle_store.manifest(function["bundle"])
        assert manifest.file_count == 5
        assert {e.path for e in manifest.entries} == {
            f"pkg/m{i}.py" for i in range(5)
        }
    finally:
        conn.close()


def test_a_single_file_node_stays_inline_no_bundle(tmp_path):
    from types import SimpleNamespace

    from test_node_hands import _grown_web_node

    app, conn, ident, desk, script_exec, agreed = _grown_web_node(tmp_path)
    try:
        node_id = desk.overview(principal="user-1", tenant="t1")[0].node_id
        app._bundle_store = BundleStore(
            conn, FilesystemArtifactStore(tmp_path / "cas")
        )
        session = SimpleNamespace(tenant_id="t1", principal_id="user-1")
        function = app._function_for_node(session, node_id)
        # Only main.py: promoted to the script, nothing left to bundle.
        assert "bundle" not in function
        assert not function.get("files")
    finally:
        conn.close()
