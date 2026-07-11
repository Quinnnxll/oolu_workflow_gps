"""The whole shell, for real: one Chromium smoke over the built app.

Everything else in the suite tests pieces behind fakes — and fakes can
lie (the upload path once "passed" while shipping hollow files). This is
the anti-lie: the COMMITTED shell bundle, served by the real GatewayASGI
over the real host runtime (model-less, temp data dir), driven by a real
browser through the flows a person actually walks:

1. boot authenticated via the desktop's own #auth token bootstrap;
2. talk to OoLu and get the deterministic greeting back;
3. upload a REAL file from disk through the + menu — its true words
   must arrive in the drawer, visibly;
4. download it back — the bytes that return must equal the bytes that
   went in. Device → drawer → device, closed loop.

Skips honestly where no Chromium/playwright exists; CI installs both so
this runs on every push.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="playwright not installed")

from browser_harness import _AsgiHttpServer, _launch  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402

from oolu.assembly import build_host_runtime  # noqa: E402

SECRET = "a-thirty-two-character-plus-signing-secret"
PASSWORD = "a-solid-password-1"
WORDS = "the drawer carries REAL bytes end to end"


def test_the_shell_carries_a_conversation_and_a_file_end_to_end(tmp_path):
    runtime = build_host_runtime(
        data_dir=tmp_path / "data", secret=SECRET, frontend="shell"
    )
    try:
        runtime.accounts.create_user("quinn", PASSWORD, tenant="main")
        token = runtime.accounts.login("quinn", PASSWORD).token
        source = tmp_path / "quarterly-notes.txt"
        source.write_text(WORDS)

        with _AsgiHttpServer(runtime.asgi) as server, sync_playwright() as p:
            browser = _launch(p)
            page = browser.new_page(viewport={"width": 1280, "height": 960})
            # The desktop's own bootstrap: the token rides the URL hash
            # once, is captured into sessionStorage, and never lingers.
            page.goto(f"{server.url}/#auth={token}")

            # 1. The conversation answers — the deterministic rule floor,
            # no model configured anywhere.
            page.get_by_placeholder("Message OoLu…").fill("hello")
            page.get_by_role("button", name="Send").click()
            expect(page.get_by_text(re.compile("ready to roll"))).to_be_visible()

            # 2. Upload a REAL file from disk through the + menu.
            page.locator("aside .convo-name", has_text="Files").first.click()
            page.get_by_role("button", name="Add").click()
            with page.expect_file_chooser() as chooser_info:
                page.get_by_role(
                    "button", name="Upload from device"
                ).click()
            chooser_info.value.set_files(str(source))
            expect(page.get_by_text(re.compile("uploaded 1 file"))).to_be_visible()

            # 3. Its true words arrived — visible on the reading page.
            page.get_by_text("quarterly-notes.txt").click()
            expect(page.get_by_text(WORDS)).to_be_visible()

            # 4. And they come back OUT byte-for-byte: the download door.
            with page.expect_download() as download_info:
                page.get_by_role("button", name="download").click()
            downloaded = download_info.value.path()
            assert Path(downloaded).read_text() == WORDS

            browser.close()
    finally:
        runtime.close()
