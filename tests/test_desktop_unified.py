"""Migration step 1: the desktop shell over the unified gateway surface.

`wfgps desktop --unified` serves the SAME multi-tenant gateway `wfgps
host` does — same routes, same front-end, same identity semantics — bound
to loopback with a local user auto-provisioned and signed in. The one
property that must survive the migration is zero friction: the browser
opens straight into the shell (the `#auth=<token>` bootstrap moves the
token into sessionStorage and out of the URL), and the loopback bind —
not a password — stays the trust boundary on the user's own machine.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from test_browser_e2e import _AsgiHttpServer, _launch

from workflow_gps import cli
from workflow_gps.assembly import build_host_runtime

ROOT = Path(__file__).resolve().parent.parent
SECRET = "a-thirty-two-character-plus-signing-secret"


def _run_unified(monkeypatch, tmp_path, argv=()):
    import uvicorn

    served = {}

    def fake_run(app, **kwargs):
        served["app"] = app
        served.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    out = io.StringIO()
    code = cli.main(
        ["desktop", "--unified", "--db", str(tmp_path / "d" / "desktop.db"), *argv],
        out=out,
    )
    return code, out.getvalue(), served


def test_unified_serves_the_gateway_signed_in_on_loopback(monkeypatch, tmp_path):
    from workflow_gps.gateway.asgi import GatewayASGI

    code, banner, served = _run_unified(monkeypatch, tmp_path)
    assert code == 0
    assert isinstance(served["app"], GatewayASGI)
    assert served["host"] == "127.0.0.1"  # the surface changed, the bind did not
    assert "#auth=" in banner  # the auto-auth link: no sign-in screen locally
    assert "signed in automatically as 'local'" in banner


def test_unified_still_refuses_non_loopback_hosts(capsys):
    code = cli.main(["desktop", "--unified", "--host", "0.0.0.0"])
    assert code == 2
    assert "loopback" in capsys.readouterr().err


def test_a_second_launch_rotates_the_ephemeral_password(monkeypatch, tmp_path):
    """Restart safety: 'local' exists from launch one with a discarded
    password; launch two must rotate it and still sign in."""
    first_code, first_banner, _ = _run_unified(monkeypatch, tmp_path)
    second_code, second_banner, _ = _run_unified(monkeypatch, tmp_path)
    assert first_code == second_code == 0
    first_token = re.search(r"#auth=(\S+)", first_banner).group(1)
    second_token = re.search(r"#auth=(\S+)", second_banner).group(1)
    assert first_token != second_token  # fresh secret, fresh token, every start


def test_the_browser_opens_straight_into_the_shell(tmp_path):
    """The #auth bootstrap end to end: no sign-in page, token out of the URL."""
    runtime = build_host_runtime(data_dir=tmp_path / "host", secret=SECRET)
    runtime.accounts.bootstrap(tenant="local", username="local", password="first-pass")
    token = runtime.accounts.login("local", "first-pass").token
    with _AsgiHttpServer(runtime.asgi) as server:
        with sync_playwright() as p:
            browser = _launch(p)
            page = browser.new_page()
            page.goto(server.url + "/#auth=" + token)
            # Straight into the shell — the sign-in screen never appears.
            expect(page.get_by_text("No runs yet")).to_be_visible()
            expect(page.get_by_text("local ·")).to_be_visible()
            # The token has left the URL for sessionStorage.
            assert "#auth=" not in page.url
            # And survives navigation within the shell.
            page.get_by_role("link", name="Health").click()
            expect(page.get_by_text("requests")).to_be_visible()
            browser.close()
    runtime.close()


def test_workflows_run_on_current_action_majors():
    """The Node 20 deprecation, kept fixed: no workflow pins an action
    major that targets the deprecated runtime."""
    stale = ("checkout@v4", "setup-python@v5", "upload-artifact@v4", "setup-node@v4")
    for workflow in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        text = workflow.read_text()
        for pin in stale:
            assert f"actions/{pin}" not in text, (workflow.name, pin)
    windows = (ROOT / ".github" / "workflows" / "desktop-windows.yml").read_text()
    assert 'node-version: "22"' in windows  # Node 20 itself is end-of-life
