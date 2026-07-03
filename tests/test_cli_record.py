from __future__ import annotations

import io
import json

import pytest

from workflow_gps.cli import _cmd_record, build_parser
from workflow_gps.skills.models import ActionEvent, Demonstration, ExecutionStatus
from workflow_gps.skills.recorder import Recording, RecordingMetrics
from workflow_gps.skills.registry import SkillRegistry


def _parse(argv):
    return build_parser().parse_args(argv)


def _fake_session_factory(actions, *, succeeded=True):
    def session(*, intent, url, headless, audit_db):
        demo = Demonstration(
            intent=intent,
            actions=[
                ActionEvent(
                    correlation_id="s",
                    adapter="browser",
                    operation=op,
                    parameters=params,
                )
                for op, params in actions
            ],
            outcome=ExecutionStatus.SUCCEEDED,
            application="web",
        )
        return Recording(
            demonstration=demo,
            metrics=RecordingMetrics(
                duration_s=2.0,
                action_count=len(actions),
                backend_event_count=0,
                error_count=0,
                succeeded=succeeded,
            ),
        )

    return session


def test_record_learns_a_browser_skill(tmp_path):
    args = _parse(
        [
            "record",
            "search for flights",
            "--url",
            "https://example.com",
            "--name",
            "Flight Search",
            "--registry",
            str(tmp_path / "reg.db"),
        ]
    )
    out = io.StringIO()
    session = _fake_session_factory(
        [("click", {"selector": "#go"}), ("fill", {"selector": "#q", "value": "LIS"})]
    )
    rc = _cmd_record(args, out, session=session)

    assert rc == 0
    assert "registered" in out.getvalue()
    reg = SkillRegistry(tmp_path / "reg.db")
    try:
        skill = reg.get("learned.flight.search")
        assert skill is not None
        assert [a.operation for a in skill.skill.actions] == ["click", "fill"]
    finally:
        reg.close()


def test_record_json_output(tmp_path):
    args = _parse(
        [
            "record",
            "do it",
            "--url",
            "https://x.test",
            "--registry",
            str(tmp_path / "r.db"),
            "--json",
        ]
    )
    out = io.StringIO()
    rc = _cmd_record(
        args, out, session=_fake_session_factory([("click", {"selector": "#a"})])
    )
    assert rc == 0
    payload = json.loads(out.getvalue())
    assert payload["status"] == "registered"
    assert payload["actions"] == 1
    assert payload["skill_id"] == "learned.do.it"


def test_record_requires_url():
    with pytest.raises(SystemExit):
        _parse(["record", "no url given"])
