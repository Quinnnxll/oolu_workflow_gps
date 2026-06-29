"""Worker control-plane tests (codex/worker-control-plane).

Exit-gate guarantees exercised directly:

1. The control plane never executes untrusted code or holds local credentials —
   it exposes no ``execute`` and no backend/credential store; only workers and
   outbound-only local agents run code and hold secrets.
2. Lost, duplicated, expired, or revoked leases cannot execute — lease
   verification rejects each case.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from workflow_gps.worker import (
    AudienceMismatch,
    ControlPlane,
    ExpiredLease,
    InMemoryLeaseLedger,
    InvalidLease,
    IsolationPolicy,
    IsolationViolation,
    LeaseSigner,
    LeaseVerifier,
    LocalAgent,
    LocalLeaseLedger,
    LocalSecretProvider,
    ReplayedLease,
    RevokedLease,
    StubWorkerExecutor,
    TaskRequest,
    TaskTimeout,
    TrustLevel,
    Worker,
    WorkerInfo,
    WorkerKind,
)

_SECRET = "control-plane-secret"
NOW = datetime(2026, 6, 29, tzinfo=UTC)


def _signer() -> LeaseSigner:
    return LeaseSigner(_SECRET)


def _verifier(audience, ledger) -> LeaseVerifier:
    return LeaseVerifier(_SECRET, audience=audience, ledger=ledger)


def _control_plane(ledger=None) -> ControlPlane:
    return ControlPlane(_signer(), ledger=ledger or InMemoryLeaseLedger())


def _docker_worker(worker_id="w1", caps=frozenset({"run"})) -> WorkerInfo:
    return WorkerInfo(
        worker_id=worker_id,
        capabilities=caps,
        backend_kind="docker",
        max_concurrency=1,
    )


def _task(
    trust=TrustLevel.UNTRUSTED_SYNTHESIZED, caps=frozenset({"run"})
) -> TaskRequest:
    return TaskRequest(
        tenant_id="t1", capabilities=caps, trust_level=trust, payload={"x": 1}
    )


# --------------------------------------------------------------------------- #
# Lease lifecycle — lost, duplicated, expired, revoked leases cannot execute.  #
# --------------------------------------------------------------------------- #
def test_valid_lease_verifies_once():
    ledger = InMemoryLeaseLedger()
    cp = _control_plane(ledger)
    cp.register_worker(_docker_worker())
    assignment = cp.dispatch(_task(), now=NOW)
    assert assignment is not None

    verifier = _verifier("w1", ledger)
    lease = verifier.verify(assignment.lease_token, now=NOW)
    assert lease.task_id == assignment.task_id
    assert lease.audience == "w1"


def test_lost_or_forged_lease_is_rejected():
    ledger = InMemoryLeaseLedger()
    verifier = _verifier("w1", ledger)
    with pytest.raises(InvalidLease):
        verifier.verify("not-a-real-token", now=NOW)
    # A token signed with the wrong key.
    forged = LeaseSigner("wrong-secret")
    cp = ControlPlane(forged, ledger=ledger)
    cp.register_worker(_docker_worker())
    bad = cp.dispatch(_task(), now=NOW)
    with pytest.raises(InvalidLease):
        verifier.verify(bad.lease_token, now=NOW)


def test_duplicated_lease_cannot_execute_twice():
    ledger = InMemoryLeaseLedger()
    cp = _control_plane(ledger)
    cp.register_worker(_docker_worker())
    assignment = cp.dispatch(_task(), now=NOW)
    verifier = _verifier("w1", ledger)
    verifier.verify(assignment.lease_token, now=NOW)  # first use consumes it
    with pytest.raises(ReplayedLease):
        verifier.verify(assignment.lease_token, now=NOW)  # replay rejected


def test_expired_lease_is_rejected():
    ledger = InMemoryLeaseLedger()
    cp = ControlPlane(_signer(), ledger=ledger, default_lease_ttl=60)
    cp.register_worker(_docker_worker())
    assignment = cp.dispatch(_task(), now=NOW)
    verifier = _verifier("w1", ledger)
    with pytest.raises(ExpiredLease):
        verifier.verify(assignment.lease_token, now=NOW + timedelta(hours=1))


def test_wrong_audience_lease_is_rejected():
    ledger = InMemoryLeaseLedger()
    cp = _control_plane(ledger)
    cp.register_worker(_docker_worker("w1"))
    assignment = cp.dispatch(_task(), now=NOW)
    # A different worker tries to use w1's lease.
    other = _verifier("w2", ledger)
    with pytest.raises(AudienceMismatch):
        other.verify(assignment.lease_token, now=NOW)


def test_revoked_lease_cannot_execute():
    ledger = InMemoryLeaseLedger()
    cp = _control_plane(ledger)
    cp.register_worker(_docker_worker())
    assignment = cp.dispatch(_task(), now=NOW)
    assert cp.cancel(assignment.task_id)  # cancellation revokes the lease
    verifier = _verifier("w1", ledger)
    with pytest.raises(RevokedLease):
        verifier.verify(assignment.lease_token, now=NOW)


def test_single_use_survives_ledger_reopen(tmp_path):
    db = tmp_path / "worker.db"
    ledger = LocalLeaseLedger(db)
    cp = _control_plane(ledger)
    cp.register_worker(_docker_worker())
    assignment = cp.dispatch(_task(), now=NOW)
    _verifier("w1", ledger).verify(assignment.lease_token, now=NOW)
    ledger.close()

    # A restarted worker must still see the lease as consumed (no replay).
    reopened = LocalLeaseLedger(db)
    with pytest.raises(ReplayedLease):
        _verifier("w1", reopened).verify(assignment.lease_token, now=NOW)
    reopened.close()


# --------------------------------------------------------------------------- #
# Isolation — untrusted code requires Docker; subprocess is for trusted skills. #
# --------------------------------------------------------------------------- #
def test_untrusted_task_refused_on_subprocess_backend():
    policy = IsolationPolicy()
    with pytest.raises(IsolationViolation):
        policy.enforce(TrustLevel.UNTRUSTED_SYNTHESIZED, "subprocess")
    # Docker is allowed.
    policy.enforce(TrustLevel.UNTRUSTED_SYNTHESIZED, "docker")


def test_trusted_local_skill_allowed_on_subprocess():
    IsolationPolicy().enforce(TrustLevel.TRUSTED_LOCAL_SKILL, "subprocess")


def test_worker_enforces_isolation_before_running():
    ledger = InMemoryLeaseLedger()
    cp = _control_plane(ledger)
    cp.register_worker(_docker_worker())
    assignment = cp.dispatch(_task(trust=TrustLevel.UNTRUSTED_SYNTHESIZED), now=NOW)
    # A worker whose backend is subprocess must refuse the untrusted lease.
    worker = Worker(
        "w1",
        _verifier("w1", ledger),
        StubWorkerExecutor(backend_kind="subprocess"),
    )
    with pytest.raises(IsolationViolation):
        worker.execute(assignment.lease_token, payload=assignment.payload, now=NOW)


def test_control_plane_only_dispatches_to_isolated_worker_for_untrusted():
    cp = _control_plane()
    # Only a subprocess worker is registered; an untrusted task has nowhere to go.
    cp.register_worker(
        WorkerInfo(
            worker_id="sub", capabilities=frozenset({"run"}), backend_kind="subprocess"
        )
    )
    assert cp.dispatch(_task(trust=TrustLevel.UNTRUSTED_SYNTHESIZED), now=NOW) is None
    # A trusted local skill can run on it.
    assert cp.dispatch(_task(trust=TrustLevel.TRUSTED_LOCAL_SKILL), now=NOW) is not None


# --------------------------------------------------------------------------- #
# Control plane holds no execution and no credentials.                        #
# --------------------------------------------------------------------------- #
def test_control_plane_has_no_execution_or_credentials():
    cp = _control_plane()
    assert not hasattr(cp, "execute")
    assert not hasattr(cp, "run")
    # No backend or credential/secret attribute anywhere on the control plane.
    for attr in vars(cp):
        lowered = attr.lower()
        assert "backend" not in lowered
        assert "secret" not in lowered
        assert "credential" not in lowered


# --------------------------------------------------------------------------- #
# Health, capacity, cancellation, timeout, quarantine.                        #
# --------------------------------------------------------------------------- #
def test_capacity_is_respected_and_freed():
    cp = _control_plane()
    cp.register_worker(_docker_worker(caps=frozenset({"run"})))  # max_concurrency=1
    first = cp.dispatch(_task(), now=NOW)
    assert first is not None
    assert cp.dispatch(_task(), now=NOW) is None  # no capacity
    cp.report_result("w1", first.task_id, success=True)  # frees a slot
    assert cp.dispatch(_task(), now=NOW) is not None


def test_unhealthy_worker_is_not_dispatched_to():
    cp = _control_plane()
    cp.register_worker(_docker_worker())
    cp.heartbeat("w1", healthy=False)
    assert cp.dispatch(_task(), now=NOW) is None


def test_repeated_failures_quarantine_worker():
    cp = ControlPlane(
        _signer(), ledger=InMemoryLeaseLedger(), failure_quarantine_threshold=2
    )
    cp.register_worker(_docker_worker())
    a = cp.dispatch(_task(), now=NOW)
    cp.report_result("w1", a.task_id, success=False)
    b = cp.dispatch(_task(), now=NOW)
    cp.report_result("w1", b.task_id, success=False)
    assert cp.is_quarantined("w1")
    assert cp.dispatch(_task(), now=NOW) is None  # quarantined: no more work


def test_worker_enforces_timeout():
    ledger = InMemoryLeaseLedger()
    cp = _control_plane(ledger)
    cp.register_worker(_docker_worker())
    task = TaskRequest(
        tenant_id="t1",
        capabilities=frozenset({"run"}),
        trust_level=TrustLevel.UNTRUSTED_SYNTHESIZED,
        timeout_seconds=0.01,
        payload={},
    )
    assignment = cp.dispatch(task, now=NOW)
    worker = Worker(
        "w1",
        _verifier("w1", ledger),
        StubWorkerExecutor(backend_kind="docker", sleep=0.2),
    )
    with pytest.raises(TaskTimeout):
        worker.execute(assignment.lease_token, payload=assignment.payload, now=NOW)


# --------------------------------------------------------------------------- #
# Outbound-only local agent: executes locally, keeps credentials local.       #
# --------------------------------------------------------------------------- #
def test_local_agent_polls_executes_and_keeps_credentials_local():
    ledger = InMemoryLeaseLedger()
    cp = _control_plane(ledger)
    cp.register_worker(
        WorkerInfo(
            worker_id="agent-1",
            kind=WorkerKind.LOCAL_AGENT,
            capabilities=frozenset({"desktop"}),
            backend_kind="subprocess",
        )
    )
    # A trusted local task targeting a desktop resource, referencing a local secret.
    task = TaskRequest(
        tenant_id="t1",
        capabilities=frozenset({"desktop"}),
        trust_level=TrustLevel.TRUSTED_LOCAL_SKILL,
        payload={"credential_ref": "desktop-token"},
    )
    assignment = cp.dispatch(task, now=NOW)
    assert assignment is not None and assignment.worker_id == "agent-1"

    worker = Worker(
        "agent-1",
        _verifier("agent-1", ledger),
        StubWorkerExecutor(backend_kind="subprocess"),
    )
    agent = LocalAgent(
        worker,
        cp,
        secrets=LocalSecretProvider({"desktop-token": "s3cr3t-value"}),
    )
    result = agent.run_once(now=NOW)
    assert result is not None and result["status"] == "succeeded"
    # The secret was resolved locally by the agent...
    assert agent.resolve_secret("desktop-token") == "s3cr3t-value"
    # ...and never reached the control plane.
    cp_state = repr(vars(cp))
    assert "s3cr3t-value" not in cp_state


def test_local_agent_returns_none_when_no_work():
    cp = _control_plane()
    cp.register_worker(
        WorkerInfo(
            worker_id="agent-1", kind=WorkerKind.LOCAL_AGENT, backend_kind="subprocess"
        )
    )
    worker = Worker(
        "agent-1",
        _verifier("agent-1", InMemoryLeaseLedger()),
        StubWorkerExecutor(backend_kind="subprocess"),
    )
    agent = LocalAgent(worker, cp)
    assert agent.run_once(now=NOW) is None
