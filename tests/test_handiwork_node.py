"""Issue 4: the desktop's prebuilt hands, packaged as ONE Handiwork node.

Every prebuilt function the app ships with shows up in Work as a single
built-in node — named Handiwork, well away from anyone's office-suite
trademarks — owned by the local user, live from birth, seeded exactly
once per install.
"""

from __future__ import annotations

import yaml
from test_work_desk import _desk_build

from oolu.nodeplace import NodeplaceService
from oolu.nodeplace.handiwork import (
    HANDIWORK_SKILL_ID,
    HANDIWORK_TITLE,
    handiwork_skill,
    seed_handiwork_node,
)
from oolu.skills.pack import parse_skill_pack, starter_pack_text


def _starter_skills():
    pack = parse_skill_pack(yaml.safe_load(starter_pack_text()) or {})
    return [entry.to_skill() for entry in pack.skills]


def test_the_manifest_carries_every_prebuilt_function():
    skills = _starter_skills()
    skill = handiwork_skill(skills)
    bundled = {a.parameters["skill_id"] for a in skill.actions}
    # The HTTP hand plus every starter skill, one read-only entry each.
    assert "oolu.hand.http" in bundled
    assert {s.id for s in skills} <= bundled
    assert all(a.operation == "describe" for a in skill.actions)


def test_seeding_creates_one_live_builtin_node_once(tmp_path):
    app, conn, ident, registry, *_rest, desk, _ = _desk_build(tmp_path)
    try:
        nodeplace = NodeplaceService(registry)
        node_id = seed_handiwork_node(
            nodeplace,
            desk,
            registry,
            tenant="t1",
            principal="local",
            skills=_starter_skills(),
        )
        assert node_id is not None

        # It stands in the Work list like any node the user answers for —
        # live from birth (the platform reviewed its functions already),
        # standalone regime, auto-growing on.
        (entry,) = desk.overview(principal="local", tenant="t1")
        assert entry.title == HANDIWORK_TITLE
        assert entry.status == "live"
        assert entry.account.is_supernode is False
        assert entry.account.authority_level is None
        assert entry.account.allow_autodev_data is True

        # Seeding again — every later launch — changes nothing.
        assert (
            seed_handiwork_node(
                nodeplace,
                desk,
                registry,
                tenant="t1",
                principal="local",
                skills=_starter_skills(),
            )
            is None
        )
        assert len(desk.overview(principal="local", tenant="t1")) == 1
        nodes = registry.list_nodes("t1", "local")
        assert [n.skill_id for n in nodes] == [HANDIWORK_SKILL_ID]
    finally:
        conn.close()


def test_the_desktop_runtime_seeds_it_for_the_local_user(tmp_path):
    from test_http_gateway import _req

    from oolu.assembly import build_host_runtime
    from oolu.gateway import GatewayConfig

    runtime = build_host_runtime(
        data_dir=tmp_path / "host",
        secret="a-thirty-two-character-plus-signing-secret",
        config=GatewayConfig(registration_tenant="local"),
        seed_handiwork_for="local",
    )
    try:
        runtime.accounts.bootstrap(
            tenant="local", username="local", password="first-pass"
        )
        token = runtime.accounts.login("local", "first-pass").token
        listed = runtime.gateway.handle(
            _req("GET", "/v1/work/nodes", token=token)
        )
        assert listed.status == 200
        titles = [item["title"] for item in listed.body["items"]]
        assert HANDIWORK_TITLE in titles
    finally:
        runtime.close()
