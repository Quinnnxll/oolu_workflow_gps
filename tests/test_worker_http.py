from __future__ import annotations

import asyncio
import json

import pytest

from workflow_gps.assembly import build_remote_worker_executor
from workflow_gps.providers.base import ProviderResponse
from workflow_gps.skills.models import ActionEvent, ExecutionStatus
from workflow_gps.worker.control_plane import ControlPlane, WorkerInfo
from workflow_gps.worker.errors import WorkerError
from workflow_gps.worker.http import HttpWorkerTransport, WorkerHttpApp
from workflow_gps.worker.leases import LeaseSigner, LeaseVerifier
from workflow_gps.worker.ledger import InMemoryLeaseLedger
from workflow_gps.worker.worker import StubWorkerExecutor, Worker

_SECRET = "shared-worker-secret"


class _StubHttp:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def request(self, method, url, *, headers=None, body=None, timeout=30.0):
        self.calls.append((method, url, headers, body))
        return self._response


class _AsgiLoopbackHttp:
    """An HttpTransport that drives an ASGI app in-process instead of the network."""

    def __init__(self, app):
        self._app = app

    def request(self, method, url, *, headers=None, body=None, timeout=30.0):
        path = "/" + url.split("/", 3)[3] if url.count("/") >= 3 else "/"
        payload = json.dumps(body).encode() if body is not None else b""
        header_items = [
            (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
        ]
        scope = {
            "type": "http",
            "method": method,
            "path": path,
            "headers": header_items,
        }

        async def receive():
            return {"type": "http.request", "body": payload, "more_body": False}

        sent = []

        async def send(message):
            sent.append(message)

        asyncio.run(self._app(scope, receive, send))
        start = next(m for m in sent if m["type"] == "http.response.start")
        raw = b"".join(
            m.get("body", b"") for m in sent if m["type"] == "http.response.body"
        )
        return ProviderResponse(status=start["status"], json=json.loads(raw))


def _worker(executor=None):
    ledger = InMemoryLeaseLedger()
    verifier = LeaseVerifier(_SECRET, audience="w1", ledger=ledger)
    return Worker("w1", verifier, executor or StubWorkerExecutor(backend_kind="docker"))


def _control_plane():
    cp = ControlPlane(LeaseSigner(_SECRET), ledger=InMemoryLeaseLedger())
    cp.register_worker(WorkerInfo(worker_id="w1", capabilities=frozenset({"run"})))
    return cp


def _action():
    return ActionEvent(correlation_id="s", adapter="worker", operation="run")


def test_client_returns_result_on_2xx():
    http = _StubHttp(
        ProviderResponse(status=200, json={"result": {"status": "succeeded", "n": 1}})
    )
    transport = HttpWorkerTransport(http, worker_urls={"w1": "http://w1.local"})
    result = transport.send("w1", "lease-token", {"task_id": "t"})
    assert result == {"status": "succeeded", "n": 1}
    method, url, headers, body = http.calls[0]
    assert (method, url) == ("POST", "http://w1.local/execute")
    assert headers["Authorization"] == "Bearer lease-token"


def test_client_raises_on_error_status():
    http = _StubHttp(ProviderResponse(status=500, json={"error": "boom"}))
    transport = HttpWorkerTransport(http, base_url="http://w1.local")
    with pytest.raises(WorkerError, match="boom"):
        transport.send("w1", "t", {})


def test_client_raises_when_no_url_for_worker():
    transport = HttpWorkerTransport(_StubHttp(None))
    with pytest.raises(WorkerError, match="no url"):
        transport.send("ghost", "t", {})


def test_server_runs_a_leased_task():
    cp = _control_plane()
    app = WorkerHttpApp(
        _worker(
            StubWorkerExecutor(backend_kind="docker", result={"status": "succeeded"})
        )
    )
    http = _AsgiLoopbackHttp(app)

    from workflow_gps.worker.control_plane import TaskRequest
    from workflow_gps.worker.leases import TrustLevel

    task = TaskRequest(
        tenant_id="t",
        capabilities=frozenset({"run"}),
        trust_level=TrustLevel.UNTRUSTED_SYNTHESIZED,
        payload={},
    )
    task = task.model_copy(update={"payload": {"task_id": task.task_id}})
    assignment = cp.dispatch(task)
    transport = HttpWorkerTransport(http, base_url="http://worker")
    result = transport.send(
        assignment.worker_id, assignment.lease_token, assignment.payload
    )
    assert result["status"] == "succeeded"


def test_server_rejects_a_bad_lease():
    app = WorkerHttpApp(_worker())
    http = _AsgiLoopbackHttp(app)
    transport = HttpWorkerTransport(http, base_url="http://worker")
    with pytest.raises(WorkerError):
        transport.send("w1", "not-a-valid-lease", {"task_id": "x"})


def test_end_to_end_over_http_loopback():
    app = WorkerHttpApp(
        _worker(
            StubWorkerExecutor(
                backend_kind="docker",
                result={"status": "succeeded", "evidence": {"ok": 1}},
            )
        )
    )
    http = _AsgiLoopbackHttp(app)
    executors = build_remote_worker_executor(
        http=http, worker_urls={"w1": "http://worker"}, secret=_SECRET
    )
    outcome = executors["worker"].execute(_action(), idempotency_key="k1")
    assert outcome.status is ExecutionStatus.SUCCEEDED
    assert outcome.evidence["ok"] == 1
    assert outcome.evidence["worker_id"] == "w1"
