from __future__ import annotations

import json
from typing import Any

from ..providers.base import HttpTransport
from .errors import IsolationViolation, LeaseError, WorkerError
from .worker import Worker

_JSON_HEADERS = {
    "Content-Type": "application/json",
    "X-Content-Type-Options": "nosniff",
}


class HttpWorkerTransport:
    def __init__(
        self,
        http: HttpTransport,
        *,
        base_url: str | None = None,
        worker_urls: dict[str, str] | None = None,
        path: str = "/execute",
        timeout: float = 30.0,
    ):
        self._http = http
        self._base_url = base_url
        self._worker_urls = dict(worker_urls or {})
        self._path = path
        self._timeout = timeout

    def send(self, worker_id: str, lease_token: str, payload: dict) -> dict:
        base = self._worker_urls.get(worker_id) or self._base_url
        if base is None:
            raise WorkerError(f"no url configured for worker {worker_id}")
        url = base.rstrip("/") + self._path
        response = self._http.request(
            "POST",
            url,
            headers={"Authorization": f"Bearer {lease_token}"},
            body={"worker_id": worker_id, "lease": lease_token, "payload": payload},
            timeout=self._timeout,
        )
        if 200 <= response.status < 300:
            result = response.json.get("result")
            return result if isinstance(result, dict) else response.json
        error = (
            response.json.get("error") or f"worker returned status {response.status}"
        )
        raise WorkerError(str(error))


class WorkerHttpApp:
    def __init__(self, worker: Worker, *, path: str = "/execute", clock=None):
        self._worker = worker
        self._path = path
        self._clock = clock

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope["type"] != "http":
            raise RuntimeError(f"unsupported scope type: {scope['type']}")
        if scope["method"] != "POST" or scope["path"] != self._path:
            await self._respond(send, 404, {"error": "not_found"})
            return

        body = await self._read_json(receive)
        lease = body.get("lease") or _bearer(scope)
        if not lease:
            await self._respond(send, 401, {"error": "missing lease"})
            return
        payload = body.get("payload") or {}
        now = self._clock() if self._clock else None
        try:
            result = self._worker.execute(str(lease), payload=payload, now=now)
        except LeaseError as exc:
            await self._respond(send, 401, {"error": str(exc)})
        except IsolationViolation as exc:
            await self._respond(send, 403, {"error": str(exc)})
        except WorkerError as exc:
            await self._respond(send, 500, {"error": str(exc)})
        else:
            await self._respond(send, 200, {"result": result})

    @staticmethod
    async def _read_json(receive: Any) -> dict[str, Any]:
        chunks: list[bytes] = []
        more = True
        while more:
            message = await receive()
            chunks.append(message.get("body", b"") or b"")
            more = message.get("more_body", False)
        raw = b"".join(chunks)
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    async def _respond(send: Any, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(k.encode(), v.encode()) for k, v in _JSON_HEADERS.items()],
            }
        )
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _lifespan(receive: Any, send: Any) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return


def _bearer(scope: dict) -> str | None:
    for key, value in scope.get("headers", []):
        if key.decode("latin1").lower() == "authorization":
            token = value.decode("latin1")
            return token[7:] if token.lower().startswith("bearer ") else token
    return None
