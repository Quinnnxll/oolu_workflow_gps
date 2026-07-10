"""Daily execution logs and the readable Supernode feed.

Every fetch of a node's activity materializes that day's execution log
file in the node's own drawer (logs/execution-YYYY-MM-DD.log) — the
full-fidelity record (ISO timestamps, run ids, executing node, raw event
types) kept for legal use and pruned after ``account.log_retention_days``.
A Supernode's feed aggregates its members' executions, each item naming
the node that executed it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from test_contract_run import _CliExecutor, _contribute_and_publish
from test_http_gateway import Request
from test_work_desk import _desk_build, _run_paid_contract

from oolu.durable import UserFile, UserFileStore

TIDY = {"name": "invoice_csv_tidy", "value_type": "str"}


def _live_req(ident, method, path, *, principal="noder-export", tenant="t1"):
    """A request stamped with the REAL clock, so 'today' matches the audit
    trail's own timestamps (tokens are minted at the same moment)."""
    now = datetime.now(UTC)
    token = ident._signer.mint(subject=principal, tenant_id=tenant, now=now)
    return Request(
        method=method,
        path=path,
        headers={"Authorization": f"Bearer {token}"},
        query={},
        body=None,
        now=now,
    )


def _rig(tmp_path):
    app, conn, ident, registry, metering, attribution, audit, desk, _ = (
        _desk_build(tmp_path, executors={"cli": _CliExecutor()})
    )
    app._files = UserFileStore(conn)
    exporter, _cleaner, run_id = _run_paid_contract(
        app, conn, ident, registry, metering, attribution, audit
    )
    node_id = registry.get_version(exporter).node_id
    return app, conn, ident, registry, desk, node_id, run_id


def test_activity_materializes_the_daily_log_idempotently(tmp_path):
    app, conn, ident, registry, desk, node_id, run_id = _rig(tmp_path)
    try:
        resp = app.handle(
            _live_req(ident, "GET", f"/v1/work/nodes/{node_id}/activity")
        )
        assert resp.status == 200, resp.body
        assert resp.body["items"][0]["node_title"] == "raw exporter"

        today = datetime.now(UTC).date().isoformat()
        files = app._files.list(tenant="t1", node_id=node_id)
        (log,) = [f for f in files if f.folder == "logs"]
        assert log.name == f"execution-{today}.log"
        # Full fidelity for legal use: ISO time, run id, node, raw event.
        assert run_id in log.content
        assert "raw exporter" in log.content
        assert "kept for legal use" in log.content

        # A second fetch adds nothing twice.
        app.handle(_live_req(ident, "GET", f"/v1/work/nodes/{node_id}/activity"))
        (again,) = [
            f
            for f in app._files.list(tenant="t1", node_id=node_id)
            if f.folder == "logs"
        ]
        assert again.content == log.content
    finally:
        conn.close()


def test_logs_past_the_retention_window_are_pruned(tmp_path):
    app, conn, ident, registry, desk, node_id, run_id = _rig(tmp_path)
    try:
        app._files.save(
            UserFile(
                tenant_id="t1",
                node_id=node_id,
                name="execution-2020-01-01.log",
                folder="logs",
                content="# ancient",
            )
        )
        app.handle(_live_req(ident, "GET", f"/v1/work/nodes/{node_id}/activity"))
        names = {
            f.name
            for f in app._files.list(tenant="t1", node_id=node_id)
            if f.folder == "logs"
        }
        # The default 180-day window keeps today's log and drops 2020's.
        assert f"execution-{datetime.now(UTC).date().isoformat()}.log" in names
        assert "execution-2020-01-01.log" not in names
    finally:
        conn.close()


def test_supernode_activity_names_the_executing_member(tmp_path):
    app, conn, ident, registry, desk, member_id, run_id = _rig(tmp_path)
    try:
        version = _contribute_and_publish(
            app,
            ident,
            registry,
            name="finance division",
            noder="noder-export",
            price=0.10,
            produces=[TIDY],
            consumes=[],
        )
        super_id = registry.get_version(version).node_id
        desk.create_account(
            super_id, principal="noder-export", tenant="t1", is_supernode=True
        )
        desk.create_account(
            member_id,
            principal="noder-export",
            tenant="t1",
            supernode_id=super_id,
        )

        resp = app.handle(
            _live_req(ident, "GET", f"/v1/work/nodes/{super_id}/activity")
        )
        assert resp.status == 200, resp.body
        # The member's execution appears on the Supernode's feed, tagged
        # with the executing node's NAME — never just an id.
        by_title = {i["node_title"] for i in resp.body["items"]}
        assert "raw exporter" in by_title
        member_item = next(
            i for i in resp.body["items"] if i["node_title"] == "raw exporter"
        )
        assert member_item["run_id"] == run_id
        assert member_item["steps"], "the steps ride along"
    finally:
        conn.close()
