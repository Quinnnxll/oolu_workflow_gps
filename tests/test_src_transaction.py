"""B2: the function lands in src transactionally, and drift heals."""

from __future__ import annotations

from test_chat_assistant import _FakeModel
from test_growth_trigger import GOAL, TASK_TURN, _chat, _rig
from test_http_gateway import _req
from test_node_interact import FakeAuthor

from oolu.identity import AuthorityGrant, Role


def _drawer_main(app, node_id):
    for file in app._files.list(tenant="t1", node_id=node_id):
        if file.folder == "src" and file.name == "main.py":
            return file
    return None


def _grant_ops(ident):
    ident.store.add_role(
        Role(tenant_id="t1", name="ops", permissions=frozenset({"users:manage"}))
    )
    ident.store.add_grant(
        AuthorityGrant(
            tenant_id="t1", principal_id="ops-1", role_name="ops", granted_by="x"
        )
    )


def test_a_publish_lands_src_equal_to_the_version(tmp_path):
    app, conn, ident, desk, script_exec = _rig(tmp_path)
    try:
        app._node_function_author = lambda tenant: FakeAuthor()
        built = _chat(app, ident, "build me a node that " + GOAL)
        assert "Built a NEW node" in built.body["reply"]
        (node,) = app._nodeplace.all_nodes()
        main = _drawer_main(app, node.node_id)
        assert main is not None, "the publish landed the drawer copy"
        version = app._nodeplace.latest_version(node.node_id)
        from oolu.skills.models import ReusableSkill

        skill = ReusableSkill.model_validate_json(version.sanitized_skill_json)
        assert main.content == app._skill_script(skill)
    finally:
        conn.close()


def test_a_write_miss_is_loud_and_stands_in_the_inbox(tmp_path, monkeypatch):
    app, conn, ident, desk, script_exec = _rig(tmp_path)
    try:
        _grant_ops(ident)

        class _RefusingDeskFiles:
            def __init__(self, *args, **kwargs):
                raise RuntimeError("the drawer is on fire")

        monkeypatch.setattr("oolu.gateway.app.DeskFiles", _RefusingDeskFiles)
        app._node_function_author = lambda tenant: FakeAuthor()
        built = _chat(app, ident, "build me a node that " + GOAL)
        reply = built.body["reply"]
        # The publish still stands, and the miss is NAMED in the receipt.
        assert "Built a NEW node" in reply
        assert "did not land" in reply
        assert "heals on its next run" in reply or "heals on" in reply
        # The defined event is on the audit chain...
        unlanded = [
            e for e in app._durable.audit.records()
            if e.event_type == "node.src_unlanded"
        ]
        assert unlanded and "on fire" in unlanded[-1].payload["problem"]
        # ...and the divergence stands in the operator inbox.
        inbox = app.handle(
            _req("GET", "/v1/inbox", token=ident.token("ops-1", "t1"))
        )
        (issue,) = inbox.body["src_issues"]
        assert "missing" in issue["problem"]
        # A member's inbox never carries the tenant-wide section.
        mine = app.handle(
            _req("GET", "/v1/inbox", token=ident.token("user-1", "t1"))
        )
        assert mine.body["src_issues"] == []
    finally:
        conn.close()


def test_a_deleted_drawer_copy_heals_on_the_next_run(tmp_path):
    app, conn, ident, desk, script_exec = _rig(tmp_path)
    try:
        _grant_ops(ident)
        app._node_function_author = lambda tenant: FakeAuthor()
        built = _chat(app, ident, "build me a node that " + GOAL)
        assert "Built a NEW node" in built.body["reply"]
        (node,) = app._nodeplace.all_nodes()
        main = _drawer_main(app, node.node_id)
        app._files.delete(main.file_id, tenant="t1")
        assert _drawer_main(app, node.node_id) is None
        inbox = app.handle(
            _req("GET", "/v1/inbox", token=ident.token("ops-1", "t1"))
        )
        assert len(inbox.body["src_issues"]) == 1  # the divergence stands

        # The next run heals the copy from the version, audited...
        model = _FakeModel([TASK_TURN])
        app._tenant_model = lambda tenant: model
        ran = _chat(app, ident, "please " + GOAL)
        assert ran.status == 200, ran.body
        healed = _drawer_main(app, node.node_id)
        assert healed is not None
        events = [
            e for e in app._durable.audit.records()
            if e.event_type == "node.src_healed"
        ]
        assert events and events[-1].payload["node_id"] == node.node_id
        # ...and the inbox item leaves by itself — a projection resolves.
        inbox = app.handle(
            _req("GET", "/v1/inbox", token=ident.token("ops-1", "t1"))
        )
        assert inbox.body["src_issues"] == []
    finally:
        conn.close()
