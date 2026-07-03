from __future__ import annotations

import asyncio
import concurrent.futures
import json

import pytest

from workflow_gps.providers.base import ProviderResponse
from workflow_gps.worker.control_plane import ControlPlane, TaskRequest, WorkerInfo
from workflow_gps.worker.errors import RevocationUnavailable, WorkerError
from workflow_gps.worker.http import (
    HttpWorkerTransport,
    RemoteRevocationLedger,
    RevocationHttpApp,
    WorkerHttpApp,
)
from workflow_gps.worker.leases import LeaseSigner, LeaseVerifier, TrustLevel
from workflow_gps.worker.ledger import InMemoryLeaseLedger
from workflow_gps.worker.worker import StubWorkerExecutor, Worker

_SECRET = "shared-worker-secret"


class _AsgiLoopbackHttp:
    def __init__(self, app):
        self._app = app

    def request(self, method, url, *, headers=None, body=None, timeout=30.0):
        tail = url.split("/", 3)
        path = "/" + tail[3] if len(tail) >= 4 else "/"
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

        # Run in a fresh thread so nested loopback calls (a worker's revocation
        # query fired from inside a dispatch call) each get their own event loop.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(lambda: asyncio.run(self._app(scope, receive, send))).result()
        start = next(m for m in sent if m["type"] == "http.response.start")
        raw = b"".join(
            m.get("body", b"") for m in sent if m["type"] == "http.response.body"
        )
        return ProviderResponse(status=start["status"], json=json.loads(raw))


class _DownHttp:
    def request(self, method, url, *, headers=None, body=None, timeout=30.0):
        return ProviderResponse(status=503, json={"error": "unreachable"})


def _wire():
    cp_ledger = InMemoryLeaseLedger()
    cp = ControlPlane(LeaseSigner(_SECRET), ledger=cp_ledger)
    cp.register_worker(WorkerInfo(worker_id="w1", capabilities=frozenset({"run"})))

    revocation_http = _AsgiLoopbackHttp(RevocationHttpApp(cp))
    worker_ledger = RemoteRevocationLedger(
        revocation_http, control_plane_url="http://control-plane"
    )
    verifier = LeaseVerifier(_SECRET, audience="w1", ledger=worker_ledger)
    worker = Worker(
        "w1",
        verifier,
        StubWorkerExecutor(backend_kind="docker", result={"status": "succeeded"}),
    )
    dispatch_http = _AsgiLoopbackHttp(WorkerHttpApp(worker))
    transport = HttpWorkerTransport(dispatch_http, base_url="http://worker")
    return cp, transport


def _dispatch(cp):
    task = TaskRequest(
        tenant_id="t",
        capabilities=frozenset({"run"}),
        trust_level=TrustLevel.UNTRUSTED_SYNTHESIZED,
        payload={},
    )
    task = task.model_copy(update={"payload": {"task_id": task.task_id}})
    return task, cp.dispatch(task)


def test_uncancelled_lease_runs():
    cp, transport = _wire()
    task, assignment = _dispatch(cp)
    result = transport.send(
        assignment.worker_id, assignment.lease_token, assignment.payload
    )
    assert result["status"] == "succeeded"


def test_cancel_on_control_plane_blocks_execution_on_the_worker():
    cp, transport = _wire()
    task, assignment = _dispatch(cp)
    assert cp.cancel(task.task_id) is True
    with pytest.raises(WorkerError, match="revoked"):
        transport.send(assignment.worker_id, assignment.lease_token, assignment.payload)


def test_revocation_authority_unreachable_is_fail_closed():
    ledger = RemoteRevocationLedger(
        _DownHttp(), control_plane_url="http://control-plane"
    )
    with pytest.raises(RevocationUnavailable):
        ledger.is_revoked("some-lease-id")


def test_revocation_endpoint_reports_state():
    cp_ledger = InMemoryLeaseLedger()
    cp = ControlPlane(LeaseSigner(_SECRET), ledger=cp_ledger)
    http = _AsgiLoopbackHttp(RevocationHttpApp(cp))
    ledger = RemoteRevocationLedger(http, control_plane_url="http://cp")

    assert ledger.is_revoked("lease-x") is False
    cp_ledger.revoke("lease-x")
    assert ledger.is_revoked("lease-x") is True
