"""Issue 3: a node's whole regime — authority included — is fixed at creation.

The walls under test: audit / auto-growing / supernode-ness / membership /
authority level can never change after creation, for anyone — the update
path has no parameters for them and the route refuses them loudly.
Authority exists only under a Supernode and is set once, when the node is
created there; onboarding offers no choices; and the human desk on a hold
can allow, sign (recorded in the audit), or type a reply back.
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


def test_member_authority_is_fixed_at_creation_for_everyone(tmp_path):
    app, conn, ident, registry, *_rest, desk, _ = _desk_build(tmp_path)
    try:
        super_id, member_id = _two_nodes(app, ident, registry)

        # The Supernode: humans in full control — it always audits, even
        # when the creator didn't ask.
        boss = desk.create_account(
            super_id, principal="noder-export", tenant="t1", is_supernode=True
        )
        assert boss.is_supernode and boss.audit_mode is True

        # A member created under it carries its authority level from birth.
        # Only the Supernode's owner can create under it (walls tested below).
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

        # And from then on it is fixed — for everyone, the Supernode's own
        # humans included. The desk has no parameter for it at all...
        with pytest.raises(TypeError):
            desk.update_account(
                member_id,
                principal="noder-export",
                tenant="t1",
                authority_level=5,
            )
        # ...and the route refuses it loudly, like every fixed trait.
        refused = app.handle(
            _req(
                "POST",
                f"/v1/work/nodes/{member_id}/account",
                token=ident.token("noder-export", "t1"),
                body={"authority_level": 5},
            )
        )
        assert refused.status == 409
        assert "fixed at creation" in refused.body["error"]["message"]
        assert desk.account_for(member_id).authority_level == 3
    finally:
        conn.close()


def test_supernodes_nest_with_authority(tmp_path):
    """A division's Supernode lives under the ministry's, carrying an
    authority level like any member — and, like any node created under
    a Supernode, taking its creator's audit choice: only the org's ROOT
    Supernode always audits (Issue 15)."""
    app, conn, ident, registry, *_rest, desk, _ = _desk_build(tmp_path)
    try:
        ministry_id, division_id = _two_nodes(app, ident, registry)
        ministry = desk.create_account(
            ministry_id, principal="noder-export", tenant="t1", is_supernode=True
        )
        assert ministry.audit_mode is True  # the root always audits
        division = desk.create_account(
            division_id,
            principal="noder-export",
            tenant="t1",
            is_supernode=True,
            supernode_id=ministry_id,
            authority_level=3,
        )
        assert division.is_supernode is True
        assert division.supernode_id == ministry_id
        assert division.authority_level == 3
        # Under the root, audit is the creator's choice — the default
        # is off: not every division needs a human countersigning
        # every run.
        assert division.audit_mode is False
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

        # Creation without the upfront policy agreement never happens.
        unsigned = app.handle(
            _req(
                "POST",
                f"/v1/work/nodes/{node_id}/account",
                token=token,
                body={"audit_mode": True, "allow_autodev_data": False},
            )
        )
        assert unsigned.status == 409
        assert "Node Policy" in unsigned.body["error"]["message"]

        created = app.handle(
            _req(
                "POST",
                f"/v1/work/nodes/{node_id}/account",
                token=token,
                body={
                    "audit_mode": True,
                    "allow_autodev_data": False,
                    "accept_policy": True,
                },
            )
        )
        assert created.status == 200
        assert created.body["audit_mode"] is True
        assert created.body["policy_version"]  # the agreement is stamped
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


def test_a_claim_ticket_lands_the_node_on_the_claimers_own_desk(tmp_path):
    """Issue 9: onboarding must work for someone who did NOT create the
    Supernode. A node created under a Supernode is an unclaimed ticket;
    whoever presents its id becomes responsible — and from that moment the
    node appears on THEIR Work desk (overview and activity both), even
    though the registry's creator column still names the Supernode's
    human. Before the fix the onboard call returned 200 but the claimer's
    desk stayed empty, which read as 'onboarding failed'."""
    app, conn, ident, registry, *_rest, desk, _ = _desk_build(tmp_path)
    try:
        super_id, child_id = _two_nodes(app, ident, registry)
        desk.create_account(
            super_id, principal="noder-export", tenant="t1", is_supernode=True
        )
        child = desk.create_account(
            child_id,
            principal="noder-export",
            tenant="t1",
            supernode_id=super_id,
            authority_level=2,
        )
        assert child.responsible == ""  # minted unclaimed — the ticket

        # The claimer is another account entirely — NOT the Supernode's
        # creator. Presenting the node id is the whole ceremony.
        claimer = ident.token("claimer", "t1")
        onboarded = app.handle(
            _req(
                "POST",
                f"/v1/work/nodes/{child_id}/account",
                token=claimer,
                body={"onboard": True},
            )
        )
        assert onboarded.status == 200
        assert onboarded.body["responsible"] == "claimer"
        assert onboarded.body["authority_level"] == 2  # regime untouched

        # The node now lives on the claimer's desk...
        mine = app.handle(_req("GET", "/v1/work/nodes", token=claimer))
        assert child_id in [item["node_id"] for item in mine.body["items"]]
        # ...their activity view opens...
        feed = app.handle(
            _req("GET", f"/v1/work/nodes/{child_id}/activity", token=claimer)
        )
        assert feed.status == 200
        # ...and a bystander's desk stays empty: answering for a node is
        # personal, not tenant-wide.
        theirs = app.handle(
            _req("GET", "/v1/work/nodes", token=ident.token("bystander", "t1"))
        )
        assert theirs.body["items"] == []

        # A claimed node cannot be onboarded away by the next passer-by.
        stolen = app.handle(
            _req(
                "POST",
                f"/v1/work/nodes/{child_id}/account",
                token=ident.token("bystander", "t1"),
                body={"onboard": True},
            )
        )
        assert stolen.status == 403
        assert desk.account_for(child_id).responsible == "claimer"
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
