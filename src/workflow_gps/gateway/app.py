"""The HTTP gateway application: a private control-plane prototype.

A framework-agnostic application over :class:`Request`/:class:`Response`. Every
non-public route requires an OIDC bearer token (validated, never trusted as text),
is scoped to the caller's tenant, and is subject to per-tenant rate limits and
quotas; mutating submissions are idempotent. Run submission is asynchronous — it
returns ``202`` with a run id, and progress is read via status, an SSE event
stream, or the audit export — so a long model run is never a synchronous request.
The gateway sits on the durable runtime, so two gateway processes over the same
database see one consistent set of runs.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..billing import BillingService
from ..durable.idempotency import IdempotencyLedger
from ..durable.service import DurableWorkflowService
from ..identity.errors import AuthenticationError, AuthorizationError
from ..identity.models import Session
from ..identity.policy import AuthorityResolver
from ..identity.service import IdentityApprovalAuthority
from ..identity.sessions import default_assurance
from ..identity.tokens import OidcValidator
from ..nodeplace import (
    ContributionError,
    NodeplaceService,
    OwnershipError,
    RatingError,
    RatingService,
    UnverifiedRunError,
    Visibility,
)
from ..orchestrator import OrchestratorError
from ..orchestrator.state import PauseKind, Phase, ResumeInput, RunState, TaskContract
from ..providers.vault import SecretVault
from ..skills.models import ReusableSkill
from .errors import GatewayError
from .http import (
    Request,
    Response,
    Router,
    apply_cors,
    json_response,
    with_security_headers,
)
from .openapi import build_openapi

_PAUSE_VALUE = {
    PauseKind.CLARIFICATION: "clarification",
    PauseKind.CONFIRMATION: "confirmation",
    PauseKind.APPROVAL: "approval",
    PauseKind.INCIDENT: "incident",
}


@dataclass(frozen=True)
class GatewayConfig:
    allowed_origins: frozenset[str] = field(default_factory=frozenset)
    rate_capacity: float = 1000.0
    rate_refill_per_second: float = 1000.0
    max_runs_per_tenant: int = 10_000
    page_size_default: int = 20
    page_size_max: int = 100


class _TokenBucket:
    def __init__(self, capacity: float, refill_per_second: float):
        self._capacity = capacity
        self._refill = refill_per_second
        self._tokens = capacity
        self._updated: float | None = None

    def allow(self, *, now: datetime) -> bool:
        ts = now.timestamp()
        if self._updated is None:
            self._updated = ts
        elapsed = max(0.0, ts - self._updated)
        self._tokens = min(self._capacity, self._tokens + elapsed * self._refill)
        self._updated = ts
        if self._tokens < 1.0:
            return False
        self._tokens -= 1.0
        return True


class GatewayApp:
    def __init__(
        self,
        durable: DurableWorkflowService,
        *,
        validator: OidcValidator,
        resolver: AuthorityResolver,
        approval_authority: IdentityApprovalAuthority | None = None,
        vault: SecretVault | None = None,
        config: GatewayConfig | None = None,
        idempotency: IdempotencyLedger | None = None,
        nodeplace: NodeplaceService | None = None,
        billing: BillingService | None = None,
        ratings: RatingService | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self._durable = durable
        self._validator = validator
        self._resolver = resolver
        self._approval = approval_authority
        self._nodeplace = nodeplace
        self._billing = billing
        self._ratings = ratings
        self._vault = vault or SecretVault()
        self._config = config or GatewayConfig()
        self._idem = idempotency or durable.idempotency
        self._clock = clock or (lambda: datetime.now(UTC))
        self._buckets: dict[str, _TokenBucket] = {}
        self._connections: dict[str, dict[str, dict]] = defaultdict(dict)
        self._metrics: dict[str, int] = defaultdict(int)
        self._router = Router()
        self._register_routes()

    # ------------------------------------------------------------------ #
    # Entry point.                                                        #
    # ------------------------------------------------------------------ #
    def handle(self, request: Request) -> Response:
        self._metrics["requests"] += 1
        try:
            response = self._route(request)
        except GatewayError as exc:
            self._metrics["errors"] += 1
            response = json_response(
                exc.status, {"error": {"code": exc.code, "message": exc.message}}
            )
        return apply_cors(
            with_security_headers(response), request, self._config.allowed_origins
        )

    def _route(self, request: Request) -> Response:
        if request.method == "OPTIONS":
            return Response(status=204, body=None)
        match = self._router.match(request.method, request.path)
        if match is None:
            allowed = self._router.allowed_methods(request.path)
            if allowed:
                raise GatewayError(405, "method_not_allowed", "method not allowed")
            raise GatewayError(404, "not_found", "resource not found")
        route, params = match
        session: Session | None = None
        if not route.public:
            session = self._authenticate(request)
            self._enforce_rate_limit(session, request)
            if route.requires_permission and not self._resolver.has_permission(
                session, route.requires_permission
            ):
                raise GatewayError(403, "forbidden", "insufficient authority")
        return route.handler(request, session, params)

    # ------------------------------------------------------------------ #
    # Middleware.                                                         #
    # ------------------------------------------------------------------ #
    def _authenticate(self, request: Request) -> Session:
        token = request.bearer_token()
        if not token:
            raise GatewayError(401, "unauthorized", "missing bearer token")
        now = request.now or self._clock()
        try:
            claims = self._validator.validate(token, now=now)
        except AuthenticationError as exc:
            raise GatewayError(401, "unauthorized", str(exc)) from exc
        return Session(
            principal_id=claims.subject,
            principal_kind=claims.principal_kind,
            tenant_id=claims.tenant_id,
            issued_at=now,
            expires_at=claims.expires_at,
            assurance_level=default_assurance(claims),
            amr=list(claims.amr),
            source_issuer=claims.issuer,
        )

    def _enforce_rate_limit(self, session: Session, request: Request) -> None:
        bucket = self._buckets.setdefault(
            session.tenant_id,
            _TokenBucket(
                self._config.rate_capacity, self._config.rate_refill_per_second
            ),
        )
        if not bucket.allow(now=request.now or self._clock()):
            raise GatewayError(429, "rate_limited", "rate limit exceeded")

    # ------------------------------------------------------------------ #
    # Routes.                                                             #
    # ------------------------------------------------------------------ #
    def _register_routes(self) -> None:
        r = self._router
        r.add("GET", "/v1/openapi.json", self._openapi, public=True)
        r.add("GET", "/v1/health", self._health, public=True)
        r.add("POST", "/v1/runs", self._submit_run)
        r.add("GET", "/v1/runs", self._list_runs)
        r.add("GET", "/v1/runs/{run_id}", self._get_run)
        r.add("GET", "/v1/runs/{run_id}/questions", self._questions)
        r.add("POST", "/v1/runs/{run_id}/answers", self._answers)
        r.add("GET", "/v1/runs/{run_id}/route", self._route_preview)
        r.add("POST", "/v1/runs/{run_id}/confirmation", self._confirm)
        r.add("GET", "/v1/runs/{run_id}/approvals", self._approvals)
        r.add("POST", "/v1/runs/{run_id}/approvals", self._approve)
        r.add("GET", "/v1/runs/{run_id}/incidents", self._incidents)
        r.add("POST", "/v1/runs/{run_id}/incidents", self._resolve_incident)
        r.add("POST", "/v1/runs/{run_id}/cancel", self._cancel)
        r.add("POST", "/v1/runs/{run_id}/feedback", self._feedback)
        r.add("GET", "/v1/runs/{run_id}/audit", self._audit)
        r.add("GET", "/v1/runs/{run_id}/events", self._events)
        r.add("GET", "/v1/provider-connections", self._list_connections)
        r.add(
            "POST",
            "/v1/provider-connections",
            self._connect_provider,
            requires_permission="providers:manage",
        )
        r.add("GET", "/v1/metrics", self._metrics_endpoint)
        r.add("GET", "/v1/nodeplace", self._list_own_nodes)
        r.add("POST", "/v1/nodeplace", self._contribute)
        r.add("POST", "/v1/nodeplace/{node_id}/revoke", self._revoke_node)
        r.add("GET", "/v1/listings", self._discover_listings)
        r.add("POST", "/v1/listings/{listing_id}/publish", self._publish_listing)
        r.add("POST", "/v1/versions/{version_id}/ratings", self._rate_version)
        r.add("GET", "/v1/versions/{version_id}/ratings", self._list_ratings)
        r.add("GET", "/v1/earnings", self._earnings_balance)
        r.add("GET", "/v1/earnings/entries", self._earnings_entries)

    # ------------------------------------------------------------------ #
    # Handlers.                                                           #
    # ------------------------------------------------------------------ #
    def _openapi(self, request, session, params) -> Response:
        return json_response(200, build_openapi())

    def _health(self, request, session, params) -> Response:
        return json_response(200, {"status": "ok"})

    def _submit_run(self, request, session, params) -> Response:
        body = request.body or {}
        intent = body.get("intent")
        if not intent:
            raise GatewayError(400, "invalid_request", "intent is required")
        tenant_runs = sum(
            1
            for s in self._durable.runs.list()
            if s.contract.metadata.get("tenant_id") == session.tenant_id
        )
        if tenant_runs >= self._config.max_runs_per_tenant:
            raise GatewayError(429, "quota_exceeded", "tenant run quota exceeded")
        max_recovery = int(body.get("max_recovery_attempts", 1))

        def submit() -> dict:
            contract = TaskContract(
                intent=intent,
                submitted_by=session.principal_id,
                metadata={"tenant_id": session.tenant_id},
            )
            state = self._durable.submit(contract, max_recovery_attempts=max_recovery)
            self._metrics["runs_submitted"] += 1
            return self._run_dict(state)

        key = request.header("idempotency-key")
        result = (
            self._idem.run(f"gw:{session.tenant_id}:{key}", submit, scope="gateway")
            if key
            else submit()
        )
        # 202 Accepted: submission is asynchronous; poll status or the event stream.
        return json_response(202, result)

    def _list_runs(self, request, session, params) -> Response:
        page = max(1, int(request.query.get("page", "1")))
        size = min(
            self._config.page_size_max,
            max(1, int(request.query.get("size", str(self._config.page_size_default)))),
        )
        runs = [
            s
            for s in self._durable.runs.list(limit=10_000)
            if s.contract.metadata.get("tenant_id") == session.tenant_id
        ]
        start = (page - 1) * size
        window = runs[start : start + size]
        return json_response(
            200,
            {
                "items": [self._run_dict(s) for s in window],
                "page": page,
                "size": size,
                "total": len(runs),
            },
        )

    def _get_run(self, request, session, params) -> Response:
        return json_response(200, self._run_dict(self._load(params["run_id"], session)))

    def _questions(self, request, session, params) -> Response:
        state = self._load(params["run_id"], session)
        questions = []
        if (
            state.pause is not None
            and state.pause.kind is PauseKind.CLARIFICATION
            and state.compilation is not None
        ):
            questions = [
                {
                    "parameter": q.parameter,
                    "question": q.question,
                    "suggested_values": list(q.suggested_values),
                    "priority": q.priority,
                }
                for q in state.compilation.questions
            ]
        return json_response(200, {"run_id": state.run_id, "questions": questions})

    def _answers(self, request, session, params) -> Response:
        body = request.body or {}
        answers = body.get("answers", {})
        state = self._resume(
            params["run_id"],
            session,
            ResumeInput(kind=PauseKind.CLARIFICATION, answers=answers),
        )
        return json_response(200, self._run_dict(state))

    def _route_preview(self, request, session, params) -> Response:
        state = self._load(params["run_id"], session)
        if state.route is None:
            return json_response(200, {"run_id": state.run_id, "route": None})
        route = state.route
        return json_response(
            200,
            {
                "run_id": state.run_id,
                "chosen": route.chosen.name,
                "total_cost": route.total_cost,
                "reserved_actions": route.reserved_action_ids,
                "exclusions": [
                    {"name": bp.name, "reason": bp.exclusion_reason}
                    for bp in [route.chosen, *route.alternatives]
                    if bp.excluded
                ],
            },
        )

    def _confirm(self, request, session, params) -> Response:
        body = request.body or {}
        state = self._resume(
            params["run_id"],
            session,
            ResumeInput(
                kind=PauseKind.CONFIRMATION, confirmed=bool(body.get("approved", False))
            ),
        )
        return json_response(200, self._run_dict(state))

    def _approvals(self, request, session, params) -> Response:
        state = self._load(params["run_id"], session)
        hc = state.human_control
        return json_response(
            200,
            {
                "run_id": state.run_id,
                "required": hc.approvers_required if hc else 0,
                "granted": len(state.granted_approvals),
            },
        )

    def _approve(self, request, session, params) -> Response:
        if self._approval is None:
            raise GatewayError(501, "not_implemented", "approvals are not configured")
        state = self._load(params["run_id"], session)
        if state.pause is None or state.pause.kind is not PauseKind.APPROVAL:
            raise GatewayError(409, "conflict", "run is not awaiting approval")
        policy = state.route.chosen.name if state.route else "execute"
        try:
            record = self._approval.approve(
                session,
                run_id=state.run_id,
                policy=policy,
                requester_id=state.contract.submitted_by,
                now=request.now or self._clock(),
            )
        except AuthorizationError as exc:
            raise GatewayError(403, "forbidden", str(exc)) from exc
        state = self._resume(
            params["run_id"],
            session,
            ResumeInput(kind=PauseKind.APPROVAL, approvals=[record]),
        )
        return json_response(200, self._run_dict(state))

    def _incidents(self, request, session, params) -> Response:
        state = self._load(params["run_id"], session)
        return json_response(
            200,
            {
                "run_id": state.run_id,
                "incidents": [
                    {"id": i.id, "reason": i.reason, "resolution": i.resolution}
                    for i in state.incidents
                ],
            },
        )

    def _resolve_incident(self, request, session, params) -> Response:
        body = request.body or {}
        state = self._resume(
            params["run_id"],
            session,
            ResumeInput(
                kind=PauseKind.INCIDENT, incident_decision=body.get("decision", "abort")
            ),
        )
        return json_response(200, self._run_dict(state))

    def _cancel(self, request, session, params) -> Response:
        state = self._load(params["run_id"], session)
        if not state.is_terminal:
            state.phase = Phase.CANCELLED
            state.failure_reason = "cancelled via gateway"
            state.pause = None
            state.updated_at = self._clock()
            self._durable.runs.save(state)
            self._durable.audit.append("workflow.cancelled", {"run_id": state.run_id})
        return json_response(200, self._run_dict(state))

    def _feedback(self, request, session, params) -> Response:
        state = self._load(params["run_id"], session)
        self._durable.audit.append(
            "feedback.received",
            {"run_id": state.run_id, "by": session.principal_id},
        )
        return json_response(202, {"run_id": state.run_id, "status": "recorded"})

    def _audit(self, request, session, params) -> Response:
        self._load(params["run_id"], session)  # tenant guard
        history = self._durable.reconstruct_history(params["run_id"])
        return json_response(
            200,
            {
                "run_id": params["run_id"],
                "verified": bool(history["audit_verified"]),
                "entries": [
                    {"seq": r.seq, "event_type": r.event_type, "at": r.at.isoformat()}
                    for r in history["audit"]
                ],
            },
        )

    def _events(self, request, session, params) -> Response:
        state = self._load(params["run_id"], session)
        records = self._durable.audit.records(run_id=state.run_id)
        frames = [
            f"event: {r.event_type}\ndata: "
            + f'{{"seq": {r.seq}, "phase": "{state.phase.value}"}}\n'
            for r in records
        ]
        return Response(
            status=200, body="\n".join(frames) + "\n", content_type="text/event-stream"
        )

    def _list_connections(self, request, session, params) -> Response:
        connections = self._connections.get(session.tenant_id, {})
        return json_response(
            200,
            {
                "items": [
                    {
                        "connection_id": cid,
                        "provider": data["provider"],
                        "status": data["status"],
                        "scopes": data["scopes"],
                    }
                    for cid, data in connections.items()
                ]
            },
        )

    def _connect_provider(self, request, session, params) -> Response:
        body = request.body or {}
        provider = body.get("provider")
        secret = body.get("secret")
        if not provider or not secret:
            raise GatewayError(400, "invalid_request", "provider and secret required")
        from uuid import uuid4

        ref = self._vault.put(secret, kind=f"{provider}_credential")
        cid = uuid4().hex
        self._connections[session.tenant_id][cid] = {
            "provider": provider,
            "status": "connected",
            "scopes": list(body.get("scopes", [])),
            "credential_ref_id": ref.ref_id,
        }
        # The secret is never echoed back.
        return json_response(
            201,
            {"connection_id": cid, "provider": provider, "status": "connected"},
        )

    def _metrics_endpoint(self, request, session, params) -> Response:
        return json_response(200, dict(self._metrics))

    # ------------------------------------------------------------------ #
    # Nodeplace (supply side) + display-only earnings.                   #
    # ------------------------------------------------------------------ #
    def _require_nodeplace(self) -> NodeplaceService:
        if self._nodeplace is None:
            raise GatewayError(404, "not_found", "nodeplace is not enabled")
        return self._nodeplace

    def _require_billing(self) -> BillingService:
        if self._billing is None:
            raise GatewayError(404, "not_found", "earnings are not enabled")
        return self._billing

    def _contribute(self, request, session, params) -> Response:
        nodeplace = self._require_nodeplace()
        body = request.body or {}
        try:
            skill = ReusableSkill.model_validate(body["skill"])
            visibility = Visibility(body.get("visibility", "public"))
        except (KeyError, ValueError, TypeError) as exc:
            raise GatewayError(400, "invalid_request", f"invalid contribution: {exc}") from exc
        try:
            result = nodeplace.contribute(
                noder_principal=session.principal_id,
                tenant_id=session.tenant_id,
                skill=skill,
                semver=str(body.get("semver", "1.0.0")),
                title=str(body.get("title", skill.name)),
                summary=str(body.get("summary", skill.description)),
                tags=list(body.get("tags", [])),
                license=str(body.get("license", "proprietary")),
                visibility=visibility,
                backend=str(body.get("backend", "docker")),
                requires_approval=bool(body.get("requires_approval", True)),
            )
        except ContributionError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        except OwnershipError as exc:
            raise GatewayError(403, "forbidden", str(exc)) from exc
        return json_response(
            201,
            {
                "node_id": result.node.node_id,
                "version_id": result.version.version_id,
                "listing_id": result.listing.listing_id,
                "content_hash": result.version.content_hash,
                "visibility": result.node.visibility.value,
            },
        )

    def _list_own_nodes(self, request, session, params) -> Response:
        nodeplace = self._require_nodeplace()
        nodes = nodeplace.list_own_nodes(
            noder_principal=session.principal_id, tenant_id=session.tenant_id
        )
        return json_response(200, {"items": [n.model_dump(mode="json") for n in nodes]})

    def _revoke_node(self, request, session, params) -> Response:
        nodeplace = self._require_nodeplace()
        try:
            revoked = nodeplace.revoke(
                params["node_id"],
                noder_principal=session.principal_id,
                tenant_id=session.tenant_id,
            )
        except OwnershipError as exc:
            raise GatewayError(403, "forbidden", str(exc)) from exc
        except ContributionError as exc:
            raise GatewayError(404, "not_found", str(exc)) from exc
        return json_response(200, {"revoked": revoked})

    def _discover_listings(self, request, session, params) -> Response:
        nodeplace = self._require_nodeplace()
        listings = nodeplace.discover(request.query.get("q", ""))
        return json_response(
            200, {"items": [listing.model_dump(mode="json") for listing in listings]}
        )

    def _publish_listing(self, request, session, params) -> Response:
        nodeplace = self._require_nodeplace()
        try:
            listing = nodeplace.publish(
                params["listing_id"],
                noder_principal=session.principal_id,
                tenant_id=session.tenant_id,
            )
        except OwnershipError as exc:
            raise GatewayError(403, "forbidden", str(exc)) from exc
        except ContributionError as exc:
            raise GatewayError(404, "not_found", str(exc)) from exc
        return json_response(200, listing.model_dump(mode="json"))

    def _require_ratings(self) -> RatingService:
        if self._ratings is None:
            raise GatewayError(404, "not_found", "ratings are not enabled")
        return self._ratings

    def _rate_version(self, request, session, params) -> Response:
        ratings = self._require_ratings()
        body = request.body or {}
        try:
            score = int(body.get("score"))
        except (TypeError, ValueError) as exc:
            raise GatewayError(400, "invalid_request", "score must be an integer") from exc
        try:
            rating = ratings.rate(
                rater_principal=session.principal_id,
                version_id=params["version_id"],
                score=score,
                text=str(body.get("text", "")),
            )
        except UnverifiedRunError as exc:
            raise GatewayError(403, "forbidden", str(exc)) from exc
        except RatingError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        return json_response(201, rating.model_dump(mode="json"))

    def _list_ratings(self, request, session, params) -> Response:
        ratings = self._require_ratings()
        version_id = params["version_id"]
        return json_response(
            200,
            {
                "items": [r.model_dump(mode="json") for r in ratings.ratings(version_id)],
                "reputation": ratings.reputation(version_id),
            },
        )

    def _earnings_balance(self, request, session, params) -> Response:
        billing = self._require_billing()
        return json_response(
            200, billing.balance(session.principal_id).model_dump(mode="json")
        )

    def _earnings_entries(self, request, session, params) -> Response:
        billing = self._require_billing()
        entries = billing.entries(session.principal_id)
        return json_response(
            200, {"items": [entry.model_dump(mode="json") for entry in entries]}
        )

    # ------------------------------------------------------------------ #
    # Helpers.                                                            #
    # ------------------------------------------------------------------ #
    def _load(self, run_id: str, session: Session) -> RunState:
        state = self._durable.get(run_id)
        # Cross-tenant access returns 404, never leaking another tenant's runs.
        if (
            state is None
            or state.contract.metadata.get("tenant_id") != session.tenant_id
        ):
            raise GatewayError(404, "not_found", "run not found")
        return state

    def _resume(self, run_id: str, session: Session, resume: ResumeInput) -> RunState:
        self._load(run_id, session)  # tenant guard before mutating
        try:
            return self._durable.resume(run_id, resume)
        except OrchestratorError as exc:
            raise GatewayError(409, "conflict", str(exc)) from exc

    def _run_dict(self, state: RunState) -> dict:
        return {
            "run_id": state.run_id,
            "intent": state.intent,
            "phase": state.phase.value,
            "awaiting": _PAUSE_VALUE[state.pause.kind] if state.pause else None,
            "prompt": state.pause.prompt if state.pause else None,
            "failure_reason": state.failure_reason,
            "result": state.result,
        }
