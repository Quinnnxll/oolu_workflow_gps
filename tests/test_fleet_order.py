"""The Supernode owner's SOP: an execution order over the fleet.

Exit gate (Issue 15): not every node created under a Supernode audits —
the org's ROOT Supernode always does (humans in full control), but under
it the creator chooses, nested divisions included. And the owner of a
Supernode orders the fleet like an SOP: work flows to the next number
(explicit hand-off), members sharing a number run in PARALLEL, a member
with no number is called whenever needed. The order is MUTABLE (an SOP
is retuned as the org learns), owner-gated, and binds at execution: a
submitted contract carrying ordered members gains ``provenance="sop"``
edges the scheduler honors — while typed data flow outranks the SOP, so
a contradiction surfaces as parallelism, never a cycle.
"""

from __future__ import annotations

import pytest
from test_http_gateway import _app, _req

from oolu.durable.connection import DurableConnection
from oolu.gateway import GatewayApp
from oolu.nodeplace import (
    NodeAccountStore,
    NodeplaceService,
    RegistryStore,
    WorkDesk,
)
from oolu.nodeplace.errors import ContributionError, OwnershipError
from oolu.nodeplace.models import Node, NodeVersion, Visibility
from oolu.skills.contract import (
    ContractEdge,
    NodeContract,
    ScriptBody,
    Slot,
    SubgraphBody,
)

BOSS = "boss-1"


def _rig(tmp_path):
    conn = DurableConnection(tmp_path / "d.db")
    registry = RegistryStore(conn)
    desk = WorkDesk(registry=registry, accounts=NodeAccountStore(conn))
    return conn, registry, desk


def _node(registry, *, skill_id: str, principal: str = BOSS) -> str:
    node = Node(
        noder_principal=principal,
        tenant_id="t1",
        skill_id=skill_id,
        visibility=Visibility.PUBLIC,
    )
    registry.add_node(node)
    return node.node_id


def _version(registry, node_id: str) -> str:
    version = NodeVersion(
        node_id=node_id,
        semver="1.0.0",
        content_hash="h",
        sanitized_skill_json="{}",
    )
    registry.add_version(version)
    return version.version_id


def _supernode(registry, desk, *, skill_id: str = "org.root") -> str:
    node_id = _node(registry, skill_id=skill_id)
    desk.create_account(
        node_id, principal=BOSS, tenant="t1", is_supernode=True
    )
    return node_id


def _member(registry, desk, supernode_id: str, *, skill_id: str) -> str:
    node_id = _node(registry, skill_id=skill_id)
    desk.create_account(
        node_id,
        principal=BOSS,
        tenant="t1",
        supernode_id=supernode_id,
        authority_level=1,
    )
    return node_id


