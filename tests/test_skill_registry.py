from __future__ import annotations

import pytest

from workflow_gps.skills.models import ActionEvent, ReusableSkill, SkillSignature
from workflow_gps.skills.registry import SkillRegistry


def _skill(name, description, *ops, params=()):
    from workflow_gps.skills.models import SkillParameter

    return ReusableSkill(
        name=name,
        description=description,
        signature=SkillSignature(application="web", adapter="browser"),
        parameters=[SkillParameter(name=p, value_type="string") for p in params],
        actions=[
            ActionEvent(correlation_id="c", adapter="browser", operation=op)
            for op in ops
        ],
    )


@pytest.fixture
def registry(tmp_path):
    reg = SkillRegistry(tmp_path / "reg.db")
    yield reg
    reg.close()


def test_register_and_get_latest(registry):
    skill = _skill("Dynamic Dropdown", "interact with a dynamic dropdown", "click")
    registry.register(skill, semver="1.0.0", tags=["ui", "dropdown"])
    registry.register(skill, semver="1.2.0", tags=["ui", "dropdown"])
    registry.register(skill, semver="1.10.0", tags=["ui", "dropdown"])

    latest = registry.get(skill.id)
    assert latest is not None
    assert latest.semver == "1.10.0"
    assert registry.versions(skill.id) == ["1.10.0", "1.2.0", "1.0.0"]
    assert registry.get(skill.id, semver="1.0.0").semver == "1.0.0"


def test_content_hash_is_stable_and_versions_are_immutable(registry):
    skill = _skill("2FA Intercept", "solve a 2FA intercept", "read_otp")
    first = registry.register(skill, semver="1.0.0")
    again = registry.register(skill, semver="1.0.0")
    assert first.content_hash == again.content_hash

    mutated = _skill("2FA Intercept", "solve a 2FA intercept differently", "read_otp")
    mutated = mutated.model_copy(update={"id": skill.id})
    with pytest.raises(ValueError):
        registry.register(mutated, semver="1.0.0")


def test_search_ranks_relevant_skills_first(registry):
    registry.register(
        _skill("Paginated Table", "extract a paginated table", "read_rows"),
        semver="1.0.0",
        tags=["table", "pagination", "extract"],
    )
    registry.register(
        _skill("Dynamic Dropdown", "interact with a dynamic dropdown", "click"),
        semver="1.0.0",
        tags=["ui", "dropdown"],
    )

    results = registry.search("extract table with pagination", limit=5)
    assert results[0].skill.name == "Paginated Table"
    assert results[0].score > 0


def test_search_limit_and_empty_query(registry):
    for i in range(5):
        registry.register(_skill(f"skill {i}", "does a thing", "run"), semver="1.0.0")
    assert len(registry.search("", limit=3)) == 3
    assert registry.search("nonexistent-term-xyz") == []


def test_registry_survives_reopen(tmp_path):
    reg = SkillRegistry(tmp_path / "reg.db")
    skill = _skill("Persisted", "persists across reopen", "run")
    reg.register(skill, semver="2.1.0")
    reg.close()

    reopened = SkillRegistry(tmp_path / "reg.db")
    try:
        got = reopened.get(skill.id)
        assert got is not None and got.semver == "2.1.0"
    finally:
        reopened.close()
