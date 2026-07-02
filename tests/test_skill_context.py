from __future__ import annotations

import pytest

from workflow_gps.skills.context import SkillContextBuilder, render_tool_manifest
from workflow_gps.skills.models import (
    ActionEvent,
    ReusableSkill,
    SkillParameter,
    SkillSignature,
)
from workflow_gps.skills.registry import SkillRegistry


def _skill(name, description, *, params=()):
    return ReusableSkill(
        name=name,
        description=description,
        signature=SkillSignature(application="web", adapter="browser"),
        parameters=[SkillParameter(name=p, value_type="string") for p in params],
        actions=[ActionEvent(correlation_id="c", adapter="browser", operation="run")],
    )


@pytest.fixture
def registry(tmp_path):
    reg = SkillRegistry(tmp_path / "reg.db")
    reg.register(
        _skill("Paginated Table", "extract a paginated table", params=["url"]),
        semver="1.0.0",
        tags=["table", "extract", "pagination"],
    )
    reg.register(
        _skill("Dynamic Dropdown", "interact with a dynamic dropdown"),
        semver="2.0.0",
        tags=["ui", "dropdown"],
    )
    reg.register(
        _skill("2FA Intercept", "solve a 2fa intercept"),
        semver="1.0.0",
        tags=["auth", "2fa"],
    )
    yield reg
    reg.close()


def test_builder_selects_only_relevant_tools(registry):
    builder = SkillContextBuilder(registry, max_tools=1)
    selected = builder.select("scrape a paginated results table")
    assert [s.name for s in selected] == ["Paginated Table"]


def test_manifest_lists_signature_and_version(registry):
    builder = SkillContextBuilder(registry, max_tools=2)
    manifest = builder.manifest("extract table data across pages")
    assert "Paginated Table(url)" in manifest
    assert "@1.0.0" in manifest
    assert "Dynamic Dropdown" not in manifest


def test_manifest_when_nothing_matches():
    assert render_tool_manifest([]) == "No registered tools match this request."
