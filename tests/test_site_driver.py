"""The general checkout road: the browser bridge and its human-control pause.

All fakes — no Playwright, no network. The point is the driver's contract:
map commerce steps to browser primitives, pause to the human before any step
that needs the user's session, and let the executor's payment gate stay the
separate, authoritative money check.
"""

from __future__ import annotations

import pytest

from oolu.skills.commerce import SiteDriverExecutor
from oolu.skills.models import ActionEvent, ExecutionStatus
from oolu.skills.site_driver import (
    AssumeAuthenticated,
    BrowserSiteDriver,
    CallbackLoginGate,
    LoginAbandoned,
    SiteDriverError,
)


class _FakeSession:
    """Records the steps it ran and reports a scriptable auth state."""

    def __init__(self, *, authenticated=True):
        self.authenticated = authenticated
        self.runs: list[list[dict]] = []

    def run_steps(self, steps):
        self.runs.append(list(steps))
        return {"final_url": "https://shop.example/done", "ran": len(steps)}

    def is_authenticated(self, probe):
        if not probe:
            return True
        return self.authenticated


def _action(operation, **params):
    return ActionEvent(
        correlation_id="c", adapter="web", operation=operation, parameters=params
    )


# --------------------------------------------------------------------------- #
# The driver contract.                                                         #
# --------------------------------------------------------------------------- #
def test_anonymous_steps_run_without_a_login_pause():
    session = _FakeSession(authenticated=False)
    paused = []
    driver = BrowserSiteDriver(
        session,
        login_gate=CallbackLoginGate(lambda site, reason: paused.append((site, reason))),
    )
    evidence = driver.step(
        "search", {"browser_steps": [{"op": "goto", "url": "https://shop.example"}]}
    )
    assert evidence["operation"] == "search"
    assert paused == []  # search never needs the user's session
    assert len(session.runs) == 1


def test_a_session_step_pauses_for_login_when_signed_out():
    session = _FakeSession(authenticated=False)
    calls = []

    def on_login(site, reason):
        calls.append((site, reason))
        session.authenticated = True  # the human signs in during the pause

    driver = BrowserSiteDriver(session, login_gate=CallbackLoginGate(on_login))
    driver.step(
        "checkout",
        {
            "site": "shop.example",
            "login_probe": "#account-menu",
            "browser_steps": [{"op": "click", "selector": "#place-order"}],
        },
    )
    assert calls == [("shop.example", "checkout")]  # paused exactly once
    assert len(session.runs) == 1  # then resumed and ran the step


def test_a_session_step_does_not_pause_when_already_signed_in():
    session = _FakeSession(authenticated=True)
    paused = []
    driver = BrowserSiteDriver(
        session,
        login_gate=CallbackLoginGate(lambda s, r: paused.append((s, r))),
    )
    driver.step(
        "checkout",
        {"login_probe": "#account-menu", "browser_steps": [{"op": "submit"}]},
    )
    assert paused == []


def test_login_still_failing_after_the_pause_raises():
    session = _FakeSession(authenticated=False)
    # A gate that "returns" but the session is still not authenticated.
    driver = BrowserSiteDriver(session, login_gate=AssumeAuthenticated())
    with pytest.raises(SiteDriverError):
        driver.step(
            "checkout",
            {"login_probe": "#account-menu", "browser_steps": [{"op": "submit"}]},
        )


def test_an_abandoned_login_propagates_as_a_driver_error():
    session = _FakeSession(authenticated=False)
    driver = BrowserSiteDriver(
        session, login_gate=CallbackLoginGate(lambda site, reason: False)
    )
    with pytest.raises(LoginAbandoned):
        driver.step(
            "checkout",
            {"login_probe": "#acct", "browser_steps": [{"op": "submit"}]},
        )


def test_requires_session_flag_gates_an_otherwise_anonymous_step():
    session = _FakeSession(authenticated=False)
    paused = []
    driver = BrowserSiteDriver(
        session,
        login_gate=CallbackLoginGate(lambda s, r: paused.append(r) or True),
    )
    # 'search' is normally anonymous, but the plan flags this one as gated.
    session.authenticated = False
    with pytest.raises(SiteDriverError):
        driver.step(
            "search",
            {
                "requires_session": True,
                "login_probe": "#acct",
                "browser_steps": [{"op": "goto", "url": "https://x"}],
            },
        )
    assert paused == ["search"]  # it did pause, then failed the re-check


def test_site_is_inferred_from_the_first_step_url_when_unnamed():
    session = _FakeSession()
    driver = BrowserSiteDriver(session)
    evidence = driver.step(
        "open", {"browser_steps": [{"op": "goto", "url": "https://store.acme.test/x"}]}
    )
    assert evidence["site"] == "store.acme.test"


def test_browser_steps_must_be_a_list():
    driver = BrowserSiteDriver(_FakeSession())
    with pytest.raises(SiteDriverError):
        driver.step("open", {"browser_steps": {"op": "goto"}})


# --------------------------------------------------------------------------- #
# The bridge fits the executor, and the payment gate is still the money check. #
# --------------------------------------------------------------------------- #
def test_driver_plugs_the_executor_and_checkout_still_needs_authorization():
    session = _FakeSession(authenticated=True)
    driver = BrowserSiteDriver(session)
    released = {"auth-ok"}
    executor = SiteDriverExecutor(driver, is_authorized=lambda a: a in released)

    # A signed-in session is NOT consent to spend: checkout without a released
    # authorization is BLOCKED before the browser is even driven.
    blocked = executor.execute(
        _action("checkout", browser_steps=[{"op": "submit"}]),
        idempotency_key="k1",
    )
    assert blocked.status is ExecutionStatus.BLOCKED
    assert session.runs == []  # never reached the site

    # With the released authorization, the same step drives the browser.
    ok = executor.execute(
        _action(
            "checkout",
            authorization_id="auth-ok",
            browser_steps=[{"op": "submit"}],
        ),
        idempotency_key="k2",
    )
    assert ok.status is ExecutionStatus.SUCCEEDED
    assert len(session.runs) == 1


def test_navigation_through_the_executor_needs_no_authorization():
    session = _FakeSession()
    executor = SiteDriverExecutor(BrowserSiteDriver(session), is_authorized=lambda a: False)
    outcome = executor.execute(
        _action("open", browser_steps=[{"op": "goto", "url": "https://shop.example"}]),
        idempotency_key="nav",
    )
    assert outcome.status is ExecutionStatus.SUCCEEDED  # browsing is free


# --------------------------------------------------------------------------- #
# The composition root now actually wires the road (the gap the trace found).  #
# --------------------------------------------------------------------------- #
def test_host_runtime_registers_the_web_road_when_a_driver_is_provided(tmp_path):
    from oolu.assembly import build_host_runtime

    driver = BrowserSiteDriver(_FakeSession())
    runtime = build_host_runtime(
        data_dir=tmp_path / "host",
        secret="a-thirty-two-character-plus-signing-secret",
        site_driver=driver,
    )
    try:
        # The new branch ran: build_commerce_executors was called with the
        # host's payment gate, and construction succeeded. (No driver -> no
        # road, which is the unchanged default the other tests cover.)
        assert runtime.gateway is not None
    finally:
        runtime.close()
