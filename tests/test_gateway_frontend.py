"""The multi-user gateway grows a face: sign-in page + shell screens.

The gateway front-end (served by ``GatewayASGI`` at ``GET /``) is the
desktop shell's philosophy against the authenticated surface: sign in
once (`POST /v1/auth/login` → the bearer token lives in sessionStorage
for that tab), every fetch carries the token, a 401 signs the tab out,
and screens degrade honestly (404 → "not enabled on this host", 403 →
"no authority"). XSS-safe by construction: DOM building, no HTML
templates. A real Chromium drives the real host runtime end to end.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright
from test_browser_e2e import _AsgiHttpServer, _launch

from workflow_gps.assembly import build_host_runtime

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "src" / "workflow_gps" / "gateway" / "frontend" / "index.html"
SECRET = "a-thirty-two-character-plus-signing-secret"


def test_frontend_script_is_valid_javascript(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available to syntax-check the UI script")
    script = re.search(r"<script>\n(.*?)</script>", INDEX.read_text(), re.S).group(1)
    path = tmp_path / "gateway-ui.js"
    path.write_text(script)
    check = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
    assert check.returncode == 0, check.stderr


def test_every_path_the_frontend_calls_is_a_real_route():
    html = INDEX.read_text()
    app_source = (ROOT / "src" / "workflow_gps" / "gateway" / "app.py").read_text()
    called = {
        "/v1/auth/login",
        "/v1/auth/users",
        "/v1/runs",
        "/v1/market/assemble",
        "/v1/runs/contract",
        "/v1/runs/contract/holds",
        "/v1/earnings",
        "/v1/earnings/entries",
        "/v1/metrics",
    }
    for path in called:
        assert path in html, f"frontend no longer calls {path}"
        assert f'"{path}"' in app_source, f"{path} is not a registered route"
    # Path-building fragments for parameterized routes.
    assert '"/v1/runs/contract/holds/"' in html  # + pending_id
    assert '"/disabled"' in html  # /v1/auth/users/{name}/disabled
    assert '"/v1/runs/"' in html  # + run_id (detail + audit)
    # The task flow (step 2 of the unified migration): every pause kind
    # has an actionable panel, plus cancel and the skills search.
    for fragment in (
        "/questions",
        "/answers",
        "/route",
        "/confirmation",
        "/approvals",
        "/incidents",
        "/cancel",
    ):
        assert f'"{fragment}"' in html, f"run detail no longer wires {fragment}"
    assert '"/v1/listings?q="' in html  # the skills screen searches listings


def test_the_shell_stays_xss_safe_by_construction():
    html = INDEX.read_text()
    assert "innerHTML" not in html
    assert "sessionStorage" in html and "localStorage" not in html


# --------------------------------------------------------------------------- #
# A real browser against the real multi-user host.                             #
# --------------------------------------------------------------------------- #
@pytest.fixture()
def host_server(tmp_path):
    runtime = build_host_runtime(data_dir=tmp_path / "host", secret=SECRET)
    runtime.accounts.bootstrap(tenant="main", username="admin", password="first-pass")
    with _AsgiHttpServer(runtime.asgi) as server:
        yield server
    runtime.close()


def _sign_in(page, base, username, password):
    page.goto(base + "/")
    page.get_by_label("Username").fill(username)
    page.get_by_label("Password").fill(password)
    page.get_by_role("button", name="Sign in").click()


def test_a_user_signs_in_works_and_signs_out(host_server):
    with sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page()

        # Locked by default; wrong passwords say only "invalid credentials".
        page.goto(host_server.url + "/")
        expect(page.get_by_text("Sign in to this host.")).to_be_visible()
        _sign_in(page, host_server.url, "admin", "wrong-password")
        expect(page.get_by_text("invalid credentials")).to_be_visible()

        _sign_in(page, host_server.url, "admin", "first-pass")
        expect(page.get_by_text("No runs yet")).to_be_visible()
        expect(page.get_by_text("admin ·")).to_be_visible()

        # Health renders the live metrics table.
        page.get_by_role("link", name="Health").click()
        expect(page.get_by_text("requests")).to_be_visible()

        # Sign out drops the tab's token and locks the shell again.
        page.get_by_role("link", name="sign out").click()
        expect(page.get_by_text("Sign in to this host.")).to_be_visible()
        browser.close()


def test_an_admin_provisions_and_disables_users_from_the_browser(host_server):
    with sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page()
        _sign_in(page, host_server.url, "admin", "first-pass")

        page.get_by_role("link", name="Users").click()
        expect(page.get_by_text("add a user")).to_be_visible()
        page.get_by_label("Username").fill("bob")
        page.get_by_label("Password").fill("bobs-password")
        page.get_by_role("button", name="Create user").click()
        expect(page.get_by_role("cell", name="bob", exact=True)).to_be_visible()

        bob_row = page.get_by_role("row", name=re.compile("bob"))
        bob_row.get_by_role("button", name="Disable").click()
        expect(
            page.get_by_role("row", name=re.compile("bob")).get_by_text(
                "disabled", exact=True
            )
        ).to_be_visible()

        # And the disabled account cannot sign in — from a fresh tab.
        fresh = browser.new_page()
        _sign_in(fresh, host_server.url, "bob", "bobs-password")
        expect(fresh.get_by_text("invalid credentials")).to_be_visible()
        browser.close()


def test_members_see_screens_degrade_honestly(host_server):
    with sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page()
        _sign_in(page, host_server.url, "admin", "first-pass")
        page.get_by_role("link", name="Users").click()
        page.get_by_label("Username").fill("carol")
        page.get_by_label("Password").fill("carols-password")
        page.get_by_role("button", name="Create user").click()
        expect(page.get_by_role("cell", name="carol", exact=True)).to_be_visible()
        page.get_by_role("link", name="sign out").click()

        _sign_in(page, host_server.url, "carol", "carols-password")
        expect(page.get_by_text("No runs yet")).to_be_visible()
        # No stored users:manage authority: the screen says so, plainly.
        page.get_by_role("link", name="Users").click()
        expect(page.get_by_text("Your account does not have authority")).to_be_visible()
        # Billing is not wired on this host: honest, not a broken screen.
        page.get_by_role("link", name="Earnings").click()
        expect(page.get_by_text("not enabled on this host")).to_be_visible()
        browser.close()


def test_the_task_flow_pauses_are_actionable_in_the_browser(tmp_path):
    """Step 2 of the unified migration, end to end: a run that pauses for
    clarification and then route confirmation is driven to completion
    entirely from the browser — including the paste-a-token sign-in path
    an IdP-fronted host would use."""
    from test_http_gateway import _app, _clarify

    from workflow_gps.gateway.asgi import GatewayASGI

    gateway, conn, ident = _app(tmp_path, _clarify)
    # The fixture's token helper mints at the suite's frozen clock; the
    # ASGI server validates with the real one, so mint a live token.
    token = ident._signer.mint(subject="user-1", tenant_id="t1")
    with _AsgiHttpServer(GatewayASGI(gateway)) as server:
        with sync_playwright() as p:
            browser = _launch(p)
            page = browser.new_page()

            # No local accounts here: the bearer-token fallback signs in.
            page.goto(server.url + "/")
            page.get_by_text("or use a bearer token").click()
            page.get_by_label("Access token").fill(token)
            page.get_by_role("button", name="Use token").click()

            page.get_by_label("Start a workflow").fill("clarify me")
            page.get_by_role("button", name="Start", exact=True).click()

            # The run paused for clarification: the question is a form.
            expect(page.get_by_text("needs clarification")).to_be_visible()
            page.locator("#q-size").fill("large")
            page.get_by_role("button", name="Answer").click()

            # Next pause: route confirmation, with the estimated cost.
            expect(page.get_by_text("confirm the route")).to_be_visible()
            expect(page.get_by_text("estimated cost")).to_be_visible()
            page.get_by_role("button", name="Confirm", exact=True).click()

            # Confirmed: the run executes to completion.
            expect(page.get_by_text("completed", exact=True)).to_be_visible()
            browser.close()
    conn.close()
