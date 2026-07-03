from __future__ import annotations

from workflow_gps.assembly import build_worker_executor
from workflow_gps.skills.models import ActionEvent, ExecutionStatus
from workflow_gps.worker.worker import StubWorkerExecutor


def _action(operation="run", adapter="worker"):
    return ActionEvent(
        correlation_id="s1",
        adapter=adapter,
        operation=operation,
        parameters={"code": "print('hi')"},
    )


def test_dispatches_to_worker_and_returns_outcome():
    executors = build_worker_executor(
        StubWorkerExecutor(
            backend_kind="docker",
            result={"status": "succeeded", "evidence": {"ran": True}},
        )
    )
    worker = executors["worker"]
    outcome = worker.execute(_action(), idempotency_key="k1")
    assert outcome.status is ExecutionStatus.SUCCEEDED
    assert outcome.evidence["worker_id"] == "worker-1"
    assert outcome.evidence["trust_level"] == "untrusted_synthesized"
    assert outcome.evidence["ran"] is True


def test_untrusted_code_cannot_land_on_a_non_isolated_backend():
    # A subprocess-only worker is not a permitted backend for untrusted code, so
    # dispatch finds no eligible worker and the action is blocked (never run here).
    executors = build_worker_executor(StubWorkerExecutor(backend_kind="subprocess"))
    outcome = executors["worker"].execute(_action(), idempotency_key="k1")
    assert outcome.status is ExecutionStatus.BLOCKED
    assert "no eligible worker" in outcome.error


def test_trusted_task_may_use_subprocess_backend():
    executors = build_worker_executor(
        StubWorkerExecutor(backend_kind="subprocess", result={"status": "succeeded"}),
        trust_level="trusted_local_skill",
    )
    outcome = executors["worker"].execute(_action(), idempotency_key="k1")
    assert outcome.status is ExecutionStatus.SUCCEEDED


def test_worker_failure_maps_to_failed():
    executors = build_worker_executor(
        StubWorkerExecutor(backend_kind="docker", fail=True)
    )
    outcome = executors["worker"].execute(_action(), idempotency_key="k1")
    assert outcome.status is ExecutionStatus.FAILED
    assert "worker error" in outcome.error


def test_timeout_is_enforced_by_the_lease_budget():
    executors = build_worker_executor(
        StubWorkerExecutor(backend_kind="docker", sleep=0.3),
        timeout_seconds=0.05,
    )
    outcome = executors["worker"].execute(_action(), idempotency_key="k1")
    assert outcome.status is ExecutionStatus.FAILED


def test_unsupported_operation_is_blocked():
    executors = build_worker_executor(StubWorkerExecutor(backend_kind="docker"))
    outcome = executors["worker"].execute(
        _action(operation="mine_bitcoin"), idempotency_key="k1"
    )
    assert outcome.status is ExecutionStatus.BLOCKED


def test_success_is_idempotent():
    executors = build_worker_executor(
        StubWorkerExecutor(backend_kind="docker", result={"status": "succeeded"})
    )
    worker = executors["worker"]
    first = worker.execute(_action(), idempotency_key="k1")
    second = worker.execute(_action(), idempotency_key="k1")
    assert first is second


def test_cancel_is_safe_for_unknown_and_known_keys():
    executors = build_worker_executor(
        StubWorkerExecutor(backend_kind="docker", result={"status": "succeeded"})
    )
    worker = executors["worker"]
    worker.cancel("never-seen")
    worker.execute(_action(), idempotency_key="k1")
    worker.cancel("k1")
