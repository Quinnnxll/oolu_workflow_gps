from __future__ import annotations

import pytest

from oolu.assembly import build_worker_executor
from oolu.models import ExecutionResult, Phase
from oolu.runtime.backend import (
    BackendUnavailable,
    ExecutionRequest,
    StubBackend,
    make_success,
)
from oolu.skills.models import ActionEvent, ExecutionStatus
from oolu.worker.execution import BackendWorkerExecutor


def _payload(script="emit_result({'ok': 1})", **params):
    return {
        "task_id": "t1",
        "action": {
            "adapter": "worker",
            "operation": "run",
            "parameters": {"script": script, **params},
        },
    }


def test_success_maps_from_backend_result():
    backend = StubBackend([make_success({"answer": 42})])
    executor = BackendWorkerExecutor(backend, backend_kind="docker")
    result = executor.run(_payload())
    assert result["status"] == "succeeded"
    assert result["evidence"]["contract_ok"] is True
    assert result["evidence"]["payload"] == {"answer": 42}
    assert result["error"] is None


def test_script_failure_maps_to_failed():
    failing = ExecutionResult(phase=Phase.EXECUTE, exit_code=1, stderr="boom")
    executor = BackendWorkerExecutor(StubBackend([failing]))
    result = executor.run(_payload())
    assert result["status"] == "failed"
    assert "boom" in result["error"]


def test_no_script_is_failed():
    executor = BackendWorkerExecutor(StubBackend([]))
    result = executor.run({"task_id": "t", "action": {"parameters": {}}})
    assert result["status"] == "failed"
    assert result["error"] == "no script to run"


def test_backend_error_surfaces_as_worker_error():
    from oolu.worker.errors import WorkerError

    executor = BackendWorkerExecutor(StubBackend([], healthy=False))
    with pytest.raises(WorkerError, match="isolation backend failed"):
        executor.run(_payload())


def test_request_carries_deps_and_limits():
    captured = {}

    class _Capturing:
        backend_kind = "docker"
        name = "cap"

        def health_check(self):
            return True

        def run(self, request: ExecutionRequest):
            captured["request"] = request
            return make_success({})

        def close(self):
            pass

    BackendWorkerExecutor(_Capturing()).run(
        _payload(
            dependencies=["numpy"],
            limits={"wall_timeout_s": 5.0},
            env={"TZ": "UTC"},
        )
    )
    request = captured["request"]
    assert request.dependencies == ["numpy"]
    assert request.limits.wall_timeout_s == 5.0
    assert request.env == {"TZ": "UTC"}
    assert request.session_id == "t1"


def test_backend_worker_runs_through_the_control_plane():
    backend = StubBackend([make_success({"done": True})])
    executors = build_worker_executor(
        BackendWorkerExecutor(backend, backend_kind="docker")
    )
    action = ActionEvent(
        correlation_id="s",
        adapter="worker",
        operation="run",
        parameters={"script": "emit_result({'done': True})"},
    )
    outcome = executors["worker"].execute(action, idempotency_key="k1")
    assert outcome.status is ExecutionStatus.SUCCEEDED
    assert outcome.evidence["payload"] == {"done": True}


def test_docker_builder_requires_docker():
    from oolu.assembly import build_docker_worker_executor

    # No docker SDK / daemon in this environment: the builder must fail loudly,
    # never silently fall back to an unsandboxed backend.
    with pytest.raises(BackendUnavailable):
        build_docker_worker_executor()