# --------------------------------------------------------------------------- #
# Audit: the root answers for the org; under it, the owner chooses.            #
# --------------------------------------------------------------------------- #
def test_root_supernode_audits_but_under_it_the_owner_chooses(tmp_path):
    conn, registry, desk = _rig(tmp_path)
    try:
        root = _supernode(registry, desk)
        # The org's root cannot opt out — humans in full control.
        assert desk.account_for(root).audit_mode is True

        # A nested division Supernode takes the creator's choice.
        division = _node(registry, skill_id="org.division")
        account = desk.create_account(
            division,
            principal=BOSS,
            tenant="t1",
            is_supernode=True,
            supernode_id=root,
            authority_level=2,
            audit_mode=False,
        )
        assert account.audit_mode is False

        # A plain member chooses too — in BOTH directions.
        free = _member(registry, desk, root, skill_id="org.free")
        assert desk.account_for(free).audit_mode is False
        watched = _node(registry, skill_id="org.watched")
        assert desk.create_account(
            watched,
            principal=BOSS,
            tenant="t1",
            supernode_id=root,
            audit_mode=True,
        ).audit_mode is True
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The dial: owner-gated, validated, clearable.                                 #
# --------------------------------------------------------------------------- #
def test_exec_order_is_the_supernode_owners_dial(tmp_path):
    conn, registry, desk = _rig(tmp_path)
    try:
        root = _supernode(registry, desk)
        intake = _member(registry, desk, root, skill_id="org.intake")
        billing = _member(registry, desk, root, skill_id="org.billing")

        # Serial and parallel: numbers, with ties sharing a group.
        assert desk.set_exec_order(
            intake, principal=BOSS, tenant="t1", order=1
        ).exec_order == 1
        assert desk.set_exec_order(
            billing, principal=BOSS, tenant="t1", order=1
        ).exec_order == 1
        # Retuning is allowed — an SOP is mutable, unlike the regime.
        assert desk.set_exec_order(
            billing, principal=BOSS, tenant="t1", order=2
        ).exec_order == 2
        # Clearing returns the node to called-whenever-needed.
        assert desk.set_exec_order(
            billing, principal=BOSS, tenant="t1", order=None
        ).exec_order is None

        # Only the Supernode's own humans turn the dial.
        with pytest.raises(OwnershipError, match="Supernode's own humans"):
            desk.set_exec_order(
                intake, principal="stranger", tenant="t1", order=3
            )
        # A standalone node has no place in anyone's SOP.
        lone = _node(registry, skill_id="org.lone")
        desk.create_account(lone, principal=BOSS, tenant="t1")
        with pytest.raises(ValueError, match="only under a Supernode"):
            desk.set_exec_order(lone, principal=BOSS, tenant="t1", order=1)
        # A step is a small whole number.
        with pytest.raises(ValueError, match="small step number"):
            desk.set_exec_order(intake, principal=BOSS, tenant="t1", order=0)
        with pytest.raises(ContributionError):
            desk.set_exec_order(
                "missing", principal=BOSS, tenant="t1", order=1
            )
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The edges: present groups chain; ties stay parallel; fleets independent.     #
# --------------------------------------------------------------------------- #
def test_sop_edges_follow_present_groups(tmp_path):
    conn, registry, desk = _rig(tmp_path)
    try:
        root = _supernode(registry, desk)
        intake_a = _member(registry, desk, root, skill_id="org.intake.a")
        intake_b = _member(registry, desk, root, skill_id="org.intake.b")
        ship = _member(registry, desk, root, skill_id="org.ship")
        helper = _member(registry, desk, root, skill_id="org.helper")
        for node_id, order in ((intake_a, 1), (intake_b, 1), (ship, 3)):
            desk.set_exec_order(
                node_id, principal=BOSS, tenant="t1", order=order
            )
        versions = {
            name: _version(registry, node_id)
            for name, node_id in {
                "a": intake_a, "b": intake_b, "ship": ship, "helper": helper,
            }.items()
        }

        edges = desk.sop_edges_for(list(versions.values()))
        # Both order-1 members hand off to the NEXT PRESENT group (3 —
        # nothing carries 2 here); ties get no edge between themselves,
        # and the unordered helper imposes and receives nothing.
        assert set(edges) == {
            (versions["a"], versions["ship"]),
            (versions["b"], versions["ship"]),
        }

        # A second Supernode's SOP never entangles with the first.
        other = _supernode(registry, desk, skill_id="org.other")
        rival = _member(registry, desk, other, skill_id="org.rival")
        desk.set_exec_order(rival, principal=BOSS, tenant="t1", order=9)
        rival_version = _version(registry, rival)
        edges = desk.sop_edges_for(
            [*versions.values(), rival_version]
        )
        assert (versions["ship"], rival_version) not in edges
        assert (rival_version, versions["a"]) not in edges
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The gateway: the dial's route, and the stamp on submitted contracts.         #
# --------------------------------------------------------------------------- #
def _host(tmp_path):
    app, conn, ident = _app(tmp_path)
    registry = RegistryStore(conn)
    desk = WorkDesk(registry=registry, accounts=NodeAccountStore(conn))
    gateway = GatewayApp(
        app._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        nodeplace=NodeplaceService(registry),
        desk=desk,
    )
    return gateway, conn, ident, registry, desk


