from __future__ import annotations

import pytest

pytest.importorskip("playwright")

from workflow_gps.skills.browser import (  # noqa: E402
    BrowserActionExecutor,
    BrowserPolicy,
    _allowed_hosts_for,
    _host,
    discover_chromium,
)
from workflow_gps.skills.models import ActionEvent, ExecutionStatus  # noqa: E402

pytestmark = pytest.mark.skipif(
    discover_chromium() is None, reason="no provisioned chromium"
)


def _page(tmp_path, name, html):
    path = tmp_path / name
    path.write_text(html)
    return path.as_uri()


def _action(steps, **params):
    return ActionEvent(
        correlation_id="s1",
        adapter="browser",
        operation="run",
        parameters={"steps": steps, **params},
    )


@pytest.fixture(scope="module")
def executor():
    ex = BrowserActionExecutor(policy=BrowserPolicy(headless=True))
    yield ex
    ex.close()


def test_read_text_and_rows(executor, tmp_path):
    url = _page(
        tmp_path,
        "t.html",
        "<h1 id=h>Report</h1><table id=g><tr><td>a</td><td>b</td></tr>"
        "<tr><td>c</td><td>d</td></tr></table>",
    )
    outcome = executor.execute(
        _action(
            [
                {"op": "goto", "url": url},
                {"op": "read_text", "selector": "#h", "name": "title"},
                {"op": "read_rows", "selector": "#g", "name": "rows"},
            ]
        ),
        idempotency_key="k1",
    )
    assert outcome.status is ExecutionStatus.SUCCEEDED
    assert outcome.evidence["extracted"]["title"] == "Report"
    assert outcome.evidence["extracted"]["rows"] == [["a", "b"], ["c", "d"]]


def test_click_then_read(executor, tmp_path):
    url = _page(
        tmp_path,
        "c.html",
        "<button id=b onclick=\"document.getElementById('o').innerText='clicked'\">go"
        "</button><div id=o></div>",
    )
    outcome = executor.execute(
        _action(
            [
                {"op": "goto", "url": url},
                {"op": "click", "selector": "#b"},
                {"op": "read_text", "selector": "#o", "name": "out"},
            ]
        ),
        idempotency_key="k2",
    )
    assert outcome.evidence["extracted"]["out"] == "clicked"


def test_fill_submit_and_param_substitution(executor, tmp_path):
    url = _page(
        tmp_path,
        "f.html",
        "<input id=i><button id=s onclick=\"document.getElementById('r').innerText="
        "document.getElementById('i').value\">s</button><div id=r></div>",
    )
    outcome = executor.execute(
        _action(
            [
                {"op": "goto", "url": "{{url}}"},
                {"op": "fill", "selector": "#i", "value": "{{value}}"},
                {"op": "click", "selector": "#s"},
                {"op": "read_text", "selector": "#r", "name": "echo"},
            ],
            url=url,
            value="hello world",
        ),
        idempotency_key="k3",
    )
    assert outcome.evidence["extracted"]["echo"] == "hello world"


def test_unsupported_step_fails(executor, tmp_path):
    url = _page(tmp_path, "u.html", "<p>x</p>")
    outcome = executor.execute(
        _action([{"op": "goto", "url": url}, {"op": "teleport"}]),
        idempotency_key="k4",
    )
    assert outcome.status is ExecutionStatus.FAILED
    assert "teleport" in outcome.error


def test_non_run_action_is_blocked(executor):
    action = ActionEvent(correlation_id="s", adapter="browser", operation="scrape")
    outcome = executor.execute(action, idempotency_key="k5")
    assert outcome.status is ExecutionStatus.BLOCKED


def test_idempotent_replay(executor, tmp_path):
    url = _page(tmp_path, "i.html", "<h1 id=h>once</h1>")
    a = _action([{"op": "goto", "url": url}, {"op": "read_text", "selector": "#h"}])
    first = executor.execute(a, idempotency_key="k6")
    second = executor.execute(a, idempotency_key="k6")
    assert first is second


def test_server_execute_drives_the_browser(executor, tmp_path):
    from workflow_gps.skills.pack import load_skill_pack
    from workflow_gps.skills.registry import SkillRegistry
    from workflow_gps.skills.server import SkillsServer

    url = _page(tmp_path, "s.html", "<h1 id=h>served</h1>")
    registry = SkillRegistry(tmp_path / "reg.db")
    load_skill_pack(
        registry,
        {
            "skills": [
                {
                    "skill_id": "web.read_title",
                    "name": "Read Title",
                    "summary": "read the page title",
                    "adapter": "browser",
                    "parameters": [{"name": "url", "value_type": "string"}],
                    "actions": [
                        {
                            "operation": "run",
                            "parameters": {
                                "steps": [
                                    {"op": "goto", "url": "{{url}}"},
                                    {"op": "read_text", "selector": "#h", "name": "t"},
                                ]
                            },
                        }
                    ],
                }
            ]
        },
    )
    server = SkillsServer(registry, executors={"browser": executor})
    try:
        status, payload = server._execute(
            {"skill_id": "web.read_title", "parameters": {"url": url}}
        )
        assert status == 200
        assert payload["outcomes"][0]["evidence"]["extracted"]["t"] == "served"
    finally:
        registry.close()


def test_host_helpers():
    assert _host("https://example.com/x") == "example.com"
    assert _host("file:///tmp/x.html") is None
    allowed = _allowed_hosts_for(
        frozenset({"cdn.example.com"}),
        {"url": "https://app.example.com"},
        [{"op": "goto", "url": "https://api.example.com/x"}],
    )
    assert allowed == frozenset(
        {"cdn.example.com", "app.example.com", "api.example.com"}
    )
