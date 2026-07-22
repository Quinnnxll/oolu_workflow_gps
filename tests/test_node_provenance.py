"""Node provenance: immutable commits, sealed releases, honest revocation.

Exit gate: every write to a node's function files an immutable,
content-hashed commit chained to its parent — nothing is overwritten,
every attempt survives; a verified run seals the EXACT tree it executed
as a content-addressed release; a vulnerable release is revoked in
words (the artifact rows never change), new runs of that exact tree
refuse with the reason, and a REVISED tree is a new draft that runs to
earn a new seal. The history and release doors are desk-walled.
"""

from __future__ import annotations

import pytest
from test_http_gateway import _app, _req

from oolu.durable.connection import DurableConnection
from oolu.durable.files import UserFile, UserFileStore
from oolu.gateway import GatewayApp, GatewayError
from oolu.nodeplace import NodeplaceService, RegistryStore
from oolu.nodeplace.desk import NodeAccountStore, WorkDesk
from oolu.nodeplace.models import Node, Visibility
from oolu.nodeplace.provenance import NodeProvenance, tree_hash


def _ledger(tmp_path):
    conn = DurableConnection(tmp_path / "p.db")
    return conn, NodeProvenance(conn)


# --------------------------------------------------------------------- #
# The commit chain: append-only, deduplicated, every attempt kept.      #
# --------------------------------------------------------------------- #
def test_commits_chain_and_never_overwrite(tmp_path):
    conn, ledger = _ledger(tmp_path)
    first = ledger.commit(
        "t1", "n1", {"main.py": "a"}, kind="build",
        instruction="build it", by="alice",
    )
    assert first.parent_id == ""
    second = ledger.commit(
        "t1", "n1", {"main.py": "b"}, kind="revise",
        instruction="handle csv too", by="alice",
    )
    assert second.parent_id == first.commit_id
    # The same tree twice in a row is the SAME commit — no empty history.
    again = ledger.commit("t1", "n1", {"main.py": "b"}, kind="edit")
    assert again.commit_id == second.commit_id
    history = ledger.history("t1", "n1")
    assert [c.commit_id for c in history] == [
        second.commit_id, first.commit_id,
    ]
    # The replaced code SURVIVES in its commit — the chain is the lab log.
    assert history[1].files == {"main.py": "a"}
    assert history[0].instruction == "handle csv too"
    # Walled per tenant: another tenant sees no chain.
    assert ledger.history("t2", "n1") == []
    conn.close()


def test_tree_hash_is_order_independent_and_content_sensitive():
    same = tree_hash({"a.py": "x", "b.py": "y"})
    assert tree_hash({"b.py": "y", "a.py": "x"}) == same
    assert tree_hash({"a.py": "x", "b.py": "z"}) != same
    assert tree_hash({"a.py": "x"}) != same


# --------------------------------------------------------------------- #
# Releases: sealed once, revoked in words, never modified.              #
# --------------------------------------------------------------------- #
def test_releases_seal_idempotently_and_revocation_stands(tmp_path):
    conn, ledger = _ledger(tmp_path)
    tree_one = {"main.py": "code v1"}
    sealed = ledger.seal(
        "t1", "n1", tree=tree_one, semver="1.0.0", verified_by_run="r1"
    )
    # Re-verifying the same tree is the same release.
    again = ledger.seal("t1", "n1", tree=tree_one, verified_by_run="r2")
    assert again.release_id == sealed.release_id
    (item,) = ledger.releases("t1", "n1")
    assert item["status"] == "active" and item["semver"] == "1.0.0"
    # Revocation flips the CONTROL row only — first reason stands.
    assert ledger.revoke(
        "t1", sealed.release_id, reason="CVE in dependency", by="alice"
    )
    assert not ledger.revoke("t1", sealed.release_id, reason="other words")
    control = ledger.control("t1", sealed.release_id)
    assert control["status"] == "revoked"
    assert control["reason"] == "CVE in dependency"
    # The guard names the exact revoked tree...
    revoked = ledger.revoked_tree("t1", "n1")
    assert revoked == (tree_hash(tree_one), "CVE in dependency")
    # ...and a revoked artifact cannot be laundered by re-sealing it.
    ledger.seal("t1", "n1", tree=tree_one, verified_by_run="r3")
    assert ledger.control("t1", sealed.release_id)["status"] == "revoked"
    # A REVISED tree earns a NEW seal, and the block lifts with it.
    fresh = ledger.seal(
        "t1", "n1", tree={"main.py": "code v2"}, verified_by_run="r4"
    )
    assert fresh.release_id != sealed.release_id
    assert ledger.revoked_tree("t1", "n1") is None
    assert [r["status"] for r in ledger.releases("t1", "n1")] == [
        "active", "revoked",
    ]
    conn.close()


