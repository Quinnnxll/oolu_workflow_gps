"""Browser-level end-to-end tests: a real Chromium drives the real shell.

The loopback app is served over an in-test ASGI HTTP server (no external
server dependency) and Playwright walks the front-end the way a user
would: assemble a marketplace goal, watch the budget verdict, confirm the
run, onboard a payout account, check health. Skipped cleanly wherever the
``browser`` extra (playwright) or a Chromium executable is unavailable —
everything the pages call is also covered by the API-level suites.
"""

from __future__ import annotations

import asyncio
import http.client
import threading
from pathlib import Path

import pytest

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="the 'browser' extra is not installed"
)

from test_contract_run import _CliExecutor  # noqa: E402
from test_gateway_market import _build  # noqa: E402
from test_market_assemble import _seed_market  # noqa: E402

from workflow_gps.billing import (  # noqa: E402
    EarningsLedger,
    FakePayoutAdapter,
    KycStatus,
    PayoutStore,
)
from workflow_gps.desktop import DesktopService  # noqa: E402
from workflow_gps.desktop.loopback import DesktopLoopbackApp  # noqa: E402
from workflow_gps.orchestrator import DagRouteRunner  # noqa: E402

_FALLBACK_CHROMIUM = Path("/opt/pw-browsers/chromium")


def _launch(p):
    """Prefer the bundled browser; fall back to the host-installed one."""
    try:
        return p.chromium.launch()
    except Exception:
        if _FALLBACK_CHROMIUM.exists():
            return p.chromium.launch(executable_path=str(_FALLBACK_CHROMIUM))
        pytest.skip("no usable Chromium for browser tests")


class _AsgiHttpServer:
    """The smallest HTTP/1.1 server that can host the loopback for a browser.

    One request per connection (Connection: close); enough for fetch()."""

    def __init__(self, app):
        self._app = app
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self.port: int | None = None

    def __enter__(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self._ready.wait(5), "server failed to start"
        return self

    def __exit__(self, *exc):
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        server = self._loop.run_until_complete(
            asyncio.start_server(self._handle, "127.0.0.1", 0)
        )
        self.port = server.sockets[0].getsockname()[1]
        self._ready.set()
        self._loop.run_forever()

    async def _handle(self, reader, writer):
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            method, target, _ = request_line.decode("latin1").split(" ", 2)
            headers = []
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                name, _, value = line.decode("latin1").partition(":")
                headers.append((name.strip().lower().encode(), value.strip().encode()))
            length = int(dict(headers).get(b"content-length", b"0"))
            body = await reader.readexactly(length) if length else b""
            path, _, query = target.partition("?")

            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}

            response = {"status": 500, "headers": [], "chunks": []}

            async def send(message):
                if message["type"] == "http.response.start":
                    response["status"] = message["status"]
                    response["headers"] = list(message.get("headers", []))
                else:
                    response["chunks"].append(message.get("body", b""))

            await self._app(
                {
                    "type": "http",
                    "method": method,
                    "path": path,
                    "query_string": query.encode("latin1"),
                    "headers": headers,
                },
                receive,
                send,
            )
            payload = b"".join(response["chunks"])
            reason = http.client.responses.get(response["status"], "OK")
            head = f"HTTP/1.1 {response['status']} {reason}\r\n"
            for name, value in response["headers"]:
                head += f"{name.decode('latin1')}: {value.decode('latin1')}\r\n"
            head += f"content-length: {len(payload)}\r\nconnection: close\r\n\r\n"
            writer.write(head.encode("latin1") + payload)
            await writer.drain()
        except Exception:
            pass  # a torn connection must not kill the server
        finally:
            writer.close()


@pytest.fixture
def shell(tmp_path):
    """A market-enabled shell with earnings + payout onboarding, served."""
    app, conn, ident, registry, metering, attribution, audit = _build(tmp_path)
    _seed_market(app, ident, registry)
    service = DesktopService(
        app._durable,
        market=app._market,
        price_book=app._price_book,
        contract_runner=DagRouteRunner({"cli": _CliExecutor()}),
        attribution=attribution,
        earnings_ledger=EarningsLedger(conn),
        payout_store=PayoutStore(conn),
        payout_adapter=FakePayoutAdapter(kyc=KycStatus.PENDING),
        noder_principal="local-noder",
    )
    with _AsgiHttpServer(DesktopLoopbackApp(service)) as server:
        yield server
    conn.close()


def test_a_user_tours_the_shell_in_a_real_browser(shell):
    """Assemble -> preview -> confirm; onboard a payout account; health."""
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page()
        page.set_default_timeout(10_000)
        page.goto(shell.url + "/#/assemble")
        expect(page).to_have_title("Workflow-GPS Shell")

        # Assemble the seeded marketplace chain and preview it.
        page.fill("#f-want", "tidy")
        page.fill("#f-q", "invoice")
        page.get_by_role("button", name="Preview").click()
        expect(page.locator(".badge", has_text="complete")).to_be_visible()
        expect(page.locator("td", has_text="raw exporter")).to_be_visible()
        expect(page.locator("td", has_text="invoice cleaner")).to_be_visible()
        expect(page.locator(".badge", has_text="within budget")).to_be_visible()

        # Confirm: the run executes through the shared money path.
        page.get_by_role("button", name="Confirm & run").click()
        expect(page.locator(".badge", has_text="succeeded")).to_be_visible()
        expect(page.locator("text=noders:")).to_be_visible()

        # Earnings: onboard a payout account; KYC pending blocks payouts.
        page.goto(shell.url + "/#/earnings")
        expect(page.locator("text=no payout account yet")).to_be_visible()
        page.get_by_role("button", name="Onboard").click()
        expect(page.locator(".badge", has_text="pending")).to_be_visible()
        expect(
            page.locator(".badge", has_text="payouts blocked until KYC verifies")
        ).to_be_visible()
        expect(page.locator("text=lifetime paid")).to_be_visible()

        # Health renders the isolation labels.
        page.goto(shell.url + "/#/health")
        expect(page.locator("text=network policy")).to_be_visible()
        browser.close()


def test_a_task_submits_and_its_screen_survives_without_websockets(shell):
    """The task detail screen works even where the transport has no
    websocket support — live streaming degrades, nothing breaks."""
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page()
        page.set_default_timeout(10_000)
        page.goto(shell.url + "/#/tasks")
        page.fill("input[placeholder='what should happen?']", "tour the shell")
        page.get_by_role("button", name="Submit").click()
        # Routed straight to the task detail screen.
        expect(page.locator("strong", has_text="tour the shell")).to_be_visible()
        expect(page.locator(".badge").first).to_be_visible()  # phase badge
        page.goto(shell.url + "/#/tasks")
        expect(page.locator("td", has_text="tour the shell")).to_be_visible()
        browser.close()
