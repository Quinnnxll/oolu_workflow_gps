"""Creative-app learning: the source file is the lesson, never the replay.

Photoshop, SolidWorks, Blender work is learned from its SOURCE FILES
(.psd, .sldprt, .blend) — fetched first, kept for model training. Screen
and mouse/keyboard traces explain the user's path but never execute the
work reliably, so a creative demonstration is refused as a replayable
skill by construction.
"""

from __future__ import annotations

from oolu.skills.creative import (
    creative_app,
    is_creative_app,
    plan_creative_capture,
    refuse_replay_reason,
    source_extensions,
)
from oolu.skills.models import ActionEvent, Demonstration, ExecutionStatus


def test_creative_apps_are_recognized_by_name():
    assert creative_app("Adobe Photoshop 2026") == "photoshop"
    assert creative_app("SOLIDWORKS Premium") == "solidworks"
    assert creative_app("blender 4.2") == "blender"
    assert creative_app("Microsoft Excel") is None
    assert not is_creative_app("cli")
    assert ".sldprt" in source_extensions("SolidWorks")


def test_capture_plan_prioritizes_source_files_over_the_trace():
    capture = plan_creative_capture(
        "Adobe Photoshop",
        files=["poster.psd", "export.png", "brief.txt"],
        trace=["screen-0001.png", "input-events.jsonl"],
    )
    # The source file is the training payload — fetched FIRST.
    assert capture.source_files == ["poster.psd"]
    assert capture.other_files == ["export.png", "brief.txt"]
    # Screenshots and input events ride along as ADVISORY path context.
    assert capture.advisory_trace == ["screen-0001.png", "input-events.jsonl"]
    # No flag anywhere can promote a pixel trace into execution.
    assert capture.replayable is False
    assert "replayable" not in capture.model_fields_set


def test_creative_demonstrations_never_compile_into_replayable_skills(tmp_path):
    from oolu.skills.learner import SkillLearner
    from oolu.skills.registry import SkillRegistry

    registry = SkillRegistry(tmp_path / "skills.db")
    try:
        learner = SkillLearner(registry, scrub_pii=False)
        demo = Demonstration(
            intent="design the launch poster",
            actions=[
                ActionEvent(
                    correlation_id="c1", adapter="browser", operation="click"
                )
            ],
            outcome=ExecutionStatus.SUCCEEDED,
            application="Adobe Photoshop 2026",
        )
        learned = learner.learn(
            demo, name="Launch Poster", description="design the launch poster"
        )
        assert learned.status == "creative_source_needed"
        assert "SOURCE FILES" in learned.reason
        assert "never execute the work reliably" in learned.reason
        assert learned.registered is None
        assert registry.list(limit=5) == []  # nothing entered the registry

        # Ordinary applications learn exactly as before.
        plain = demo.model_copy(update={"application": "terminal"})
        assert refuse_replay_reason("terminal") is None
        result = learner.learn(
            plain,
            name="List Things",
            description="list",
            adapter="browser",
            verify=False,
            register_unverified=True,
        )
        assert result.status != "creative_source_needed"
    finally:
        registry.close()


def test_desktop_hands_include_the_local_cli_unless_disabled(tmp_path):
    from oolu.assembly import build_desktop_hands

    hands = build_desktop_hands(data_dir=tmp_path, environ={})
    # The desktop engine commands the LOCAL DEVICE: HTTP plus the
    # discovered command-line tools, workspace-confined.
    assert "http" in hands
    assert "cli" in hands
    assert (tmp_path / "workspace").is_dir()

    off = build_desktop_hands(
        data_dir=tmp_path, environ={"OOLU_CLI_TOOLS": "off"}
    )
    assert "cli" not in off and "http" in off
