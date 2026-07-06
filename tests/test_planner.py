from __future__ import annotations

from oolu.orchestrator import SkillRegistryPlanner, classify_risk
from oolu.skills.models import ActionEvent, ReusableSkill, SkillSignature


def _skill(name, *ops):
    return ReusableSkill(
        name=name,
        description=name,
        signature=SkillSignature(application="cli", adapter="cli"),
        actions=[
            ActionEvent(correlation_id="c", adapter="cli", operation=op) for op in ops
        ],
    )


def test_classify_risk_buckets():
    assert classify_risk("list_files") == "read"
    assert classify_risk("get") == "read"
    assert classify_risk("create_invoice") == "write"
    assert classify_risk("run") == "write"
    assert classify_risk("delete_account") == "irreversible"
    assert classify_risk("something_unknown") == "write"


def test_planner_capabilities_and_blueprints():
    planner = SkillRegistryPlanner([_skill("a", "read"), _skill("b", "send", "delete")])
    assert planner.capabilities() == frozenset({"read", "send", "delete"})

    plans = {bp.name: bp for bp in planner.blueprints()}
    assert set(plans) == {"a", "b"}
    assert plans["a"].estimated_cost == 1.0
    assert len(plans["b"].actions) == 2


def test_irreversible_actions_are_reserved():
    (blueprint,) = SkillRegistryPlanner([_skill("danger", "delete")]).blueprints()
    (action,) = blueprint.actions
    assert action.risk == "irreversible"
    assert action.reserved is True
    assert action.required_capabilities == frozenset({"delete"})


def test_skill_with_no_actions_is_skipped():
    empty = ReusableSkill(
        name="empty",
        description="",
        signature=SkillSignature(application="cli", adapter="cli"),
        actions=[],
    )
    assert SkillRegistryPlanner([empty]).blueprints() == []
