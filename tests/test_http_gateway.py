"""HTTP gateway tests (codex/http-gateway).

Exit gate: multi-process and cross-tenant behaviour, plus restart, retry/timeout
(via the durable backend), duplicate submission, and webhook replay. Also covers
OIDC auth, tenant-aware RBAC, quotas/rate limits, pagination, SSE, and the
versioned OpenAPI contract.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from oolu.durable import DurableConnection, DurableWorkflowService
from oolu.gateway import (
    GatewayApp,
    GatewayConfig,
    Request,
    WebhookError,
    WebhookSigner,
    WebhookVerifier,
)
from oolu.identity import (
    AuthorityGrant,
    AuthorityResolver,
    Hs256Signer,
    Hs256Verifier,
    IdentityApprovalAuthority,
    IdentityStore,
    OidcValidator,
    ProviderConfig,
    Role,
    Tenant,
)
from oolu.orchestrator import (
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
from oolu.skills.models import ActionEvent, ExecutionOutcome, ExecutionStatus
from oolu.skills.requirements import (
    AuthorizationGrant,
    AuthorizationMode,
    ParameterDomain,
    ParameterSource,
    RequirementBrief,
    RequirementParameter,
)

_IDP = "idp-secret"
_ISSUER = "https://idp"
_AUDIENCE = "oolu"
NOW = datetime(2026, 6, 29, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Backend + identity builders.                                               #
# --------------------------------------------------------------------------- #
class _Executor:
    name = "test"

    def __init__(self, caps, *, fail_times=0):
        self._caps = frozenset(caps)
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
            suggested_values=["a"],
            question=f"What {name}?",
        )
    return RequirementParameter(
        name=name,
        description=name,
        domain=ParameterDomain(value_type="str"),
        required=True,
        value=value,
        source=ParameterSource.USER,
    )


def _blueprint(*, operation="run", capability="run", reserved=False, risk="read"):
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
        estimated_cost=1.0,
    )


def _autonomous():
    return (
        RequirementBrief(
            intent="auto",
            parameters=[_param("a", value="b")],
            authorization=AuthorizationGrant(mode=AuthorizationMode.FULLY_DELEGATED),
        ),
        _blueprint(),
        _Executor({"run"}),
        {"a": "run"},
    )


def _clarify():
    return (
        RequirementBrief(
            intent="clarify",
            parameters=[_param("size")],
            authorization=AuthorizationGrant(mode=AuthorizationMode.GUIDED),
        ),
        _blueprint(operation="apply", capability="apply", risk="write"),
        _Executor({"apply"}),
        {"size": "apply"},
    )


def _approval():
    return (
        RequirementBrief(
            intent="approve-me",
            parameters=[_param("target", value="prod")],
            authorization=AuthorizationGrant(mode=AuthorizationMode.GUIDED),
        ),
        _blueprint(operation="apply", capability="apply", reserved=True, risk="write"),
        _Executor({"apply"}),
        {"target": "apply"},
    )


def _factory(brief, blueprint, executor, grounding_map):
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


class _Identity:
    def __init__(self, tmp_path):
        store = IdentityStore(tmp_path / "identity.db")
        for tenant in ("t1", "t2"):
            store.add_tenant(Tenant(tenant_id=tenant, name=tenant))
        store.add_role(
            Role(tenant_id="t1", name="approver", permissions=frozenset({"approve:*"}))
        )
        store.add_grant(
            AuthorityGrant(
                tenant_id="t1",
                principal_id="approver-1",
                role_name="approver",
                granted_by="x",
            )
        )
        store.add_role(
            Role(
                tenant_id="t1",
                name="admin",
                permissions=frozenset({"providers:manage"}),
            )
        )
        store.add_grant(
            AuthorityGrant(
                tenant_id="t1",
                principal_id="admin-1",
                role_name="admin",
                granted_by="x",
            )
        )
        self.store = store
        self.validator = OidcValidator(
            [
                ProviderConfig(
                    issuer=_ISSUER,
                    audiences=frozenset({_AUDIENCE}),
                    verifier=Hs256Verifier(_IDP),
                )
            ]
        )
        self.resolver = AuthorityResolver(store)
        self.authority = IdentityApprovalAuthority(self.resolver)
        self._signer = Hs256Signer(secret=_IDP, issuer=_ISSUER, audience=_AUDIENCE)

    def token(self, subject, tenant="t1"):
        return self._signer.mint(subject=subject, tenant_id=tenant, now=NOW)


def _app(tmp_path, scenario=_autonomous, *, ident=None, config=None, path=None):
    ident = ident or _Identity(tmp_path)
    brief, blueprint, executor, grounding = scenario()
    conn = DurableConnection(path or (tmp_path / "durable.db"))
    durable = DurableWorkflowService(
        conn, _factory(brief, blueprint, executor, grounding)
    )
    app = GatewayApp(
        durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        config=config,
    )
    return app, conn, ident


def _req(method, path, *, token=None, body=None, query=None, headers=None, now=None):
    hdrs = dict(headers or {})
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    return Request(
        method=method,
        path=path,
        headers=hdrs,
        query=query or {},
        body=body,
        now=now or NOW,
    )


# --------------------------------------------------------------------------- #
# OpenAPI + auth.                                                             #
# --------------------------------------------------------------------------- #
def test_openapi_is_public_and_versioned(tmp_path):
    app, conn, _ = _app(tmp_path)
    response = app.handle(_req("GET", "/v1/openapi.json"))
    assert response.status == 200
    assert response.body["info"]["version"] == "v1"
    assert "/v1/runs" in response.body["paths"]
    # Security headers on every response.
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    conn.close()


def test_authentication_is_required(tmp_path):
    app, conn, _ = _app(tmp_path)
    assert app.handle(_req("GET", "/v1/runs")).status == 401
    assert app.handle(_req("GET", "/v1/runs", token="garbage")).status == 401
    conn.close()


# --------------------------------------------------------------------------- #
# Async submission, status, idempotency, duplicate submission.                #
# --------------------------------------------------------------------------- #
def test_submit_is_async_and_status_is_readable(tmp_path):
    app, conn, ident = _app(tmp_path)
    token = ident.token("user-1")
    submit = app.handle(_req("POST", "/v1/runs", token=token, body={"intent": "auto"}))
    assert submit.status == 202
    run_id = submit.body["run_id"]
    status = app.handle(_req("GET", f"/v1/runs/{run_id}", token=token))
    assert status.status == 200
    assert status.body["phase"] == "completed"
    conn.close()


def test_duplicate_submission_is_idempotent(tmp_path):
    app, conn, ident = _app(tmp_path)
    token = ident.token("user-1")
    headers = {"Idempotency-Key": "key-1"}
    first = app.handle(
        _req("POST", "/v1/runs", token=token, body={"intent": "x"}, headers=headers)
    )
    second = app.handle(
        _req("POST", "/v1/runs", token=token, body={"intent": "x"}, headers=headers)
    )
    assert first.body["run_id"] == second.body["run_id"]
    listing = app.handle(_req("GET", "/v1/runs", token=token))
    assert listing.body["total"] == 1  # only one run created
    conn.close()


# --------------------------------------------------------------------------- #
# Cross-tenant isolation + multi-process + restart.                          #
# --------------------------------------------------------------------------- #
def test_cross_tenant_access_is_not_found(tmp_path):
    app, conn, ident = _app(tmp_path)
    run_id = app.handle(
        _req(
            "POST", "/v1/runs", token=ident.token("user-1", "t1"), body={"intent": "x"}
        )
    ).body["run_id"]
    # A token for tenant t2 cannot see tenant t1's run.
    other = app.handle(
        _req("GET", f"/v1/runs/{run_id}", token=ident.token("user-2", "t2"))
    )
    assert other.status == 404
    conn.close()


def test_the_run_list_is_scoped_to_the_account_not_the_tenant(tmp_path):
    # Two people on ONE host (same tenant) must never see each other's
    # Noder activity — a run belongs to the account that submitted it.
    app, conn, ident = _app(tmp_path)
    alice, bob = ident.token("alice", "t1"), ident.token("bob", "t1")
    app.handle(_req("POST", "/v1/runs", token=alice, body={"intent": "alice's"}))
    app.handle(_req("POST", "/v1/runs", token=bob, body={"intent": "bob's"}))

    alice_list = app.handle(_req("GET", "/v1/runs", token=alice)).body
    assert alice_list["total"] == 1
    assert alice_list["items"][0]["intent"] == "alice's"
    bob_list = app.handle(_req("GET", "/v1/runs", token=bob)).body
    assert bob_list["total"] == 1 and bob_list["items"][0]["intent"] == "bob's"
    conn.close()


def test_multi_process_sees_one_consistent_view(tmp_path):
    db = tmp_path / "shared.db"
    ident = _Identity(tmp_path)
    app1, conn1, _ = _app(tmp_path, ident=ident, path=db)
    app2, conn2, _ = _app(tmp_path, ident=ident, path=db)
    token = ident.token("user-1")
    run_id = app1.handle(
        _req("POST", "/v1/runs", token=token, body={"intent": "x"})
    ).body["run_id"]
    # A second gateway process over the same database sees the run.
    seen = app2.handle(_req("GET", f"/v1/runs/{run_id}", token=token))
    assert seen.status == 200 and seen.body["run_id"] == run_id
    conn1.close()
    conn2.close()


def test_restart_preserves_runs(tmp_path):
    db = tmp_path / "restart.db"
    ident = _Identity(tmp_path)
    app1, conn1, _ = _app(tmp_path, ident=ident, path=db)
    token = ident.token("user-1")
    run_id = app1.handle(
        _req("POST", "/v1/runs", token=token, body={"intent": "x"})
    ).body["run_id"]
    conn1.close()  # process restart

    app2, conn2, _ = _app(tmp_path, ident=ident, path=db)
    assert app2.handle(_req("GET", f"/v1/runs/{run_id}", token=token)).status == 200
    conn2.close()


# --------------------------------------------------------------------------- #
# Quotas, rate limits, pagination.                                           #
# --------------------------------------------------------------------------- #
def test_rate_limit_returns_429(tmp_path):
    app, conn, ident = _app(
        tmp_path, config=GatewayConfig(rate_capacity=1, rate_refill_per_second=0)
    )
    token = ident.token("user-1")
    assert app.handle(_req("GET", "/v1/runs", token=token)).status == 200
    assert app.handle(_req("GET", "/v1/runs", token=token)).status == 429
    conn.close()


def test_quota_returns_429(tmp_path):
    app, conn, ident = _app(tmp_path, config=GatewayConfig(max_runs_per_tenant=1))
    token = ident.token("user-1")
    assert (
        app.handle(_req("POST", "/v1/runs", token=token, body={"intent": "x"})).status
        == 202
    )
    over = app.handle(_req("POST", "/v1/runs", token=token, body={"intent": "y"}))
    assert over.status == 429
    assert over.body["error"]["code"] == "quota_exceeded"
    conn.close()


def test_pagination(tmp_path):
    app, conn, ident = _app(tmp_path)
    token = ident.token("user-1")
    for _ in range(3):
        app.handle(_req("POST", "/v1/runs", token=token, body={"intent": "x"}))
    page1 = app.handle(
        _req("GET", "/v1/runs", token=token, query={"page": "1", "size": "2"})
    )
    page2 = app.handle(
        _req("GET", "/v1/runs", token=token, query={"page": "2", "size": "2"})
    )
    assert page1.body["total"] == 3
    assert len(page1.body["items"]) == 2
    assert len(page2.body["items"]) == 1
    conn.close()


# --------------------------------------------------------------------------- #
# RBAC + provider connections (no secret echoed).                             #
# --------------------------------------------------------------------------- #
def test_provider_connect_requires_permission_and_hides_secret(tmp_path):
    app, conn, ident = _app(tmp_path)
    body = {"provider": "openai", "secret": "sk-secret-xyz", "scopes": ["models"]}
    # A plain user lacks providers:manage.
    denied = app.handle(
        _req("POST", "/v1/provider-connections", token=ident.token("user-1"), body=body)
    )
    assert denied.status == 403
    # An admin may connect; the secret is never echoed.
    ok = app.handle(
        _req(
            "POST", "/v1/provider-connections", token=ident.token("admin-1"), body=body
        )
    )
    assert ok.status == 201
    assert "sk-secret-xyz" not in str(ok.body)
    listing = app.handle(
        _req("GET", "/v1/provider-connections", token=ident.token("admin-1"))
    )
    assert "sk-secret-xyz" not in str(listing.body)
    conn.close()


# --------------------------------------------------------------------------- #
# Clarification + approval flows.                                            #
# --------------------------------------------------------------------------- #
def test_clarification_flow(tmp_path):
    app, conn, ident = _app(tmp_path, scenario=_clarify)
    token = ident.token("user-1")
    run_id = app.handle(
        _req("POST", "/v1/runs", token=token, body={"intent": "clarify"})
    ).body["run_id"]
    questions = app.handle(_req("GET", f"/v1/runs/{run_id}/questions", token=token))
    assert [q["parameter"] for q in questions.body["questions"]] == ["size"]
    answered = app.handle(
        _req(
            "POST",
            f"/v1/runs/{run_id}/answers",
            token=token,
            body={"answers": {"size": "large"}},
        )
    )
    assert answered.body["awaiting"] == "confirmation"
    conn.close()


def test_approval_flow_and_unauthorized_approval(tmp_path):
    app, conn, ident = _app(tmp_path, scenario=_approval)
    requester = ident.token("user-1")
    run_id = app.handle(
        _req("POST", "/v1/runs", token=requester, body={"intent": "approve-me"})
    ).body["run_id"]
    # Confirm the write route.
    confirmed = app.handle(
        _req(
            "POST",
            f"/v1/runs/{run_id}/confirmation",
            token=requester,
            body={"approved": True},
        )
    )
    assert confirmed.body["awaiting"] == "approval"
    # A non-approver cannot approve.
    denied = app.handle(
        _req("POST", f"/v1/runs/{run_id}/approvals", token=ident.token("nobody"))
    )
    assert denied.status == 403
    # The authorized approver completes it.
    approved = app.handle(
        _req("POST", f"/v1/runs/{run_id}/approvals", token=ident.token("approver-1"))
    )
    assert approved.body["phase"] == "completed"
    conn.close()


# --------------------------------------------------------------------------- #
# SSE + audit export.                                                         #
# --------------------------------------------------------------------------- #
def test_event_stream_and_audit_export(tmp_path):
    app, conn, ident = _app(tmp_path)
    token = ident.token("user-1")
    run_id = app.handle(
        _req("POST", "/v1/runs", token=token, body={"intent": "x"})
    ).body["run_id"]
    events = app.handle(_req("GET", f"/v1/runs/{run_id}/events", token=token))
    assert events.content_type == "text/event-stream"
    assert "event:" in events.body
    audit = app.handle(_req("GET", f"/v1/runs/{run_id}/audit", token=token))
    assert audit.body["verified"] is True
    conn.close()


# --------------------------------------------------------------------------- #
# Verified, replay-protected webhooks.                                       #
# --------------------------------------------------------------------------- #
def test_webhook_signature_and_replay_protection():
    signer = WebhookSigner("wh-secret")
    verifier = WebhookVerifier("wh-secret", tolerance_seconds=300)
    payload = {"event": "run.completed", "run_id": "r1"}
    headers = signer.sign(payload, delivery_id="d-1", now=NOW)

    verifier.verify(payload, headers, now=NOW)  # first delivery: ok
    with pytest.raises(WebhookError):
        verifier.verify(payload, headers, now=NOW)  # replay of same id


def test_webhook_rejects_tampering_and_skew():
    signer = WebhookSigner("wh-secret")
    verifier = WebhookVerifier("wh-secret", tolerance_seconds=300)
    payload = {"event": "x"}
    headers = signer.sign(payload, delivery_id="d-2", now=NOW)
    # Tampered payload.
    with pytest.raises(WebhookError):
        verifier.verify({"event": "tampered"}, headers, now=NOW)
    # Stale timestamp (replay outside the window).
    from datetime import timedelta

    with pytest.raises(WebhookError):
        verifier.verify(payload, headers, now=NOW + timedelta(hours=1))


# --------------------------------------------------------------------------- #
# Metrics + unknown routes.                                                   #
# --------------------------------------------------------------------------- #
def test_metrics_and_unknown_route(tmp_path):
    app, conn, ident = _app(tmp_path)
    token = ident.token("user-1")
    app.handle(_req("GET", "/v1/runs", token=token))
    # Counters are the operator's: metrics:read gates the surface.
    assert app.handle(_req("GET", "/v1/metrics", token=token)).status == 403
    ident.store.add_role(
        Role(
            tenant_id="t1",
            name="monitoring",
            permissions=frozenset({"metrics:read"}),
        )
    )
    ident.store.add_grant(
        AuthorityGrant(
            tenant_id="t1",
            principal_id="user-1",
            role_name="monitoring",
            granted_by="x",
        )
    )
    metrics = app.handle(_req("GET", "/v1/metrics", token=token))
    assert metrics.body["requests"] >= 1
    assert metrics.body["uptime_seconds"] >= 0
    assert app.handle(_req("GET", "/v1/nope", token=token)).status == 404
    conn.close()
