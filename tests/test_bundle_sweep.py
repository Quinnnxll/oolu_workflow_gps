"""The sweep: reclaiming the shared content-addressed store, safely.

The bundle tiers made boot fast; this makes idle lean. Every edited node
re-freezes to a new bundle and leaves the old one behind, so without a
sweep the CAS grows forever. The sweep reclaims dead frozen trees — but
the store is SHARED (bundle blobs, drawer blobs, CAD exports are all one
content-addressed store), so the invariant under test is the safety one:
a blob is deleted only if NO source references it, the sweep's authority
is limited to bytes a dead bundle introduced, and everything else is left
untouched.
"""

from __future__ import annotations

from test_http_gateway import _req

from oolu.durable import DurableConnection, UserFile, UserFileStore
from oolu.durable.artifacts import FilesystemArtifactStore
from oolu.runtime.bundle import BundleStore
from oolu.runtime.sweep import CallableSource, CasSweep


def _wire(tmp_path):
    conn = DurableConnection(tmp_path / "sweep.db")
    cas = FilesystemArtifactStore(tmp_path / "cas")
    return conn, cas, BundleStore(conn, cas), UserFileStore(conn, artifacts=cas)


def _drawer_source(conn):
    def refs():
        found = set()
        for row in conn.db.execute("SELECT payload_json FROM user_files").fetchall():
            blob_ref = UserFile.model_validate_json(row["payload_json"]).blob_ref
            if blob_ref:
                found.add(blob_ref)
        return found

    return CallableSource("drawer", refs)


# --------------------------------------------------------------------------- #
# The core: dead trees are reclaimed, live ones spared.                        #
# --------------------------------------------------------------------------- #
def test_a_dead_bundle_is_reclaimed_and_a_live_one_is_kept(tmp_path):
    conn, cas, bundles, _files = _wire(tmp_path)
    try:
        live = bundles.freeze({"main.py": "print('live')\n", "keep.py": "K=1\n"})
        dead = bundles.freeze({"main.py": "print('dead')\n", "gone.py": "G=1\n"})
        sweep = CasSweep(
            bundles, cas, live_bundle_ids=lambda: {live.bundle_id}, grace_seconds=0
        )

        plan = sweep.inspect()
        assert dead.bundle_id in plan.dead_manifests
        assert live.bundle_id not in plan.dead_manifests
        gone = f"sha256:{[e for e in dead.entries if e.path == 'gone.py'][0].sha256}"
        keep = f"sha256:{[e for e in live.entries if e.path == 'keep.py'][0].sha256}"
        assert gone in plan.orphan_blobs
        assert keep not in plan.orphan_blobs
        # inspect() is a pure dry run — nothing was deleted.
        assert cas.exists(gone) and len(bundles.manifests()) == 2

        applied = sweep.collect()
        assert applied.applied is True
        assert applied.reclaimed_bytes > 0
        assert not cas.exists(gone)  # dead-unique blob reclaimed
        assert cas.exists(keep)  # live blob spared
        assert [m.bundle_id for m, _ in bundles.manifests()] == [live.bundle_id]
    finally:
        conn.close()


def test_a_blob_shared_with_a_live_source_is_never_deleted(tmp_path):
    conn, cas, bundles, files = _wire(tmp_path)
    try:
        # A dead bundle and a drawer file with the SAME bytes -> one object.
        dead = bundles.freeze({"main.py": "x\n", "shared.bin": "SHARED-BYTES\n"})
        saved = files.save_bytes(
            UserFile(tenant_id="t1", node_id="n1", name="d.bin"), b"SHARED-BYTES\n"
        )
        sweep = CasSweep(
            bundles,
            cas,
            sources=[_drawer_source(conn)],
            live_bundle_ids=lambda: set(),  # the bundle is dead
            grace_seconds=0,
        )
        sweep.collect()
        # The manifest is gone, but the SHARED blob survived — the drawer
        # still references it, and losing it would corrupt the file.
        assert dead.bundle_id not in [m.bundle_id for m, _ in bundles.manifests()]
        assert cas.exists(saved.blob_ref)
        assert files.read_bytes(saved) == b"SHARED-BYTES\n"
    finally:
        conn.close()


def test_the_sweep_never_touches_blobs_no_bundle_introduced(tmp_path):
    conn, cas, bundles, _files = _wire(tmp_path)
    try:
        # A CAD-like export: in the shared store, referenced by NO bundle.
        cad = cas.put("cad", b"BINARY-PART", media_type="application/step")
        dead = bundles.freeze({"main.py": "d\n", "gone.py": "G\n"})
        sweep = CasSweep(
            bundles, cas, live_bundle_ids=lambda: set(), grace_seconds=0
        )
        plan = sweep.collect()
        # Out-of-scope blob untouched; the dead bundle's own blob reclaimed.
        assert cas.exists(cad)
        assert cad not in plan.orphan_blobs
        gone = f"sha256:{[e for e in dead.entries if e.path == 'gone.py'][0].sha256}"
        assert not cas.exists(gone)
    finally:
        conn.close()


