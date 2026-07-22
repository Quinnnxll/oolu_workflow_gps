"""The interact agent's new hands: files, folders, members, access.

Exit gate: inside a node's interact window the agent can upload files
into folders (write_file grows a ``folder`` arm), create a new folder
(held open by a ``.keep`` file until real files arrive), mint a member
on the org's access desk, grant an egress host, block a host, and
block a user — every hand flowing through the SAME real handlers as
the Access desk's own buttons, so ownership walls, validation, and
audit bind unchanged. And a member's card names its Supernode in
words, for the onboarder exactly as for the owner.
"""

from __future__ import annotations

from types import SimpleNamespace

from test_http_gateway import _req
from test_node_deletion import _host as _org_host
from test_node_interact import _rig

from oolu.chat import NodeChatTools, _run_tool, _ToolCall
from oolu.durable.connection import DurableConnection
from oolu.durable.files import UserFileStore
from oolu.nodeplace.models import Node, Visibility


def _tools(tmp_path, **hands):
    conn = DurableConnection(tmp_path / "h.db")
    store = UserFileStore(conn)
    tools = NodeChatTools(
        store,
        tenant="t1",
        principal="alice",
        node={"node_id": "n1", "title": "Cleaner", "status": "live"},
        holds_list=lambda: [],
        holds_decide=lambda *_a: "done",
        holds_reply=lambda *_a: "done",
        builder=lambda goal: "built",
        **hands,
    )
    return conn, store, tools


def test_folders_and_uploads_land_in_the_nodes_drawer(tmp_path):
    conn, store, tools = _tools(tmp_path)
    try:
        # A new folder is held open by its .keep until real files arrive.
        said = tools.create_folder("reports/2026")
        assert "created folder reports/2026/" in said
        again = tools.create_folder("reports/2026")
        assert "already exists" in again
        # Upload INTO the folder through the same write_file hand.
        answer, action = _run_tool(
            tools,
            _ToolCall(
                name="write_file",
                args={
                    "name": "july.csv",
                    "content": "a,b\n1,2\n",
                    "folder": "reports/2026",
                },
            ),
        )
        assert answer == "saved reports/2026/july.csv"
        assert action == {"tool": "write_file", "name": "july.csv"}
        files = store.list(tenant="t1", node_id="n1")
        assert {(f.folder, f.name) for f in files} == {
            ("reports/2026", ".keep"),
            ("reports/2026", "july.csv"),
        }
        # Unwired org hands answer honestly instead of crashing.
        assert tools.create_member("X").startswith("error:")
        assert tools.node_access("grant_host", "x.com").startswith("error:")
    finally:
        conn.close()


def test_access_hands_flow_through_the_real_account_door(tmp_path):
    app, conn, ident, registry, desk, node_id, _pending = _rig(tmp_path)
    try:
        session = SimpleNamespace(tenant_id="t1", principal_id="noder-export")
        tools, note = app._node_chat_tools(
            _req("POST", "/v1/chat"), session, node_id
        )
        # The context note teaches the new hands.
        for name in ("create_folder", "create_member", "grant_host",
                     "block_host", "block_user"):
            assert name in note
        # Grant, block host, block user — each lands on the account.
        assert "granted api.example.com" in tools.node_access(
            "grant_host", "api.example.com"
        )
        assert "blocked host bad.example.com" in tools.node_access(
            "block_host", "bad.example.com"
        )
        assert "blocked user spammer-1" in tools.node_access(
            "block_user", "spammer-1"
        )
        account = desk.account_for(node_id)
        assert "api.example.com" in account.network_hosts
        assert "bad.example.com" in account.blocked_hosts
        assert "spammer-1" in account.blocked_users
        # Idempotent in words, not silent double rows.
        assert "already granted" in tools.node_access(
            "grant_host", "api.example.com"
        )
    finally:
        conn.close()


def test_create_member_mints_on_the_orgs_desk(tmp_path):
    app, conn, ident, registry, desk, node_id, _pending = _rig(tmp_path)
    try:
        supernode = Node(
            noder_principal="noder-export",
            tenant_id="t1",
            skill_id="org.hq",
            visibility=Visibility.PUBLIC,
        )
        registry.add_node(supernode)
        desk.create_account(
            supernode.node_id,
            principal="noder-export",
            tenant="t1",
            is_supernode=True,
        )
        session = SimpleNamespace(tenant_id="t1", principal_id="noder-export")
        tools, _ = app._node_chat_tools(
            _req("POST", "/v1/chat"), session, supernode.node_id
        )
        said = tools.create_member("Ledger Bot", 2, False)
        assert said.startswith("Created member “Ledger Bot”"), said
        assert "UNCLAIMED" in said
        members = desk.members_of(supernode.node_id, tenant="t1")
        assert any(m["title"] == "Ledger Bot" for m in members)
        # The standalone audit node refuses in words — orgs mint members.
        alone, _ = app._node_chat_tools(
            _req("POST", "/v1/chat"), session, node_id
        )
        assert "stands alone" in alone.create_member("X")
        # The dispatch reaches the same hand by tool name.
        answer, action = _run_tool(
            tools,
            _ToolCall(
                name="create_member",
                args={"title": "Filing Bot", "authority": 3},
            ),
        )
        assert "Created member “Filing Bot”" in answer
        assert action == {"tool": "create_member"}
    finally:
        conn.close()


def test_a_members_card_names_its_supernode_in_words(tmp_path):
    gateway, conn, ident, desk, _files, super_id, member_id = _org_host(
        tmp_path
    )
    try:
        # The ONBOARDER's list (user-1 answers only for the member —
        # the Supernode is not on their desk) still names the org.
        listed = gateway.handle(
            _req("GET", "/v1/work/nodes", token=ident.token("user-1", "t1"))
        )
        assert listed.status == 200
        (member,) = [
            n for n in listed.body["items"] if n["node_id"] == member_id
        ]
        assert member["supernode_title"] == desk.node_title(super_id)
        assert member["supernode_title"]  # words, never a bare id
    finally:
        conn.close()
