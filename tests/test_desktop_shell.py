"""Desktop shell tests (codex/desktop-shell).

Exit gate:
1. A non-developer can complete, pause, resume, inspect, and recover a local
   workflow entirely through ``DesktopService``.
2. The UI cannot bypass backend policy (no execute path; approvals require an
   authorized identity session) or expose provider credentials (no secret ever
   appears in a view).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from workflow_gps.desktop import DesktopService
from workflow_gps.durable import DurableConnection, DurableWorkflowService
from workflow_gps.identity import (
    AuthorityGrant,
    AuthorityResolver,
    Hs256Signer,
    Hs256Verifier,
    IdentityApprovalAuthority,
    IdentityStore,
    OidcValidator,
    ProviderConfig,
    Role,
    SessionManager,
    Tenant,
)
from workflow_gps.identity.errors import AuthorizationError
from workflow_gps.orchestrator import (
    ActionExecutorRouteRunner,
    Blueprint,
    BoundedRetryRecovery,
    CapabilityGrounder,
    CollectingFeedbackSink,
    LeastCostRouteOptimizer,
    ReservedAction,
    RiskBasedHumanControl,
    StaticIntaker,
    StatusOutcomeMonitor,
    WorkflowOrchestrator,
)
from workflow_gps.skills.models import (
    ActionEvent,
    ExecutionOutcome,
    ExecutionStatus,
)
from workflow_gps.skills.requirements import (
    AuthorizationGrant as AuthGrant,
)
from workflow_gps.skills.requirements import (
    AuthorizationMode,
    ParameterDomain,
    ParameterSource,
    RequirementBrief,
    RequirementParameter,
)

_SECRET = "idp-secret"
_ISSUER = "https://idp"
_AUDIENCE = "wfgps"
NOW = datetime(2026, 6, 29, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Backend builders.                                                          #
# --------------------------------------------------------------------------- #
class ScriptedActionExecutor:
    name = "test"

    def __init__(self, capabilities, *, fail_times=0):
        self._caps = frozenset(capabilities)
        self._fail_times = fail_times
        self.calls = 0

    def capabilities(self):
        return self._caps

    def execute(self, action, *, idempotency_key):
        self.calls += 1
        status = (
            ExecutionStatus.FAILED
            if self.calls <= self._fail_times
            else ExecutionStatus.SUCCEEDED
        )
        return ExecutionOutcome(
            idempotency_key=idempotency_key,
            skill_id=action.correlation_id,
            status=status,
            error="boom" if status is ExecutionStatus.FAILED else None,
        )

    def cancel(self, idempotency_key):
        return None


def _param(name, *, value=None):
    if value is None:
        return RequirementParameter(
            name=name,
            description=name,
            domain=ParameterDomain(value_type="str"),
            required=True,
            suggested_values=["small", "large"],
            question=f"What {name}?",
            question_priority=1,
        )
    return RequirementParameter(
        name=name,
        description=name,
        domain=ParameterDomain(value_type="str"),
        required=True,
        value=value,
        source=ParameterSource.USER,
    )


def _blueprint(*, operation, capability, reserved, risk):
    action = ActionEvent(correlation_id="c1", adapter="test", operation=operation)
    return Blueprint(
        name=f"{operation}-route",
        actions=[
            ReservedAction(
                action=action,
                required_capabilities=frozenset({capability}),
                reserved=reserved,
                risk=risk,
            )
        ],
        estimated_cost=2.5,
    )


def _factory(*, brief, blueprint, executor, grounding_map):
    def build(events):
        return WorkflowOrchestrator(
            intaker=StaticIntaker(brief),
            grounder=CapabilityGrounder(grounding_map),
            optimizer=LeastCostRouteOptimizer([blueprint]),
            human_control=RiskBasedHumanControl(),
            executor=ActionExecutorRouteRunner({"test": executor}),
            monitor=StatusOutcomeMonitor(),
            recovery=BoundedRetryRecovery(),
            feedback=CollectingFeedbackSink(),
            events=events,
        )

    return build


def _identity(tmp_path):
    store = IdentityStore(tmp_path / "identity.db")
    store.add_tenant(Tenant(tenant_id="t1", name="t1"))
    store.add_role(
        Role(tenant_id="t1", name="approver", permissions=frozenset({"approve:*"}))
    )
    store.add_grant(
        AuthorityGrant(
            tenant_id="t1",
            principal_id="approver-1",
            role_name="approver",
            granted_by="admin",
        )
    )
    validator = OidcValidator(
        [
            ProviderConfig(
                issuer=_ISSUER,
                audiences=frozenset({_AUDIENCE}),
                verifier=Hs256Verifier(_SECRET),
            )
        ]
    )
    manager = SessionManager(store, validator)
    signer = Hs256Signer(secret=_SECRET, issuer=_ISSUER, audience=_AUDIENCE)
    authority = IdentityApprovalAuthority(AuthorityResolver(store))
    # Mint at the real wall clock so the session is valid when approve() runs
    # (approve uses datetime.now internally, not the fixed test clock).
    moment = datetime.now(UTC)
    approver = manager.login(
        signer.mint(subject="approver-1", tenant_id="t1", now=moment), now=moment
    )
    intruder = manager.login(
        signer.mint(subject="nobody", tenant_id="t1", now=moment), now=moment
    )
    return store, authority, approver, intruder


def _desktop(tmp_path, *, brief, blueprint, executor, grounding_map, authority=None):
    conn = DurableConnection(tmp_path / "durable.db")
    durable = DurableWorkflowService(
        conn,
        _factory(
            brief=brief,
            blueprint=blueprint,
            executor=executor,
            grounding_map=grounding_map,
        ),
    )
    return DesktopService(durable, approval_authority=authority), conn


# --------------------------------------------------------------------------- #
# Full lifecycle: clarify -> confirm -> approve -> execute -> complete.        #
# --------------------------------------------------------------------------- #
def test_full_lifecycle_through_desktop_service(tmp_path):
    store, authority, approver, intruder = _identity(tmp_path)
    brief = RequirementBrief(
        intent="provision",
        parameters=[_param("size")],  # unresolved -> clarification
        authorization=AuthGrant(mode=AuthorizationMode.GUIDED),
    )
    ds, conn = _desktop(
        tmp_path,
        brief=brief,
        blueprint=_blueprint(
            operation="apply", capability="apply", reserved=True, risk="write"
        ),
        executor=ScriptedActionExecutor({"apply"}),
        grounding_map={"size": "apply"},
        authority=authority,
    )

    # Task entry surfaces a guided question.
    view = ds.submit_task("provision", submitted_by="local-user")
    run_id = view.run_id
    assert view.awaiting == "clarification"
    assert [q.parameter for q in view.questions] == ["size"]

    # Answer -> confirmation.
    view = ds.answer_questions(run_id, {"size": "large"})
    assert view.awaiting == "confirmation"
    assert any(item.run_id == run_id for item in ds.inbox("confirmation"))

    # Route preview shows cost and the reserved action.
    preview = ds.route_preview(run_id)
    assert preview.chosen is not None
    assert preview.total_cost == 2.5
    assert preview.chosen.reserved_action_count == 1

    # Confirm -> approval.
    view = ds.confirm(run_id, approved=True)
    assert view.awaiting == "approval"
    assert any(item.run_id == run_id for item in ds.inbox("approval"))

    # Approve -> complete.
    view = ds.approve(run_id, session=approver)
    assert view.phase == "completed"

    # Inspect: timeline + verifiable audit.
    assert ds.timeline(run_id)
    audit = ds.audit(run_id)
    assert audit.verified
    conn.close()


def test_recovery_through_incident_inbox(tmp_path):
    brief = RequirementBrief(
        intent="sync",
        parameters=[_param("src", value="s3")],
        authorization=AuthGrant(mode=AuthorizationMode.FULLY_DELEGATED),
    )
    # Two automatic attempts fail; the operator retry succeeds.
    ds, conn = _desktop(
        tmp_path,
        brief=brief,
        blueprint=_blueprint(
            operation="sync", capability="sync", reserved=False, risk="read"
        ),
        executor=ScriptedActionExecutor({"sync"}, fail_times=2),
        grounding_map={"src": "sync"},
    )
    view = ds.submit_task("sync")
    run_id = view.run_id
    assert view.awaiting == "incident"
    assert any(item.run_id == run_id for item in ds.inbox("incident"))

    view = ds.resolve_incident(run_id, decision="retry")
    assert view.phase == "completed"
    conn.close()


def test_cancel_a_paused_workflow(tmp_path):
    brief = RequirementBrief(
        intent="provision",
        parameters=[_param("size")],
        authorization=AuthGrant(mode=AuthorizationMode.GUIDED),
    )
    ds, conn = _desktop(
        tmp_path,
        brief=brief,
        blueprint=_blueprint(
            operation="apply", capability="apply", reserved=False, risk="read"
        ),
        executor=ScriptedActionExecutor({"apply"}),
        grounding_map={"size": "apply"},
    )
    view = ds.submit_task("provision")
    assert view.awaiting == "clarification"
    view = ds.cancel(view.run_id)
    assert view.phase == "cancelled"
    conn.close()


# --------------------------------------------------------------------------- #
# The UI cannot bypass backend policy.                                        #
# --------------------------------------------------------------------------- #
def test_desktop_service_has_no_execution_path(tmp_path):
    ds, conn = _desktop(
        tmp_path,
        brief=RequirementBrief(intent="x", parameters=[_param("a", value="b")]),
        blueprint=_blueprint(
            operation="run", capability="run", reserved=False, risk="read"
        ),
        executor=ScriptedActionExecutor({"run"}),
        grounding_map={"a": "run"},
    )
    assert not hasattr(ds, "execute")
    assert not hasattr(ds, "run_script")
    conn.close()


def test_unauthorized_session_cannot_approve(tmp_path):
    store, authority, approver, intruder = _identity(tmp_path)
    brief = RequirementBrief(
        intent="delete",
        parameters=[_param("ds", value="logs")],
        authorization=AuthGrant(mode=AuthorizationMode.FULLY_DELEGATED),
    )
    ds, conn = _desktop(
        tmp_path,
        brief=brief,
        blueprint=_blueprint(
            operation="delete", capability="delete", reserved=True, risk="irreversible"
        ),
        executor=ScriptedActionExecutor({"delete"}),
        grounding_map={"ds": "delete"},
        authority=authority,
    )
    view = ds.submit_task("delete", submitted_by="local-user")
    run_id = view.run_id
    assert view.awaiting == "approval"
    # An intruder session with no grant cannot approve; the run is not advanced.
    with pytest.raises(AuthorizationError):
        ds.approve(run_id, session=intruder)
    assert ds.task(run_id).awaiting == "approval"
    # The authorized approver succeeds.
    assert ds.approve(run_id, session=approver).phase == "completed"
    conn.close()


def test_cannot_approve_before_approval_phase(tmp_path):
    store, authority, approver, intruder = _identity(tmp_path)
    brief = RequirementBrief(
        intent="provision",
        parameters=[_param("size")],
        authorization=AuthGrant(mode=AuthorizationMode.GUIDED),
    )
    ds, conn = _desktop(
        tmp_path,
        brief=brief,
        blueprint=_blueprint(
            operation="apply", capability="apply", reserved=True, risk="write"
        ),
        executor=ScriptedActionExecutor({"apply"}),
        grounding_map={"size": "apply"},
        authority=authority,
    )
    view = ds.submit_task("provision")
    # Still at clarification — approving now must be refused (no skipping gates).
    with pytest.raises(RuntimeError):
        ds.approve(view.run_id, session=approver)
    conn.close()


# --------------------------------------------------------------------------- #
# The UI cannot expose provider credentials.                                  #
# --------------------------------------------------------------------------- #
def test_provider_credentials_never_appear_in_views(tmp_path):
    ds, conn = _desktop(
        tmp_path,
        brief=RequirementBrief(intent="x", parameters=[_param("a", value="b")]),
        blueprint=_blueprint(
            operation="run", capability="run", reserved=False, risk="read"
        ),
        executor=ScriptedActionExecutor({"run"}),
        grounding_map={"a": "run"},
    )
    secret = "sk-super-secret-value-123"
    view = ds.connect_provider("openai", secret, scopes=["models"])
    assert view.provider == "openai"
    assert view.status == "connected"
    # The secret appears in no view surface.
    surfaces = [
        repr(view),
        str(ds.list_connections()),
        str(ds.connect_provider("anthropic", "sk-ant-other", scopes=[])),
    ]
    for surface in surfaces:
        assert secret not in surface
        assert "sk-ant-other" not in surfaces[0]

    # Disconnect flips status without revealing anything.
    disconnected = ds.disconnect(view.connection_id)
    assert disconnected.status == "disconnected"
    assert secret not in repr(disconnected)
    conn.close()


def test_worker_health_labels_trusted_and_untrusted(tmp_path):
    ds, conn = _desktop(
        tmp_path,
        brief=RequirementBrief(intent="x", parameters=[_param("a", value="b")]),
        blueprint=_blueprint(
            operation="run", capability="run", reserved=False, risk="read"
        ),
        executor=ScriptedActionExecutor({"run"}),
        grounding_map={"a": "run"},
    )
    health = ds.worker_health()
    assert health.docker_available
    by_trust = {label.trust_level: label for label in health.labels}
    assert by_trust["untrusted_synthesized"].isolated is True
    assert by_trust["trusted_local_skill"].isolated is False
    conn.close()


def test_offline_policy_and_export_then_delete(tmp_path):
    brief = RequirementBrief(
        intent="sync",
        parameters=[_param("src", value="s3")],
        authorization=AuthGrant(mode=AuthorizationMode.FULLY_DELEGATED),
    )
    ds, conn = _desktop(
        tmp_path,
        brief=brief,
        blueprint=_blueprint(
            operation="sync", capability="sync", reserved=False, risk="read"
        ),
        executor=ScriptedActionExecutor({"sync"}),
        grounding_map={"src": "sync"},
    )
    view = ds.submit_task("sync")
    run_id = view.run_id
    assert view.phase == "completed"

    assert ds.offline_policy()["network"] == "local-only"

    bundle = ds.export_data(run_id)
    assert bundle.run_id == run_id
    assert bundle.run_state["intent"] == "sync"
    assert bundle.audit  # audit entries exported

    counts = ds.delete_data(run_id)
    assert counts["workflow_runs"] == 1
    assert ds._durable.get(run_id) is None  # erased
    conn.close()