# --------------------------------------------------------------------- #
# The gateway stamp and the production guard.                           #
# --------------------------------------------------------------------- #
def test_the_stamp_and_the_production_guard(tmp_path):
    app, conn, ident = _app(tmp_path)
    ledger = NodeProvenance(conn)
    gateway = GatewayApp(
        app._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        provenance=ledger,
    )
    try:
        sealed = ledger.seal(
            "t1", "n1", tree={"main.py": "code"}, verified_by_run="r1"
        )
        # The exact sealed tree is stamped sealed.
        function = gateway._stamp_release_state(
            "t1", {"node_id": "n1", "script": "code"}
        )
        assert function["_release"] == {
            "release_id": sealed.release_id, "sealed": True,
        }
        gateway._refuse_revoked(function)  # active: passes
        # A drawer edited since the seal is a DRAFT — it runs.
        draft = gateway._stamp_release_state(
            "t1", {"node_id": "n1", "script": "code v2"}
        )
        assert draft["_release"]["sealed"] is False
        assert "_revoked" not in draft
        # Revoked: the exact tree refuses with the reason named...
        ledger.revoke("t1", sealed.release_id, reason="leaks the key")
        stamped = gateway._stamp_release_state(
            "t1", {"node_id": "n1", "script": "code"}
        )
        assert stamped["_revoked"] == "leaks the key"
        with pytest.raises(GatewayError) as refusal:
            gateway._refuse_revoked(stamped)
        assert refusal.value.code == "release_revoked"
        assert "leaks the key" in refusal.value.message
        # ...while the revised draft still passes the guard.
        gateway._refuse_revoked(
            gateway._stamp_release_state(
                "t1", {"node_id": "n1", "script": "code v2"}
            )
        )
    finally:
        conn.close()


def test_the_drawer_tree_commits_through_the_files_store(tmp_path):
    app, conn, ident = _app(tmp_path)
    files = UserFileStore(conn)
    ledger = NodeProvenance(conn)
    gateway = GatewayApp(
        app._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        files=files,
        provenance=ledger,
    )
    try:
        files.save(
            UserFile(
                tenant_id="t1", node_id="n1", name="main.py",
                folder="src", content="print('v1')",
            )
        )
        files.save(
            UserFile(
                tenant_id="t1", node_id="n1", name="util.py",
                folder="src/lib", content="pass",
            )
        )
        gateway._file_node_commit(
            "t1", "n1", kind="edit", instruction="hand edit", by="alice"
        )
        head = ledger.head("t1", "n1")
        assert head is not None
        # The commit's tree speaks the RUN's path form: main.py at the
        # root, subfolders under it — the exact tree that executes.
        assert head.files == {
            "main.py": "print('v1')", "lib/util.py": "pass",
        }
    finally:
        conn.close()


# --------------------------------------------------------------------- #
# The doors: history and releases, desk-walled; revocation in words.    #
# --------------------------------------------------------------------- #
def _host(tmp_path):
    app, conn, ident = _app(tmp_path)
    registry = RegistryStore(conn)
    desk = WorkDesk(registry=registry, accounts=NodeAccountStore(conn))
    ledger = NodeProvenance(conn)
    gateway = GatewayApp(
        app._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        nodeplace=NodeplaceService(registry),
        desk=desk,
        provenance=ledger,
    )
    node = Node(
        noder_principal="user-1",
        tenant_id="t1",
        skill_id="fn-provenance",
        visibility=Visibility.PUBLIC,
    )
    registry.add_node(node)
    desk.create_account(node.node_id, principal="user-1", tenant="t1")
    return gateway, conn, ident, ledger, node.node_id


def test_the_history_and_release_doors_are_desk_walled(tmp_path):
    gateway, conn, ident, ledger, node_id = _host(tmp_path)
    try:
        ledger.commit(
            "t1", node_id, {"main.py": "a"}, kind="build",
            instruction="build", by="user-1",
        )
        ledger.commit(
            "t1", node_id, {"main.py": "b"}, kind="revise",
            instruction="fix", by="user-1",
        )
        sealed = ledger.seal(
            "t1", node_id, tree={"main.py": "b"}, verified_by_run="r1"
        )

        commits = gateway.handle(
            _req(
                "GET", f"/v1/work/nodes/{node_id}/commits",
                token=ident.token("user-1", "t1"),
            )
        )
        assert commits.status == 200
        assert [c["kind"] for c in commits.body["items"]] == [
            "revise", "build",
        ]
        releases = gateway.handle(
            _req(
                "GET", f"/v1/work/nodes/{node_id}/releases",
                token=ident.token("user-1", "t1"),
            )
        )
        assert releases.status == 200
        assert releases.body["items"][0]["status"] == "active"

        # A stranger's desk holds no such node: both doors answer 404.
        walled = gateway.handle(
            _req(
                "GET", f"/v1/work/nodes/{node_id}/commits",
                token=ident.token("stranger", "t1"),
            )
        )
        assert walled.status == 404

        # Revocation demands the reason, lands once, and answers status.
        unsaid = gateway.handle(
            _req(
                "POST",
                f"/v1/work/nodes/{node_id}/releases/{sealed.release_id}/revoke",
                token=ident.token("user-1", "t1"),
                body={},
            )
        )
        assert unsaid.status == 400
        revoked = gateway.handle(
            _req(
                "POST",
                f"/v1/work/nodes/{node_id}/releases/{sealed.release_id}/revoke",
                token=ident.token("user-1", "t1"),
                body={"reason": "credential leak"},
            )
        )
        assert revoked.status == 200
        assert revoked.body["status"] == "revoked"
        again = gateway.handle(
            _req(
                "POST",
                f"/v1/work/nodes/{node_id}/releases/{sealed.release_id}/revoke",
                token=ident.token("user-1", "t1"),
                body={"reason": "different words"},
            )
        )
        # Idempotent — and the FIRST reason stands.
        assert again.status == 200
        assert again.body["reason"] == "credential leak"
        missing = gateway.handle(
            _req(
                "POST",
                f"/v1/work/nodes/{node_id}/releases/nr0000000000000000000000/revoke",
                token=ident.token("user-1", "t1"),
                body={"reason": "x"},
            )
        )
        assert missing.status == 404
    finally:
        conn.close()
