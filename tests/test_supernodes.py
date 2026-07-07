"""Issue 3: a node's regime is fixed at creation; Supernodes rule authority.

The walls under test: audit / auto-growing / supernode-ness / membership
can never change after creation (the update path has no parameters for
them, and the route refuses them loudly); authority exists only under a
Supernode and only its humans set it; onboarding offers no choices; and
the human desk on a hold can allow, sign (recorded in the audit), or type
a reply back to the requester.
"""

from __future__ import annotations

import pytest
from test_contract_run import _assembled_contract, _CliExecutor, _seed_chain
from test_http_gateway import _req
from test_work_desk import _desk_build

from oolu.nodeplace import OwnershipError


def _two_nodes(app, ident, registry):
    exporter, cleaner = _seed_chain(app, ident, registry)
    return (
        registry.get_version(exporter).node_id,
        registry.get_version(cleaner).node_id,
    )


def test_a_supernode_rules_its_members_authority(tmp_path):
    app, conn, ident, registry, *_rest, desk, _ = _desk_build(tmp_path)
    try:
        super_id, member_id = _two_nodes(app, ident, registry)

        # The Supernode: humans in full control — it always audits, even
        # when the creator didn't ask.
        boss = desk.create_account(
            super_id, principal="noder-export", tenant="t1", is_supernode=True
        )
        assert boss.is_supernode and boss.audit_mode is True

        # A member created under it carries an authority level. Only the
        # Supernode's creator could do this (ownership enforced below).
        member = desk.create_account(
            member_id,
            principal="noder-export",
            tenant="t1",
            supernode_id=super_id,
            authority_level=3,
            audit_mode=False,
        )
        assert member.supernode_id == super_id
        assert member.authority_level == 3

        # Its authority is the Supernode humans' call: they may change it;
        # anyone else — including the member's own responsible if that were
        # someone else — is refused.
        raised = desk.update_account(
            member_id, principal="noder-export", tenant="t1", authority_level=5
        )
        assert raised.authority_level == 5
        with pytest.raises(OwnershipError, match="Supernode's humans"):
            desk.update_account(
                member_id, principal="stranger", tenant="t1", authority_level=1
            )
    finally:
        conn.close()


def test_creation_rules_are_walls(tmp_path):
    app, conn, ident, registry, *_rest, desk, _ = _desk_build(tmp_path)
    try:
        super_id, node_id = _two_nodes(app, ident, registry)

        # Authority without a Supernode: refused — standalone nodes have none.
        with pytest.raises(ValueError, match="only under a Supernode"):
            desk.create_account(
                node_id, principal="noder-export", tenant="t1", authority_level=2
            )

        # Under a Supernode that doesn't exist / isn't yours: refused.
        with pytest.raises(Exception, match="no Supernode"):
            desk.create_account(
                node_id,
                principal="noder-export",
                tenant="t1",
                supernode_id="not-a-node",
            )
        desk.create_account(
            super_id, principal="noder-export", tenant="t1", is_supernode=True
        )
        with pytest.raises(OwnershipError, match="own the Supernode"):
            desk.create_account(
                node_id,
                principal="someone-else",
                tenant="t1",
                supernode_id=super_id,
            )

        # An account, once created, can never be created again — the
        # regime was fixed the first time.
        desk.create_account(
            node_id, principal="noder-export", tenant="t1", audit_mode=True
        )
        with pytest.raises(OwnershipError, match="fixed"):
            desk.create_account(
                node_id, principal="noder-export", tenant="t1", audit_mode=False
            )
    finally:
        conn.close()


def test_the_route_refuses_fixed_traits_and_onboard_takes_no_choices(tmp_path):
    app, conn, ident, registry, *_rest, desk, _ = _desk_build(tmp_path)
    try:
        _, node_id = _two_nodes(app, ident, registry)
        token = ident.token("noder-export", "t1")

        created = app.handle(
            _req(
                "POST",
                f"/v1/work/nodes/{node_id}/account",
                token=token,
                body={"audit_mode": True, "allow_autodev_data": False},
            )
        )
        assert created.status == 200
        assert created.body["audit_mode"] is True
        assert created.body["authority_level"] is None  # standalone

        # Any fixed trait in an update is refused loudly, never merged.
        for trait in (
            {"audit_mode": False},
            {"allow_autodev_data": True},
            {"is_supernode": True},
            {"supernode_id": "x"},
        ):
            refused = app.handle(
                _req(
                    "POST",
                    f"/v1/work/nodes/{node_id}/account",
                    token=token,
                    body=trait,
                )
            )
            assert refused.status == 409, trait
            assert "fixed at creation" in refused.body["error"]["message"]
        # The regime is exactly as created.
        assert desk.account_for(node_id).audit_mode is True

        # Onboarding sends no choices — and changes nothing.
        onboarded = app.handle(
            _req(
                "POST",
                f"/v1/work/nodes/{node_id}/account",
                token=token,
                body={"onboard": True},
            )
        )
        assert onboarded.status == 200
        assert onboarded.body["audit_mode"] is True
        assert onboarded.body["allow_autodev_data"] is False
    finally:
        conn.close()


def test_the_desk_can_sign_and_reply_on_a_held_request(tmp_path):
    app, conn, ident, registry, *_rest, desk, _ = _desk_build(
        tmp_path, executors={"cli": _CliExecutor()}
    )
    try:
        exporter, _ = _seed_chain(app, ident, registry)
        node_id = registry.get_version(exporter).node_id
        desk.create_account(
            node_id, principal="noder-export", tenant="t1", audit_mode=True
        )
        contract = _assembled_contract(app, ident)
        held = app.handle(
            _req(
                "POST",
                "/v1/runs/contract",
                token=ident.token("consumer", "t2"),
                body={"contract": contract},
            )
        )
        assert held.status == 202, held.body
        pending_id = held.body["pending_id"]
        from test_contract_run import _grant_approver

        _grant_approver(ident, "approver-2", "t2")
        approver = ident.token("approver-2", "t2")

        # Typing and sending a reply — without deciding anything yet.
        replied = app.handle(
            _req(
                "POST",
                f"/v1/runs/contract/holds/{pending_id}/reply",
                token=approver,
                body={"message": "who requested this export?"},
            )
        )
        assert replied.status == 200
        assert replied.body["replies"][0]["message"] == "who requested this export?"
        listing = app.handle(
            _req("GET", "/v1/runs/contract/holds", token=approver)
        )
        assert listing.body["items"][0]["replies"][0]["author"] == "approver-2"

        # Signing and allowing: the signature lands in the audit trail.
        signed = app.handle(
            _req(
                "POST",
                f"/v1/runs/contract/holds/{pending_id}",
                token=approver,
                body={"approved": True, "signature": "Quinn M."},
            )
        )
        assert signed.status == 200, signed.body
        approvals = [
            e
            for e in app._durable.audit.records()
            if e.event_type == "contract.approved"
        ]
        assert approvals[-1].payload["signature"] == "Quinn M."
    finally:
        conn.close()
