from __future__ import annotations

import pytest

from oolu.skills.browser_observer import BrowserObserver


def test_on_record_builds_action_events():
    obs = BrowserObserver()
    obs._on_record(None, {"op": "click", "selector": "#go"})
    obs._on_record(None, {"op": "fill", "selector": "#name", "value": "Ada"})
    obs._on_record(None, {"op": "nonsense", "selector": "#x"})  # ignored
    obs._on_record(None, "not-a-dict")  # ignored

    events = obs.observe()
    assert [e.operation for e in events] == ["click", "fill"]
    assert all(e.adapter == "browser" for e in events)
    fill = events[1]
    assert fill.parameters == {"selector": "#name", "value": "Ada"}
    # One session id ties the demonstration together.
    assert len({e.correlation_id for e in events}) == 1


def test_clear_resets_the_buffer():
    obs = BrowserObserver()
    obs._on_record(None, {"op": "submit", "selector": "form"})
    assert len(obs.observe()) == 1
    obs.clear()
    assert obs.observe() == ()


# --------------------------------------------------------------------------- #
# Real-Chromium capture.                                                      #
# --------------------------------------------------------------------------- #
pytest.importorskip("playwright")
from oolu.skills.browser import discover_chromium  # noqa: E402

_browser = pytest.mark.skipif(
    discover_chromium() is None, reason="no provisioned chromium"
)


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, executable_path=discover_chromium())
        yield b
        b.close()


@pytest.fixture
def page(browser):
    pg = browser.new_page()
    yield pg
    pg.close()


@_browser
def test_captures_real_interactions(page):
    obs = BrowserObserver()
    obs.attach(page)
    page.set_content(
        "<button id=go>go</button>"
        "<input id=name>"
        "<input id=pw type=password>"
        "<select id=choice><option value=a>a</option><option value=b>b</option></select>"
    )
    page.click("#go")
    page.fill("#name", "alice@example.com")
    page.locator("#name").blur()  # commit the field -> change event
    page.fill("#pw", "hunter2")
    page.locator("#pw").blur()
    page.select_option("#choice", "b")
    page.wait_for_timeout(100)  # let the exposed binding flush

    events = {e.operation: e for e in obs.observe()}
    assert "click" in events and events["click"].parameters["selector"] == "#go"
    assert events["fill"].parameters["selector"] in ("#name", "#pw")

    fills = [e for e in obs.observe() if e.operation == "fill"]
    name_fill = next(e for e in fills if e.parameters["selector"] == "#name")
    assert name_fill.parameters["value"] == "alice@example.com"
    pw_fill = next(e for e in fills if e.parameters["selector"] == "#pw")
    assert pw_fill.parameters["value"] == "<MASKED>"  # password never leaves the page

    select = next(e for e in obs.observe() if e.operation == "select_option")
    assert select.parameters == {"selector": "#choice", "value": "b"}


@_browser
def test_recorded_browser_demo_feeds_the_learner(page, tmp_path):
    from oolu.skills.learner import SkillLearner, scrub_demonstration
    from oolu.skills.recorder import DemonstrationRecorder
    from oolu.skills.registry import SkillRegistry

    obs = BrowserObserver()
    obs.attach(page)
    recorder = DemonstrationRecorder(obs)
    recorder.start()
    page.set_content("<button id=go>go</button><input id=q>")
    page.click("#go")
    page.fill("#q", "contact bob@example.com")
    page.locator("#q").blur()
    page.wait_for_timeout(100)
    recording = recorder.stop(intent="search the thing", application="web")

    # The demonstration carries the captured browser actions; scrubbing masks PII.
    ops = [a.operation for a in recording.demonstration.actions]
    assert "click" in ops and "fill" in ops
    scrubbed = scrub_demonstration(recording.demonstration)
    fill = next(a for a in scrubbed.actions if a.operation == "fill")
    assert "bob@example.com" not in fill.parameters["value"]

    reg = SkillRegistry(tmp_path / "reg.db")
    try:
        learned = SkillLearner(reg, scrub_pii=True).learn(
            recording.demonstration,
            name="Web Search",
            description="a recorded web search",
            adapter="browser",
            mode="actions",  # a browser demo has no file artifacts
            verify=False,  # no sandbox browser wired in this test
        )
        assert learned.status == "registered"
        assert learned.registered.skill.actions  # captured actions survived
    finally:
        reg.close()
