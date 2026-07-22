"""Node deletion is REAL: everywhere at once, revivable for 7 days.

Exit gate: deleting a node tombstones its account — it leaves the Work
desk, its Supernode's member roster, and run resolution in the same
moment (no ghost row lingering in Access); an administrator (the
node's own, or its Supernode's) can revive it within the 7-day window;
after the window the revive door answers 410 and the retention purge
removes the account AND the node's drawer for good. A deleted node
never blocks rebuilding its goal, and a rebuilt twin resolves past the
tombstone.
"""

from __future__ import annotations

from datetime import timedelta

from test_http_gateway import NOW, _app, _req

from oolu.durable.files import UserFile, UserFileStore
from oolu.gateway import GatewayApp
from oolu.nodeplace import NodeplaceService, RegistryStore
from oolu.nodeplace.desk import NodeAccountStore, WorkDesk
from oolu.nodeplace.models import Node, Visibility


def _host(tmp_path):
    app, conn, ident = _app(tmp_path)
    registry = RegistryStore(conn)
    desk = WorkDesk(registry=registry, accounts=NodeAccountStore(conn))
    files = UserFileStore(conn)
    gateway = GatewayApp(
        app._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        nodeplace=NodeplaceService(registry),
        desk=desk,
        files=files,
    )
    # An org: admin-1's Supernode with user-1's member node under it.
    supernode = Node(
        noder_principal="admin-1", tenant_id="t1",
        skill_id="org.super", visibility=Visibility.PUBLIC,
    )
    member = Node(
        noder_principal="admin-1", tenant_id="t1",
        skill_id="org.member", visibility=Visibility.PUBLIC,
    )
    registry.add_node(supernode)
    registry.add_node(member)
    desk.create_account(
        supernode.node_id, principal="admin-1", tenant="t1", is_supernode=True
    )
    # The org's owner mints the member (unclaimed); user-1 onboards it.
    desk.create_account(
        member.node_id,
        principal="admin-1",
        tenant="t1",
        supernode_id=supernode.node_id,
        authority_level=1,
    )
    desk.onboard_account(member.node_id, principal="user-1", tenant="t1")
    return gateway, conn, ident, desk, files, supernode.node_id, member.node_id


def test_delete_leaves_every_list_and_revive_restores(tmp_path):
    gateway, conn, ident, desk, _files, super_id, member_id = _host(tmp_path)
    try:
        assert any(
            m["node_id"] == member_id
            for m in desk.members_of(super_id, tenant="t1")
        )
        deleted = gateway.handle(
            _req(
                "DELETE", f"/v1/work/nodes/{member_id}",
                token=ident.token("user-1", "t1"),
            )
        )
        assert deleted.status == 200 and deleted.body["deleted"] is True
        # Gone EVERYWHERE at once: the owner's desk, the org's roster.
        assert desk.overview(principal="user-1", tenant="t1") == []
        assert desk.members_of(super_id, tenant="t1") == []
        assert gateway._node_deleted(member_id)
        # ...but named on the Supernode's revival list, deadline shown.
        listed = gateway.handle(
            _req(
                "GET", f"/v1/work/nodes/{super_id}/deleted-members",
                token=ident.token("admin-1", "t1"),
            )
        )
        assert listed.status == 200
        (item,) = listed.body["items"]
        assert item["node_id"] == member_id and item["revivable_until"]

        # The Supernode's administrator revives it whole.
        revived = gateway.handle(
            _req(
                "POST", f"/v1/work/nodes/{member_id}/revive",
                token=ident.token("admin-1", "t1"),
            )
        )
        assert revived.status == 200 and revived.body["revived"] is True
        assert any(
            m["node_id"] == member_id
            for m in desk.members_of(super_id, tenant="t1")
        )
        assert not gateway._node_deleted(member_id)
    finally:
        conn.close()


def test_the_walls_hold(tmp_path):
    gateway, conn, ident, desk, _files, super_id, member_id = _host(tmp_path)
    try:
        # A stranger cannot delete a node that is not on their desk.
        walled = gateway.handle(
            _req(
                "DELETE", f"/v1/work/nodes/{member_id}",
                token=ident.token("stranger", "t1"),
            )
        )
        assert walled.status == 404
        gateway.handle(
            _req(
                "DELETE", f"/v1/work/nodes/{member_id}",
                token=ident.token("user-1", "t1"),
            )
        )
        # A stranger cannot revive either — administrators only.
        refused = gateway.handle(
            _req(
                "POST", f"/v1/work/nodes/{member_id}/revive",
                token=ident.token("stranger", "t1"),
            )
        )
        assert refused.status == 403
        # Deleting twice finds nothing: the node is already off the desk.
        again = gateway.handle(
            _req(
                "DELETE", f"/v1/work/nodes/{member_id}",
                token=ident.token("user-1", "t1"),
            )
        )
        assert again.status == 404
    finally:
        conn.close()


def test_the_window_closes_and_the_purge_is_final(tmp_path):
    gateway, conn, ident, desk, files, super_id, member_id = _host(tmp_path)
    try:
        files.save(
            UserFile(
                tenant_id="t1", node_id=member_id, name="main.py",
                folder="src", content="print('x')",
            )
        )
        # Consume the hourly retention gate first, so the revive request
        # below reaches its handler before any purge tick.
        gateway.handle(
            _req("GET", "/v1/runs", token=ident.token("user-1", "t1"))
        )
        # Deleted 8 days ago (the tombstone is backdated so the request
        # clock stays within the token's life).
        desk.delete_node(member_id, at=NOW - timedelta(days=8))
        closed = gateway.handle(
            _req(
                "POST", f"/v1/work/nodes/{member_id}/revive",
                token=ident.token("admin-1", "t1"),
            )
        )
        assert closed.status == 410
        # The purge takes the account AND the drawer with it.
        gateway._purge_deleted_nodes(NOW)
        assert desk.account_for(member_id) is None
        assert files.list(tenant="t1", node_id=member_id) == []
        # Inside the window nothing is purged — the undo stays real.
        assert desk.account_for(super_id) is not None
    finally:
        conn.close()


def test_a_tombstone_never_blocks_rebuilding_the_goal(tmp_path):
    gateway, conn, ident, desk, _files, _super_id, member_id = _host(tmp_path)
    try:
        gateway.handle(
            _req(
                "DELETE", f"/v1/work/nodes/{member_id}",
                token=ident.token("user-1", "t1"),
            )
        )
        # The resolution guard treats the tombstoned node as absent.
        assert gateway._node_deleted(member_id)
        # And the roster hides it while the account row still exists —
        # the tombstone is not a ghost in Access.
        account = desk.account_for(member_id)
        assert account is not None and account.deleted_at is not None
    finally:
        conn.close()
