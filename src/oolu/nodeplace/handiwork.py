"""The Handiwork node: OoLu's built-in hands, packaged as one node.

Every prebuilt function the desktop ships with — the reviewed starter
skills and the engine's HTTP hand that executes behind them — surfaces in
the Work environment as ONE node named Handiwork, owned by the local
user. One node, not a scatter of internals: the human sees a single
built-in worker they are responsible for, with the usual account,
activity feed, and (fixed-at-creation) regime.

The name is deliberately ours alone — "handiwork" describes built-in,
handmade capability and stays well clear of anyone's office-suite
trademarks.

Seeding is idempotent: the node is created once per install (keyed by its
stable skill id) and left alone on every later launch.
"""

from __future__ import annotations

from ..skills.models import ActionEvent, ReusableSkill, SkillSignature

HANDIWORK_SKILL_ID = "oolu.handiwork"
HANDIWORK_TITLE = "Handiwork"
HANDIWORK_SUMMARY = (
    "OoLu's built-in hands: every prebuilt function this app ships with, "
    "packaged as one node — web reading through the guarded HTTP hand and "
    "the reviewed starter skills."
)

# The engine hand itself: prebuilt whether or not a starter pack loaded.
_HTTP_HAND = {
    "skill_id": "oolu.hand.http",
    "name": "Web fetch (HTTP GET)",
    "summary": "Read public web pages and JSON APIs behind the always-on "
    "SSRF guard.",
}


def handiwork_skill(skills: list[ReusableSkill]) -> ReusableSkill:
    """The manifest skill: one read-only entry per bundled function."""
    functions = [_HTTP_HAND] + [
        {"skill_id": s.id, "name": s.name, "summary": s.description}
        for s in skills
        if s.id != HANDIWORK_SKILL_ID
    ]
    return ReusableSkill(
        id=HANDIWORK_SKILL_ID,
        name=HANDIWORK_TITLE,
        description=HANDIWORK_SUMMARY,
        signature=SkillSignature(application="desktop", adapter="pack"),
        actions=[
            ActionEvent(
                correlation_id=HANDIWORK_SKILL_ID,
                adapter="pack",
                # "describe" is read-only on purpose: the manifest carries
                # no authority of its own — execution stays with each
                # bundled function's own adapter and its guards.
                operation="describe",
                parameters=entry,
            )
            for entry in functions
        ],
    )


def seed_handiwork_node(
    nodeplace,  # nodeplace.NodeplaceService
    desk,  # nodeplace.WorkDesk
    registry,  # nodeplace.RegistryStore
    *,
    tenant: str,
    principal: str,
    skills: list[ReusableSkill] | None = None,
) -> str | None:
    """Create the Handiwork node once; return its id (None if it exists).

    The account takes the standalone defaults — no Supernode, no
    authority, audit off, auto-growing on — and goes straight to "live":
    the bundled functions are the platform's own, reviewed before they
    shipped, so there is nothing left to verify.
    """
    for node in registry.list_nodes(tenant, principal):
        if node.skill_id == HANDIWORK_SKILL_ID:
            return None  # already seeded on an earlier launch
    result = nodeplace.contribute(
        noder_principal=principal,
        tenant_id=tenant,
        skill=handiwork_skill(list(skills or [])),
        semver="1.0.0",
        title=HANDIWORK_TITLE,
        summary=HANDIWORK_SUMMARY,
        tags=["built-in", "hands", "prebuilt"],
        license="builtin",
    )
    node_id = result.node.node_id
    desk.create_account(node_id, principal=principal, tenant=tenant)
    desk.update_account(
        node_id, principal=principal, tenant=tenant, status="live"
    )
    return node_id
