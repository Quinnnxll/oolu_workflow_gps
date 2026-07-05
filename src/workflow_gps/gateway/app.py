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

import json
import random
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..billing import (
    BillingService,
    DisputeService,
    PayoutAdapter,
    PayoutStatus,
    PayoutStore,
)
from ..durable.idempotency import IdempotencyLedger
from ..durable.service import DurableWorkflowService
from ..identity.errors import AuthenticationError, AuthorizationError
from ..identity.models import Session
from ..identity.policy import AuthorityResolver
from ..identity.service import IdentityApprovalAuthority
from ..identity.sessions import default_assurance
from ..identity.tokens import OidcValidator
from ..knowledge.traces import TraceStore
from ..metering.attribution import AttributionStore
from ..nodeplace import (
    CandidateAssembler,
    ConsumerAccount,
    ContributionError,
    NodeplaceService,
    OwnershipError,
    PriceBook,
    PricingPolicy,
    QuoteEngine,
    QuoteMode,
    RatingError,
    RatingService,
    ReservedActionsError,
    StepCandidates,
    SubscriptionPlan,
    UnverifiedRunError,
    Visibility,
    build_run_binding,
    compile_runnable,
    execute_contract,
    preview_assembly,
    reward_multiplier,
    utility,
)
from ..orchestrator import (
    DagRouteRunner,
    GoalSpec,
    OrchestratorError,
)
from ..orchestrator.state import (
    PauseKind,
    Phase,
    ResumeInput,
    RunState,
    TaskContract,
)
from ..providers.vault import SecretVault
from ..skills.contract import NodeContract, Slot
from ..skills.models import ReusableSkill
from ..skills.ports import ActionExecutor
from .errors import GatewayError, WebhookError
from .http import (
    Request,
    Response,
    Router,
    apply_cors,
    json_response,
    with_security_headers,
)
from .openapi import build_openapi
from .webhooks import WebhookVerifier

_PAUSE_VALUE = {
    PauseKind.CLARIFICATION: "clarification",
    PauseKind.CONFIRMATION: "confirmation",
    PauseKind.APPROVAL: "approval",
    PauseKind.INCIDENT: "incident",
}