def test_the_grace_window_spares_a_fresh_blob(tmp_path):
    conn, cas, bundles, _files = _wire(tmp_path)
    try:
        dead = bundles.freeze({"main.py": "d\n", "fresh.py": "F\n"})
        # A large grace: even a dead bundle's blob is kept while young — a
        # freeze or a run may still be in flight.
        sweep = CasSweep(
            bundles, cas, live_bundle_ids=lambda: set(), grace_seconds=3600
        )
        plan = sweep.collect()
        assert plan.orphan_blobs == ()
        assert plan.kept_blobs >= 1
        fresh = f"sha256:{[e for e in dead.entries if e.path == 'fresh.py'][0].sha256}"
        assert cas.exists(fresh)  # spared by the grace
    finally:
        conn.close()


def test_a_store_without_enumeration_is_reported_unsweepable(tmp_path):
    conn, cas, bundles, _files = _wire(tmp_path)
    try:
        bundles.freeze({"main.py": "d\n", "x.py": "X\n"})

        class _NoRefs:
            def delete(self, ref):  # pragma: no cover - never reached
                raise AssertionError("must not delete without enumeration")

        sweep = CasSweep(bundles, _NoRefs(), live_bundle_ids=lambda: set())
        plan = sweep.collect()
        # No refs() -> no authority -> nothing removed, nothing claimed.
        assert plan.orphan_blobs == ()
        assert plan.reclaimed_bytes == 0
        assert plan.dead_manifests == ()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Through the gateway: live ids recomputed from real drawers, approve-gated.   #
# --------------------------------------------------------------------------- #
def test_the_gateway_recomputes_live_ids_from_current_drawers(tmp_path):
    # A node with a multi-file tree is live; a stale bundle from an earlier
    # tree is dead. The gateway recomputes live ids from the CURRENT drawer,
    # so the stale one is reclaimed and the live one is kept.
    from test_node_hands import _grown_web_node

    app, conn, ident, desk, script_exec, agreed = _grown_web_node(tmp_path)
    try:
        node_id = desk.overview(principal="user-1", tenant="t1")[0].node_id
        # Wire the bundle store onto the gateway (assembly does this live).
        cas = FilesystemArtifactStore(tmp_path / "cas")
        app._bundle_store = BundleStore(conn, cas)

        # A helper file beyond main.py -> the node's CURRENT (live) tree.
        app._files.save(
            UserFile(
                tenant_id="t1",
                node_id=node_id,
                folder="src",
                name="helper.py",
                content="H = 1\n",
            )
        )
        live_ids = app._bundle_live_ids()
        assert len(live_ids) == 1
        live_id = next(iter(live_ids))
        # The live tree omits main.py (the entry is the script, never bundled).
        manifest = app._bundle_store.manifest(live_id)
        assert {e.path for e in manifest.entries} == {"helper.py"}

        # A stale bundle from an OLD tree the node no longer has.
        stale = app._bundle_store.freeze({"old.py": "OLD\n"})
        assert stale.bundle_id != live_id

        # The gateway's own sweep sees the stale one dead, the live one spared.
        plan = app._bundle_sweep().inspect()
        assert stale.bundle_id in plan.dead_manifests
        assert live_id not in plan.dead_manifests
    finally:
        conn.close()


def test_the_apply_route_is_platform_gated(tmp_path):
    # POST reclaims real bytes: it requires approve authority, exactly like
    # the hygiene sweep. An ordinary member is refused.
    from test_node_hands import _grown_web_node

    app, conn, ident, desk, script_exec, agreed = _grown_web_node(tmp_path)
    try:
        app._bundle_store = BundleStore(conn, FilesystemArtifactStore(tmp_path / "cas"))
        resp = app.handle(
            _req(
                "POST",
                "/v1/work/bundles/sweep",
                token=ident.token("user-1", "t1"),
            )
        )
        assert resp.status == 403
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The fleet: one shared materialized root, many hosts, one remover — the sweep.#
# --------------------------------------------------------------------------- #
def test_two_hosts_share_one_materialized_root(tmp_path, monkeypatch):
    # Two hosts (two MaterializedBundleDir instances) mount one network
    # root: the first extracts; the second finds the tree already there and
    # extracts NOTHING.
    import oolu.runtime.bundle as bundle_mod
    from oolu.runtime.bundle import MaterializedBundleDir, freeze_tree, pack_tar

    manifest, blobs = freeze_tree({"pkg/__init__.py": "", "pkg/v.py": "V=9\n"})
    tar = pack_tar({e.path: blobs[e.sha256] for e in manifest.entries})

    extractions = []
    real = bundle_mod._extract_readonly

    def counting(tar_bytes, dest):
        extractions.append(dest)
        return real(tar_bytes, dest)

    monkeypatch.setattr(bundle_mod, "_extract_readonly", counting)
    shared_root = tmp_path / "fleet-mount"
    host_a = MaterializedBundleDir(shared_root, shared=True)
    host_b = MaterializedBundleDir(shared_root, shared=True)

    path_a = host_a.ensure(manifest.bundle_id, tar)
    path_b = host_b.ensure(manifest.bundle_id, tar)
    assert path_a == path_b  # one tree serves the whole fleet
    assert len(extractions) == 1  # host B extracted nothing
    assert host_b.top_level(manifest.bundle_id) == ["pkg"]


