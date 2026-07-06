"""Shared browser-test plumbing: a Chromium launcher with the bundled
fallback, and the smallest HTTP/1.1 server that can host an ASGI app for
a real browser (one request per connection; enough for fetch())."""

from __future__ import annotations

import asyncio
import http.client
import threading
from pathlib import Path

import pytest

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
    """Hosts one ASGI app on an ephemeral loopback port, thread-backed."""

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