# The plan applied to /v1/market/quotes when the request names none. A
# documented money knob (like billing.policy), not a hidden default.
DEFAULT_QUOTE_PLAN = SubscriptionPlan(
    name="api-default",
    monthly_price=20.0,
    automation_cost_budget=6.0,
    included_cli_calls=1200,
    included_api_calls=400,
)


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
        market: CandidateAssembler | None = None,
        price_book: PriceBook | None = None,
        attribution: AttributionStore | None = None,
        contract_executors: dict[str, ActionExecutor] | None = None,
        trace_store: TraceStore | None = None,
        rng: random.Random | None = None,
        payout_store: PayoutStore | None = None,
        payout_adapter: PayoutAdapter | None = None,
        disputes: DisputeService | None = None,
        webhook_verifier: WebhookVerifier | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self._durable = durable
        self._validator = validator
        self._resolver = resolver
        self._approval = approval_authority
        self._nodeplace = nodeplace
        self._billing = billing
        self._ratings = ratings
        self._market = market
        self._price_book = price_book
        self._attribution = attribution
        self._contract_runner = (
            DagRouteRunner(contract_executors) if contract_executors else None
        )
        # Node-granular trace recording happens in execute_contract (per
        # contract child), not in the runner — attaching the store to the
        # runner too would double-count the whole-route outcome.
        self._trace_store = trace_store
        # Thompson sampling for explore-mode assembly; injectable so tests
        # (and reproducibility-minded operators) can seed it.
        self._rng = rng or random.Random()
        self._payout_store = payout_store
        self._payout_adapter = payout_adapter
        self._disputes = disputes
        self._webhook_verifier = webhook_verifier
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
        return self._session_for(request.bearer_token(), request.now or self._clock())

    def _session_for(self, token: str | None, now: datetime) -> Session:
        if not token:
            raise GatewayError(401, "unauthorized", "missing bearer token")
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

    # ------------------------------------------------------------------ #
    # Live event transport (ADR-0004).                                    #
    #                                                                     #
    # The gateway is transport-agnostic: it exposes the two operations a  #
    # live pushing transport (WebSocket over the ASGI binding) needs —    #
    # authorize a run stream, and read event frames after a sequence —    #
    # without knowing anything about sockets. The SSE ``_events`` handler #
    # and the WebSocket binding both consume ``run_event_frames``.        #
    # ------------------------------------------------------------------ #
    def authorize_stream(
        self, token: str | None, run_id: str, *, now: datetime | None = None
    ) -> RunState:
        """Authenticate a live-stream subscriber and tenant-guard the run.

        Mirrors the HTTP auth path (validated token → session, never trusted
        text) and the cross-tenant guard of ``_load`` (a run owned by another
        tenant is indistinguishable from a missing one). Raises
        :class:`GatewayError`; the ASGI binding maps its status onto a close code.
        """
        session = self._session_for(token, now or self._clock())
        return self._load(run_id, session)

    def run_event_frames(self, run_id: str, *, after_seq: int = 0) -> list[dict]:
        """Return audit-derived event frames for a run after ``after_seq``.

        Each frame carries the audit ``seq`` (the resumable cursor), the event
        type, the run's current ``phase``, and the entry timestamp. The durable
        audit stream is append-only, so ``after_seq`` yields only new frames —
        the increment a live transport pushes. Returns ``[]`` for an unknown run.
        """
        state = self._durable.get(run_id)
        if state is None:
            return []
        return [
            {
                "seq": r.seq,
                "event_type": r.event_type,
                "phase": state.phase.value,
                "at": r.at.isoformat(),
            }
            for r in self._durable.audit.records(run_id=run_id)
            if r.seq > after_seq
        ]

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
        r.add("GET", "/v1/market/candidates", self._market_candidates)
        r.add("POST", "/v1/market/quotes", self._market_quote)
        r.add("POST", "/v1/market/assemble", self._market_assemble)
        r.add("POST", "/v1/runs/contract", self._submit_contract_run)
        r.add("GET", "/v1/earnings", self._earnings_balance)
        r.add("GET", "/v1/earnings/entries", self._earnings_entries)
        r.add("GET", "/v1/payout-accounts", self._get_payout_account)
        r.add("POST", "/v1/payout-accounts", self._create_payout_account)
        r.add("GET", "/v1/disputes/{event_id}", self._list_disputes)
        r.add("POST", "/v1/webhooks/processor", self._processor_webhook, public=True)

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
        node_version_id = body.get("node_version_id")
        if node_version_id is not None and (
            self._market is None
            or self._price_book is None
            or self._attribution is None
        ):
            raise GatewayError(404, "not_found", "market economics are not enabled")
        tenant_runs = sum(
            1
            for s in self._durable.runs.list()
            if s.contract.metadata.get("tenant_id") == session.tenant_id
        )
        if tenant_runs >= self._config.max_runs_per_tenant:
            raise GatewayError(429, "quota_exceeded", "tenant run quota exceeded")
        max_recovery = int(body.get("max_recovery_attempts", 1))

        def submit() -> dict:
            # A marketplace run is priced and attributed BEFORE anything can
            # settle: assemble live economics, clear the price (committing —
            # a real run moves the market reference), and bind the run to its
            # shares. The exactly-once pipeline (metering deriver -> billing
            # -> ledger) turns the binding into earnings only if the audit
            # log later shows a platform-verified success for this run_id.
            entry = None
            if node_version_id is not None:
                entry = self._market.assemble_version(str(node_version_id))
                if entry is None:
                    raise GatewayError(
                        404,
                        "not_found",
                        f"no active public listing for version '{node_version_id}'",
                    )
            contract = TaskContract(
                intent=intent,
                submitted_by=session.principal_id,
                metadata={"tenant_id": session.tenant_id},
            )
            state = self._durable.submit(contract, max_recovery_attempts=max_recovery)
            self._metrics["runs_submitted"] += 1
            result = self._run_dict(state)
            if entry is not None:
                cleared = self._price_book.clear(
                    class_key=entry.candidate.class_key,
                    node_class=entry.candidate.node_class,
                    ask=entry.candidate.cleared_price,
                    cost=entry.candidate.cost,
                    substitutes=entry.signals.substitutes,
                )
                candidate = entry.candidate.model_copy(
                    update={"cleared_price": cleared.cleared}
                )
                binding = build_run_binding(
                    run_id=state.run_id,
                    consumer_tenant=session.tenant_id,
                    candidate=candidate,
                    signals=entry.signals,
                    # Royalty ancestors come from the version's recorded
                    # lineage — derivation provenance, not caller input.
                    ancestors=self._market.lineage_for(candidate.version_id),
                    consumer_principal=session.principal_id,
                )
                self._attribution.bind(binding)
                self._metrics["market_runs_bound"] += 1
                result["market"] = {
                    "version_id": candidate.version_id,
                    "gross": binding.gross,
                    "provider_cost": binding.provider_cost,
                    "cleared": cleared.model_dump(mode="json"),
                    "noders": [s.noder_principal for s in binding.shares],
                }
            return result

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
        # SSE snapshot: the polling fallback for the live WebSocket transport
        # (ADR-0004). Both render the same ``run_event_frames`` so a client can
        # switch between them without seeing a different event shape.
        state = self._load(params["run_id"], session)
        frames = [
            f"event: {frame['event_type']}\ndata: "
            + json.dumps({"seq": frame["seq"], "phase": frame["phase"]})
            + "\n"
            for frame in self.run_event_frames(state.run_id)
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
            raise GatewayError(
                400, "invalid_request", f"invalid contribution: {exc}"
            ) from exc
        pricing = None
        if isinstance(body.get("pricing"), dict):
            try:
                pricing = PricingPolicy.model_validate(
                    {**body["pricing"], "version_id": "pending"}
                )
            except Exception as exc:
                raise GatewayError(
                    400, "invalid_request", f"bad pricing: {exc}"
                ) from exc
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
                pricing=pricing,
                backend=str(body.get("backend", "docker")),
                requires_approval=bool(body.get("requires_approval", True)),
                derived_from=body.get("derived_from"),
                consumes=self._parse_slots(body.get("consumes")),
                produces=self._parse_slots(body.get("produces")),
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
            raise GatewayError(
                400, "invalid_request", "score must be an integer"
            ) from exc
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
                "items": [
                    r.model_dump(mode="json") for r in ratings.ratings(version_id)
                ],
                "reputation": ratings.reputation(version_id),
            },
        )

    # ------------------------------------------------------------------ #
    # Market economics: candidates + quotes from live production data.    #
    # ------------------------------------------------------------------ #
    def _require_market(self) -> tuple[CandidateAssembler, PriceBook]:
        if self._market is None or self._price_book is None:
            raise GatewayError(404, "not_found", "market economics are not enabled")
        return self._market, self._price_book

    @staticmethod
    def _parse_mode(raw: str) -> QuoteMode:
        try:
            return QuoteMode(raw)
        except ValueError as exc:
            valid = ", ".join(m.value for m in QuoteMode)
            raise GatewayError(
                400, "invalid_request", f"mode must be one of: {valid}"
            ) from exc

    def _market_candidates(self, request, session, params) -> Response:
        """Rank live candidates for a step. Read-only: never moves the book."""
        assembler, book = self._require_market()
        mode = self._parse_mode(request.query.get("mode", "standard"))
        try:
            days_elapsed = float(request.query.get("days_elapsed", 30.0))
        except ValueError as exc:
            raise GatewayError(
                400, "invalid_request", "days_elapsed must be a number"
            ) from exc

        items = []
        for entry in assembler.assemble(request.query.get("q", "")):
            cleared = book.clear(
                class_key=entry.candidate.class_key,
                node_class=entry.candidate.node_class,
                ask=entry.candidate.cleared_price,
                cost=entry.candidate.cost,
                substitutes=entry.signals.substitutes,
                days_elapsed=days_elapsed,
                commit=False,  # browsing must not shift market state
            )
            candidate = entry.candidate.model_copy(
                update={"cleared_price": cleared.cleared}
            )
            items.append(
                {
                    "listing_id": entry.listing_id,
                    "title": entry.title,
                    "tags": entry.tags,
                    "utility": utility(candidate, mode),
                    "candidate": candidate.model_dump(mode="json"),
                    "cleared": cleared.model_dump(mode="json"),
                    "signals": entry.signals.model_dump(mode="json"),
                    "reward_multiplier": reward_multiplier(entry.signals).multiplier,
                }
            )
        items.sort(key=lambda item: item["utility"], reverse=True)
        return json_response(200, {"mode": mode.value, "items": items})

    @staticmethod
    def _parse_slots(raw) -> list[Slot] | None:
        if raw is None:
            return None
        if not isinstance(raw, list):
            raise GatewayError(400, "invalid_request", "slots must be a list")
        try:
            return [Slot.model_validate(item) for item in raw]
        except Exception as exc:
            raise GatewayError(400, "invalid_request", f"bad slot: {exc}") from exc

    def _market_assemble(self, request, session, params) -> Response:
        """Goal in, assembled marketplace workflow out — a planning preview.

        Backward-chains the wanted slots through the marketplace's slot
        vocabularies. Read-only: prices preview without moving the book, and
        payout previews use the same lineage-aware split settlement will.
        """
        assembler, book = self._require_market()
        body = request.body or {}
        if not isinstance(body.get("goal"), dict):
            raise GatewayError(
                400, "invalid_request", "a goal object with name and want is required"
            )
        try:
            goal = GoalSpec.model_validate(body["goal"])
        except Exception as exc:
            raise GatewayError(400, "invalid_request", f"bad goal: {exc}") from exc
        if not goal.want:
            raise GatewayError(400, "invalid_request", "goal.want must not be empty")

        preview = preview_assembly(
            assembler,
            book,
            goal,
            query=str(body.get("q", "")),
            fill_gaps=bool(body.get("fill_gaps", False)),
            # Picks carry the tenant's own confirmed-run history on top of
            # platform-verified counts — personalized per tenant bucket.
            trace_store=self._trace_store,
            trace_context=session.tenant_id,
            # explore: Thompson-sample producer picks from those posteriors
            # instead of taking the greedy best — opt-in per request.
            rng=self._rng if bool(body.get("explore", False)) else None,
        )
        return json_response(200, preview.model_dump(mode="json"))

    def _submit_contract_run(self, request, session, params) -> Response:
        """Execute an assembled contract directly, with multi-node binding.

        The counterpart to ``/v1/market/assemble``: post the contract it
        returned and this compiles it to a DAG blueprint, binds every
        marketplace node in it to the run (one aggregate ``RunBinding`` whose
        shares merge each node's lineage split, weighted by its cleared
        price — a real run, so prices commit), executes it on the configured
        executors, and appends the outcome to the durable audit log — the
        same event the metering deriver pays from on verified success.

        Human control stays intact: a contract containing reserved actions is
        refused here and must go through the orchestrator's approval flow.
        """
        if self._contract_runner is None:
            raise GatewayError(404, "not_found", "contract execution is not enabled")
        assembler, book = self._require_market()
        if self._attribution is None:
            raise GatewayError(404, "not_found", "market economics are not enabled")
        body = request.body or {}
        if not isinstance(body.get("contract"), dict):
            raise GatewayError(400, "invalid_request", "a contract object is required")
        try:
            contract = NodeContract.model_validate(body["contract"])
        except Exception as exc:
            raise GatewayError(400, "invalid_request", f"bad contract: {exc}") from exc
        try:
            compiled = compile_runnable(contract)
        except ReservedActionsError as exc:
            raise GatewayError(403, "forbidden", str(exc)) from exc
        except ValueError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc

        def submit() -> dict:
            result = execute_contract(
                contract,
                compiled,
                runner=self._contract_runner,
                assembler=assembler,
                price_book=book,
                attribution=self._attribution,
                audit=self._durable.audit,
                consumer_tenant=session.tenant_id,
                consumer_principal=session.principal_id,
                trace_store=self._trace_store,
                trace_context=session.tenant_id,
            )
            self._metrics["contract_runs"] += 1
            return result.model_dump(mode="json")

        key = request.header("idempotency-key")
        result = (
            self._idem.run(
                f"gw:contract:{session.tenant_id}:{key}", submit, scope="gateway"
            )
            if key
            else submit()
        )
        return json_response(200, result)

    def _market_quote(self, request, session, params) -> Response:
        """Quote a workflow off live economics. A forecast: no money moves,
        and (by default) the price book's references are not committed."""
        assembler, book = self._require_market()
        body = request.body or {}
        mode = self._parse_mode(str(body.get("mode", "standard")))

        raw_steps = body.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise GatewayError(400, "invalid_request", "steps must be a non-empty list")

        plan = DEFAULT_QUOTE_PLAN
        if isinstance(body.get("plan"), dict):
            try:
                plan = SubscriptionPlan.model_validate(body["plan"])
            except Exception as exc:
                raise GatewayError(400, "invalid_request", f"bad plan: {exc}") from exc

        steps: list[StepCandidates] = []
        for raw in raw_steps:
            if not isinstance(raw, dict) or not raw.get("name"):
                raise GatewayError(
                    400, "invalid_request", "each step needs at least a name"
                )
            assembled = assembler.assemble(str(raw.get("q", raw["name"])))
            if not assembled:
                raise GatewayError(
                    404, "not_found", f"no candidates found for step '{raw['name']}'"
                )
            steps.append(
                StepCandidates(
                    name=str(raw["name"]),
                    candidates=[entry.candidate for entry in assembled],
                    signals={
                        entry.candidate.version_id: entry.signals for entry in assembled
                    },
                    cli_calls=int(raw.get("cli_calls", 0)),
                    api_calls=int(raw.get("api_calls", 0)),
                    vendor=raw.get("vendor"),
                    minutes_saved=float(raw.get("minutes_saved", 0.0)),
                )
            )

        account = ConsumerAccount(user_id=session.principal_id, plan=plan)
        quote = QuoteEngine(book).quote(
            account,
            steps,
            mode=mode,
            commit_prices=bool(body.get("commit_prices", False)),
        )
        return json_response(200, quote.model_dump(mode="json"))

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

    def _create_payout_account(self, request, session, params) -> Response:
        if self._payout_store is None or self._payout_adapter is None:
            raise GatewayError(404, "not_found", "payout accounts are not enabled")
        body = request.body or {}
        account = self._payout_adapter.create_account(
            noder_principal=session.principal_id,
            country=str(body.get("country", "US")),
            currency=str(body.get("currency", "usd")),
        )
        self._payout_store.save_account(account)
        return json_response(201, account.model_dump(mode="json"))

    def _get_payout_account(self, request, session, params) -> Response:
        if self._payout_store is None:
            raise GatewayError(404, "not_found", "payout accounts are not enabled")
        account = self._payout_store.get_account(session.principal_id)
        if account is None:
            raise GatewayError(404, "not_found", "no payout account for this principal")
        return json_response(200, account.model_dump(mode="json"))

    def _list_disputes(self, request, session, params) -> Response:
        if self._disputes is None:
            raise GatewayError(404, "not_found", "disputes are not enabled")
        disputes = self._disputes.for_event(params["event_id"])
        return json_response(
            200, {"items": [d.model_dump(mode="json") for d in disputes]}
        )

    def _processor_webhook(self, request, session, params) -> Response:
        if self._webhook_verifier is None or self._disputes is None:
            raise GatewayError(404, "not_found", "processor webhooks are not enabled")
        body = request.body or {}
        headers = {
            "X-Webhook-Id": request.header("x-webhook-id"),
            "X-Webhook-Timestamp": request.header("x-webhook-timestamp"),
            "X-Webhook-Signature": request.header("x-webhook-signature"),
        }
        try:
            self._webhook_verifier.verify(
                body, headers, now=request.now or self._clock()
            )
        except WebhookError as exc:
            raise GatewayError(400, "invalid_webhook", str(exc)) from exc

        def process() -> dict:
            event_type = body.get("type", "")
            result: dict = {"handled": event_type}
            if event_type in ("charge.refunded", "charge.dispute.created"):
                event_id = body.get("event_id")
                self._disputes.refund(event_id=event_id, reason=event_type)
                result["clawback_event_id"] = event_id
            elif event_type in ("payout.paid", "payout.failed") and self._payout_store:
                batch = self._payout_store.get_batch(body.get("batch_id", ""))
                if batch is not None:
                    status = (
                        PayoutStatus.PAID
                        if event_type == "payout.paid"
                        else PayoutStatus.FAILED
                    )
                    self._payout_store.update_batch(
                        batch.model_copy(
                            update={
                                "status": status,
                                "provider_ref": body.get("provider_ref"),
                            }
                        )
                    )
                    result["batch_id"] = batch.batch_id
            return result

        result = self._idem.run(
            f"webhook:{headers['X-Webhook-Id']}", process, scope="webhooks"
        )
        return json_response(200, result)

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