def test_shared_mode_never_evicts_on_a_hosts_own_judgement(tmp_path):
    from oolu.runtime.bundle import MaterializedBundleDir, freeze_tree, pack_tar

    # A tiny budget and zero grace would evict aggressively in host-private
    # mode; in SHARED mode a host must never remove fleet trees itself.
    md = MaterializedBundleDir(
        tmp_path / "fleet", max_bytes=1, grace_seconds=0, shared=True
    )
    ids = []
    for i in range(3):
        m, blobs = freeze_tree({f"f{i}.py": "x" * 4000})
        tar = pack_tar({e.path: blobs[e.sha256] for e in m.entries})
        md.ensure(m.bundle_id, tar)
        ids.append(m.bundle_id)
    assert all(md.path_for(b).is_dir() for b in ids)  # nothing evicted


def test_discard_respects_the_grace_window(tmp_path):
    from oolu.runtime.bundle import MaterializedBundleDir, freeze_tree, pack_tar

    m, blobs = freeze_tree({"a.py": "A\n"})
    tar = pack_tar({e.path: blobs[e.sha256] for e in m.entries})
    # Within the grace: the tree may back a run somewhere — discard refuses.
    guarded = MaterializedBundleDir(tmp_path / "g", grace_seconds=3600, shared=True)
    guarded.ensure(m.bundle_id, tar)
    assert guarded.discard(m.bundle_id) is False
    assert guarded.path_for(m.bundle_id).is_dir()
    # Past the grace (zero window): discard removes it.
    open_dir = MaterializedBundleDir(tmp_path / "o", grace_seconds=0, shared=True)
    open_dir.ensure(m.bundle_id, tar)
    assert open_dir.discard(m.bundle_id) is True
    assert not open_dir.path_for(m.bundle_id).is_dir()


def test_the_sweep_purges_dead_accelerators_and_spares_live_ones(tmp_path):
    from oolu.runtime.bundle import (
        MaterializedBundleDir,
        WarmBundleTier,
        pack_tar,
    )

    conn, cas, bundles, _files = _wire(tmp_path)
    try:
        live = bundles.freeze({"main.py": "L\n"})
        dead = bundles.freeze({"main.py": "D\n"})
        warm = WarmBundleTier(tmp_path / "warm")
        mounted = MaterializedBundleDir(
            tmp_path / "fleet", grace_seconds=0, shared=True
        )
        for manifest in (live, dead):
            tree = bundles.materialize(manifest)
            warm.put(manifest.bundle_id, pack_tar(tree))
            mounted.ensure(manifest.bundle_id, pack_tar(tree))

        sweep = CasSweep(
            bundles,
            cas,
            live_bundle_ids=lambda: {live.bundle_id},
            tiers=[warm, mounted],
            grace_seconds=0,
        )
        # Dry run touches no tier.
        assert sweep.inspect().tier_discards == 0
        assert warm.get(dead.bundle_id) is not None

        plan = sweep.collect()
        assert plan.tier_discards == 2  # dead's warm tar + materialized tree
        assert warm.get(dead.bundle_id) is None
        assert not mounted.path_for(dead.bundle_id).is_dir()
        # The live bundle's accelerators are untouched.
        assert warm.get(live.bundle_id) is not None
        assert mounted.path_for(live.bundle_id).is_dir()
    finally:
        conn.close()


def test_the_gateway_hands_its_tiers_to_the_sweep(tmp_path):
    from test_node_hands import _grown_web_node

    from oolu.runtime.bundle import WarmBundleTier

    app, conn, ident, desk, script_exec, agreed = _grown_web_node(tmp_path)
    try:
        app._bundle_store = BundleStore(conn, FilesystemArtifactStore(tmp_path / "cas"))
        warm = WarmBundleTier(tmp_path / "warm")
        app._bundle_tiers = [warm]
        sweep = app._bundle_sweep()
        assert sweep._tiers == [warm]
    finally:
        conn.close()
