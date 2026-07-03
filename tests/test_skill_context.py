from __future__ import annotations

import pytest

from workflow_gps.skills.context import (
    PlanningContextBuilder,
    SkillContextBuilder,
    render_tool_env,
    render_tool_manifest,
    select_tools,
)
from workflow_gps.skills.discovery import DiscoveredTool
from workflow_gps.skills.models import (
    ActionEvent,
    ReusableSkill,
    SkillParameter,
    SkillSignature,
)
from workflow_gps.skills.registry import SkillRegistry

_TOOLS = [
    DiscoveredTool(
        name="ffmpeg",
        path="/usr/bin/ffmpeg",
        category="media",
        tags=["video", "trim", "convert"],
    ),
    DiscoveredTool(
        name="jq", path="/usr/bin/jq", category="data", tags=["json", "filter"]
    ),
    DiscoveredTool(
        name="pandoc",
        path="/usr/bin/pandoc",
        category="document",
        tags=["markdown", "pdf", "convert"],
    ),
]


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


def test_empty_renderers_return_blank():
    assert render_tool_manifest([]) == ""
    assert render_tool_env([]) == ""


def test_skill_context_builder_falls_back_to_sentinel(registry):
    assert (
        SkillContextBuilder(registry)
        .manifest("nonexistent-xyz-term")
        .startswith("No registered")
    )


def test_select_tools_by_intent():
    picked = select_tools(_TOOLS, "trim a video clip", limit=5)
    assert [t.name for t in picked] == ["ffmpeg"]
    # A term that matches nothing yields nothing (don't clutter the context).
    assert select_tools(_TOOLS, "zzz", limit=2) == []
    # An empty intent falls back to everything, capped by limit.
    assert len(select_tools(_TOOLS, "", limit=2)) == 2


def test_render_tool_env_lists_name_category_path():
    text = render_tool_env([_TOOLS[0]])
    assert "ffmpeg [media] at /usr/bin/ffmpeg" in text
    assert "video" in text


def test_planning_context_combines_skills_and_tools(registry):
    builder = PlanningContextBuilder(registry, tools=_TOOLS, max_skills=2, max_tools=2)
    manifest = builder.manifest("extract a paginated table and convert to pdf")
    assert "Registered skills" in manifest
    assert "Paginated Table" in manifest
    assert "Local tools" in manifest
    assert "pandoc" in manifest  # matched "convert"/"pdf"
    assert "jq" not in manifest  # irrelevant to this intent


def test_planning_context_tools_only_and_empty():
    tools_only = PlanningContextBuilder(tools=_TOOLS).manifest("filter json")
    assert "Local tools" in tools_only
    assert "Registered skills" not in tools_only

    assert PlanningContextBuilder().manifest("anything").startswith("No registered")
