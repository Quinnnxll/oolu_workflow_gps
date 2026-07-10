"""The node interaction window: OoLu acting ON one node.

Through ``POST /v1/chat`` with a ``node_id``, the assistant gains that
node's desk: listing held requests, deciding and SIGNING them (the manual
floor of final-result audit signing), replying to requesters, and —
gated on the auto-build consent — building execution nodes on the node's
path, registered in the marketplace so they are callable and routable.
Every hand goes through the gateway's own walls: tenant scope, approve
authority, audit; and failed automations carry stable error codes.
"""

from __future__ import annotations

from test_contract_run import (
    RAW,
    TIDY,
    _CliExecutor,
    _grant_approver,
    _seed_chain,
)
from test_http_gateway import _req
from test_work_desk import _desk_build


def _rig(tmp_path):
    """A desk, an audit node owned by 'noder-export' in t1, and one held
    request submitted by a consumer in the SAME tenant (the desktop
    shape: one tenant, owner and consumer alike)."""
    app, conn, ident, registry, *_rest, desk, _ = _desk_build(
        tmp_path, executors={"cli": _CliExecutor()}
    )
    # The chat's hands exist only where a file store does (the desktop
    # always has one); the desk rig adds it explicitly.
    from oolu.durable import UserFileStore

    app._files = UserFileStore(conn)
    exporter, _ = _seed_chain(app, ident, registry)
    node_id = registry.get_version(exporter).node_id
    desk.create_account(
        node_id, principal="noder-export", tenant="t1", audit_mode=True
    )
    assembled = app.handle(
        _req(
            "POST",
            "/v1/market/assemble",
            token=ident.token("noder-export", "t1"),
            body={
                "goal": {"name": "clean-the-books", "want": [TIDY]},
                "q": "invoice",
            },
        )
    )
    assert assembled.status == 200 and assembled.body["complete"], assembled.body
    held = app.handle(
        _req(
            "POST",
            "/v1/runs/contract",
            # Submitted by ANOTHER person in the tenant: a submitter may
            # never approve their own request, and the point of the desk
            # is the node's human deciding someone else's ask.
            token=ident.token("consumer-1", "t1"),
            body={"contract": assembled.body["contract"]},
        )
    )
    assert held.status == 202, held.body
    return app, conn, ident, registry, desk, node_id, held.body["pending_id"]


def _chat(app, ident, node_id, message, *, principal="noder-export"):
    return app.handle(
        _req(
            "POST",
            "/v1/chat",
            token=ident.token(principal, "t1"),
            body={"message": message, "history": [], "node_id": node_id},
        )
    )


def test_pending_and_accelerate_list_the_nodes_held_requests(tmp_path):
    app, conn, ident, registry, desk, node_id, pending_id = _rig(tmp_path)
    try:
        listed = _chat(app, ident, node_id, "pending")
        assert listed.status == 200, listed.body
        assert "clean-the-books" in listed.body["reply"]
        assert listed.body["actions"] == [{"tool": "node_holds"}]

        accelerated = _chat(app, ident, node_id, "accelerate")
        assert "sign all as" in accelerated.body["reply"]
    finally:
        conn.close()


def test_sign_all_decides_the_holds_with_the_typed_signature(tmp_path):
    app, conn, ident, registry, desk, node_id, pending_id = _rig(tmp_path)
    try:
        _grant_approver(ident, "noder-export", "t1")
        signed = _chat(app, ident, node_id, "sign all as Quinn M.")
        assert signed.status == 200, signed.body
        assert "signed and allowed" in signed.body["reply"]

        # The signature rode the approval into the audit trail, and the
        # queue is empty afterwards.
        approvals = [
            e
            for e in app._durable.audit.records()
            if e.event_type == "contract.approved"
        ]
        assert approvals[-1].payload["signature"] == "Quinn M."
        again = _chat(app, ident, node_id, "pending")
        assert "Nothing is waiting" in again.body["reply"]
    finally:
        conn.close()


def test_deciding_without_authority_reports_the_wall_in_words(tmp_path):
    app, conn, ident, registry, desk, node_id, pending_id = _rig(tmp_path)
    try:
        # No approve authority: the gateway wall answers through the chat.
        refused = _chat(app, ident, node_id, "allow clean-the-books")
        assert refused.status == 200
        assert "I couldn't" in refused.body["reply"]
    finally:
        conn.close()


def test_replying_lands_on_the_hold(tmp_path):
    app, conn, ident, registry, desk, node_id, pending_id = _rig(tmp_path)
    try:
        replied = _chat(
            app, ident, node_id, "reply clean-the-books: who asked for this?"
        )
        assert replied.status == 200, replied.body
        assert "Reply sent" in replied.body["reply"]
        listing = app.handle(
            _req(
                "GET",
                "/v1/runs/contract/holds",
                token=ident.token("noder-export", "t1"),
            )
        )
        assert (
            listing.body["items"][0]["replies"][0]["message"]
            == "who asked for this?"
        )
    finally:
        conn.close()


def test_build_node_needs_consent_then_registers_a_callable_child(tmp_path):
    app, conn, ident, registry, desk, node_id, pending_id = _rig(tmp_path)
    try:
        # Consent off: the refusal names the settings switch.
        refused = _chat(app, ident, node_id, "build normalize invoice csv files")
        assert "Auto-build nodes on my paths" in refused.body["reply"]

        from oolu.durable import DurableConnection  # noqa: F401 (import guard)
        from oolu.settings_node import SettingsNode, SettingsStore

        settings = SettingsNode(SettingsStore(conn))
        settings.set("t1", "account.autobuild_consent", True)
        app._settings = settings

        built = _chat(app, ident, node_id, "build normalize invoice csv files")
        assert built.status == 200, built.body
        assert "Built" in built.body["reply"]
        assert built.body["actions"] == [{"tool": "build_node"}]

        # The execution node is REGISTERED — a citizen the planner can
        # route to — named by keywords, owned by the caller (this node is
        # not a Supernode, so it lands standalone on their desk).
        mine = desk.overview(principal="noder-export", tenant="t1")
        titles = {e.title for e in mine}
        assert "Normalize Invoice Csv Files" in titles
        new = next(e for e in mine if e.title == "Normalize Invoice Csv Files")
        assert new.account.responsible == "noder-export"
        assert new.status == "needs_verification"
    finally:
        conn.close()


def test_build_under_a_supernode_starts_unclaimed(tmp_path):
    app, conn, ident, registry, desk, node_id, pending_id = _rig(tmp_path)
    try:
        # Make a Supernode and interact THERE.
        from test_contract_run import _contribute_and_publish

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
        from oolu.settings_node import SettingsNode, SettingsStore

        settings = SettingsNode(SettingsStore(conn))
        settings.set("t1", "account.autobuild_consent", True)
        app._settings = settings

        built = _chat(app, ident, super_id, "build tax filing checker")
        assert "UNCLAIMED" in built.body["reply"], built.body
        mine = desk.overview(principal="noder-export", tenant="t1")
        new = next(e for e in mine if e.title == "Tax Filing Checker")
        assert new.account.supernode_id == super_id
        assert new.account.responsible == ""  # the node id is the claim ticket
    finally:
        conn.close()


def test_a_node_off_the_callers_desk_is_a_404(tmp_path):
    app, conn, ident, registry, desk, node_id, pending_id = _rig(tmp_path)
    try:
        strange = _chat(app, ident, "not-a-node", "pending")
        assert strange.status == 404
        other = _chat(app, ident, node_id, "pending", principal="stranger")
        assert other.status == 404
    finally:
        conn.close()