def test_the_order_route_is_owner_gated(tmp_path):
    gateway, conn, ident, registry, desk = _host(tmp_path)
    try:
        root = _supernode(registry, desk)
        member = _member(registry, desk, root, skill_id="org.m")

        set_order = gateway.handle(
            _req(
                "POST",
                f"/v1/work/nodes/{member}/order",
                token=ident.token(BOSS, "t1"),
                body={"order": 2},
            )
        )
        assert set_order.status == 200, set_order.body
        assert set_order.body["exec_order"] == 2

        cleared = gateway.handle(
            _req(
                "POST",
                f"/v1/work/nodes/{member}/order",
                token=ident.token(BOSS, "t1"),
                body={"order": None},
            )
        )
        assert cleared.status == 200 and cleared.body["exec_order"] is None

        stranger = gateway.handle(
            _req(
                "POST",
                f"/v1/work/nodes/{member}/order",
                token=ident.token("user-2", "t1"),
                body={"order": 1},
            )
        )
        assert stranger.status == 403
        assert gateway.handle(
            _req(
                "POST",
                f"/v1/work/nodes/{member}/order",
                token=ident.token(BOSS, "t1"),
                body={"order": "first"},
            )
        ).status == 400
    finally:
        conn.close()


def _child(version_id: str, name: str, **slots) -> NodeContract:
    return NodeContract(
        id=version_id,
        name=name,
        body=ScriptBody(goal=name),
        consumes=list(slots.get("consumes", [])),
        produces=list(slots.get("produces", [])),
    )


def test_submitted_contracts_wear_the_owners_sop(tmp_path):
    gateway, conn, ident, registry, desk = _host(tmp_path)
    try:
        root = _supernode(registry, desk)
        intake = _member(registry, desk, root, skill_id="org.intake")
        ship = _member(registry, desk, root, skill_id="org.ship")
        for node_id, order in ((intake, 1), (ship, 2)):
            desk.set_exec_order(
                node_id, principal=BOSS, tenant="t1", order=order
            )
        first, second = _version(registry, intake), _version(registry, ship)

        # No slot relation between the children: the SOP alone orders them.
        contract = NodeContract(
            name="fleet-flow",
            body=SubgraphBody(
                nodes=[_child(first, "intake"), _child(second, "ship")],
                edges=[],
            ),
        )
        stamped = gateway._stamp_fleet_order(contract)
        [edge] = stamped.body.edges
        assert (edge.source, edge.target) == (first, second)
        assert edge.provenance == "sop"

        # Typed data flow outranks the SOP: when slots already order the
        # children the OTHER way, the contradicting sop edge is dropped —
        # parallelism by data, never a learned cycle.
        raw = Slot(name="raw", value_type="str")
        need = Slot(name="raw", value_type="str")
        against = NodeContract(
            name="against-the-grain",
            body=SubgraphBody(
                nodes=[
                    _child(first, "intake", consumes=[need]),
                    _child(second, "ship", produces=[raw]),
                ],
                edges=[],
            ),
        )
        assert gateway._stamp_fleet_order(against).body.edges == []

        # Unordered strangers to the fleet stay untouched entirely.
        lone = NodeContract(
            name="solo",
            body=SubgraphBody(nodes=[_child("v-x", "solo")], edges=[]),
        )
        assert gateway._stamp_fleet_order(lone).body.edges == []
    finally:
        conn.close()


def test_existing_explicit_edges_are_never_duplicated(tmp_path):
    gateway, conn, ident, registry, desk = _host(tmp_path)
    try:
        root = _supernode(registry, desk)
        intake = _member(registry, desk, root, skill_id="org.intake")
        ship = _member(registry, desk, root, skill_id="org.ship")
        for node_id, order in ((intake, 1), (ship, 2)):
            desk.set_exec_order(
                node_id, principal=BOSS, tenant="t1", order=order
            )
        first, second = _version(registry, intake), _version(registry, ship)
        contract = NodeContract(
            name="already-ordered",
            body=SubgraphBody(
                nodes=[_child(first, "intake"), _child(second, "ship")],
                edges=[
                    ContractEdge(
                        source=first, target=second, provenance="learned"
                    )
                ],
            ),
        )
        stamped = gateway._stamp_fleet_order(contract)
        assert len(stamped.body.edges) == 1  # the SOP added nothing new
    finally:
        conn.close()
