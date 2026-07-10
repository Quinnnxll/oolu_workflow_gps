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
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from html import escape as _escape
from uuid import uuid4

from pydantic import ValidationError

from ..billing import (
    BillingService,
    DisputeService,
    PaymentError,
    PaymentMethodsService,
    PayoutAdapter,
    PayoutStatus,
    PayoutStore,
)
from ..billing.launch import LaunchGuard
from ..billing.subscription import SubscriptionError, SubscriptionService
from ..chat import (
    ChatAssistant,
    GatewayChatTools,
    ModelBudgetExceeded,
    ModelUnavailable,
    NodeChatTools,
    author_node_function,
    mood_directive,
    obviously_chat,
)
from ..durable.files import (
    FileTooLargeError,
    UserFile,
    UserFileStore,
    normalize_folder,
)
from ..durable.idempotency import IdempotencyLedger
from ..durable.service import DurableWorkflowService
from ..identity.apikeys import KEY_PREFIX, ApiKeyError, ApiKeyService, scope_allows
from ..identity.errors import AuthenticationError, AuthorizationError
from ..identity.google_signin import (
    GoogleSignIn,
    IdentityLinkStore,
    SignInError,
    username_from_email,
)
from ..identity.models import PrincipalKind, Session
from ..identity.policy import AuthorityResolver
from ..identity.service import IdentityApprovalAuthority
from ..identity.sessions import default_assurance
from ..identity.tokens import OidcValidator
from ..knowledge.traces import TraceStore
from ..metering.attribution import AttributionStore
from ..nodeplace import (
    NODE_POLICY,
    NODE_POLICY_VERSION,
    BudgetExceededError,
    BudgetPolicy,
    CandidateAssembler,
    ConsumerAccount,
    ContributionError,
    NodeplaceService,
    OwnershipError,
    PendingContractRecord,
    PendingContractStore,
    PriceBook,
    PricingPolicy,
    QuoteEngine,
    QuoteMode,
    RatingError,
    RatingService,
    ReviewRequiredError,
    StepCandidates,
    SubscriptionPlan,
    SubscriptionRequired,
    UnverifiedRunError,
    Visibility,
    WorkDesk,
    assess_budget,
    build_run_binding,
    compile_contract,
    enforce_budget,
    estimate_contract_gross,
    execute_contract,
    preview_assembly,
    reserved_operations,
    reward_multiplier,
    utility,
)
from ..orchestrator import (
    DagRouteRunner,
    GoalSpec,
    OrchestratorError,
    patch_or_defaults,
)
from ..naming import concise_name
from ..orchestrator.rebuild import AUTOBUILD_CONSENT_KEY, AUTOBUILD_HINT
from ..orchestrator.state import (
    PauseKind,
    Phase,
    ResumeInput,
    RunState,
    TaskContract,
)
from ..providers.chatmodel import ChatModelRouter
from ..providers.keyring import PROVIDERS, ModelKeyring
from ..providers.vault import SecretVault
from ..settings_node import SettingError, SettingsNode
from ..skills.contract import NodeContract, Slot, SubgraphBody
from ..skills.inputs import bind_inputs, inputs_manifest
from ..skills.models import ExecutionStatus, ReusableSkill
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
from .notify import RunEventNotifier, WebhookEndpoint, WebhookEndpointStore
from .openapi import build_openapi
from .webhooks import WebhookVerifier

# The hold lifecycle as it appears on the audit log — and therefore on the
# approver's SSE feed. Every transition is one of these; nothing is silent.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_HOLD_EVENT_TYPES = frozenset(
    {"contract.held", "contract.approved", "contract.declined", "contract.expired"}
)

_PAUSE_VALUE = {
    PauseKind.CLARIFICATION: "clarification",
    PauseKind.CONFIRMATION: "confirmation",
    PauseKind.APPROVAL: "approval",
    PauseKind.INCIDENT: "incident",
}


def _event_detail(payload: object) -> str:
    """One human-readable line for a timeline event, from its audit payload:
    the status, the exact failing node when one is known, and the reason."""
    if not isinstance(payload, dict):
        return ""
    parts: list[str] = []
    status = payload.get("status")
    if status:
        parts.append(str(status))
    label = payload.get("failed_action_label")
    reason = payload.get("reason") or payload.get("error")
    if label and not (reason and str(label) in str(reason)):
        parts.append(f"node '{label}' failed")
    if reason:
        parts.append(str(reason))
    return " — ".join(parts)


def _plan_view(state: RunState) -> dict | None:
    """How OoLu planned the steps: the chosen route as an ordered node list,
    each carrying its live execution status, with the exact failing node
    marked. ``origin``/``notes`` distinguish an LLM-rebuilt route (and show
    the model's numbered plan) from an assembled one."""
    if state.route is None:
        return None
    chosen = state.route.chosen
    execution = state.execution
    outcome_by_action: dict[str, object] = {}
    if execution is not None:
        for outcome in execution.action_outcomes:
            # Per-action idempotency keys end in the action id (both runners).
            outcome_by_action[outcome.idempotency_key.rsplit(":", 1)[-1]] = outcome
    failed_id = execution.failed_action_id if execution else None
    steps = []
    for item in chosen.actions:
        outcome = outcome_by_action.get(item.action.id)
        failed = item.action.id == failed_id
        if outcome is not None:
            status = outcome.status.value
            error = outcome.error
        elif failed:
            # Blocked before an outcome existed (e.g. a capability gate).
            status = execution.status.value if execution else "blocked"
            error = execution.error if execution else None
        else:
            status = "planned"
            error = None
        steps.append(
            {
                "id": item.action.id,
                "label": f"{item.action.adapter}/{item.action.operation}",
                "status": status,
                "error": error,
                "failed": failed,
            }
        )
    return {
        "route": chosen.name,
        "origin": chosen.origin,
        "notes": list(chosen.plan_notes),
        "steps": steps,
    }


def _no_route_view(state: RunState) -> dict | None:
    """Why there was no route or node to search from — only for runs that
    failed before a viable route existed. Shows what grounding resolved,
    which terms it could not, and every candidate route the optimizer
    excluded, each with its reason."""
    if state.phase is not Phase.FAILED:
        return None
    if state.route is not None and not state.route.chosen.excluded:
        return None
    candidates = []
    if state.route is not None:
        for bp in [state.route.chosen, *state.route.alternatives]:
            candidates.append(
                {
                    "name": bp.name,
                    "excluded": bp.excluded,
                    "reason": bp.exclusion_reason,
                }
            )
    grounding = state.grounding
    return {
        "code": "PLAN_NO_ROUTE",
        "reason": state.failure_reason or "no route could be planned",
        "unresolved_terms": list(grounding.unresolved_terms) if grounding else [],
        "resolved_capabilities": (
            sorted(grounding.resolved_capabilities) if grounding else []
        ),
        "candidates": candidates,
    }


def _failure_view(state: RunState) -> dict | None:
    """The exact node that caused the most recent execution failure.

    ``code`` is the stable machine label for what went wrong — when a
    node's automation fails, this is the error code the user keeps to fix
    it later: EXEC_BLOCKED (a control/capability gate refused the node),
    EXEC_NODE_FAILED (the node ran and broke)."""
    execution = state.execution
    if execution is None or execution.status is ExecutionStatus.SUCCEEDED:
        return None
    payload = state.pause.payload if state.pause else {}
    return {
        "code": (
            "EXEC_BLOCKED"
            if execution.status is ExecutionStatus.BLOCKED
            else "EXEC_NODE_FAILED"
        ),
        "node_id": execution.failed_action_id,
        "node_label": execution.failed_action_label,
        "error": execution.error,
        "attempt": execution.attempt,
        "user_retries": state.user_retries,
        "rebuild_refusal": (
            payload.get("rebuild_refusal") if isinstance(payload, dict) else None
        ),
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
    # The online server this install pairs with (what the sign-in screen
    # uses instead of asking the user for a server). None = ask.
    server_url: str | None = None
    # Self-serve e-mail registration. Off by default: an online host
    # opts in with --open-registration. (E-mail *verification* arrives
    # with the mail-sender milestone; until then this is honest,
    # unverified sign-up for pre-launch testing.)
    open_registration: bool = False
    # Which tenant self-served accounts land in.
    registration_tenant: str = "main"
    # Is this deployment the OoLu GLOBAL service? Supernodes serving the
    # global ecosystem carry a higher trust score and must obey the KYC
    # policy (with its paying-plan gate). Edge installs — the desktop and
    # self-hosted/private-network servers — leave this off: their
    # Supernodes owe nobody a verification or a subscription.
    global_service: bool = False
    # How long a held reserved contract stays decidable. After this it is
    # swept (audited as contract.expired) — a stale hold must never be
    # released long after the submitter's intent went cold. None = never.
    contract_hold_ttl_seconds: int | None = 7 * 24 * 3600


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


def _media_type_for(name: str) -> str:
    lowered = name.lower()
    if lowered.endswith((".csv", ".tsv")):
        return "text/csv"
    if lowered.endswith(".json"):
        return "application/json"
    if lowered.endswith(".txt"):
        return "text/plain"
    return "text/markdown"


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
        proposal_model=None,  # orchestrator.ProposalModel; None + trace_store
        # -> TraceProposalModel over the calling tenant's own run history
        wallet_lookup: Callable[[str, str], float | None] | None = None,
        payout_store: PayoutStore | None = None,
        payout_adapter: PayoutAdapter | None = None,
        disputes: DisputeService | None = None,
        webhook_verifier: WebhookVerifier | None = None,
        accounts=None,  # identity.LocalAccountService: local multi-user login
        desk: WorkDesk | None = None,  # the Work environment's node desk
        kyc=None,  # nodeplace.KycService: Supernode legal-entity verification
        hygiene=None,  # nodeplace.NodeHygieneService: clone/fraud/zombie
        files: UserFileStore | None = None,  # user documents/sheets
        settings_node: SettingsNode | None = None,  # the settings node
        payments: PaymentMethodsService | None = None,  # card on file
        launch_guard: LaunchGuard | None = None,  # pre-launch charge gate
        subscriptions: SubscriptionService | None = None,  # plan lifecycle
        api_keys: ApiKeyService | None = None,  # machine credentials
        webhook_endpoints: WebhookEndpointStore | None = None,
        notifier: RunEventNotifier | None = None,  # run-event webhooks
        chat: ChatAssistant | None = None,  # the /v1/chat assistant; a
        # model-less default keeps the conversational surface working
        model_keys: ModelKeyring | None = None,  # tenant model API keys
        model_meter=None,  # billing.ModelCallMeter: chat spend enters books
        model_transport=None,  # providers.HttpTransport; None = real httpx
        subscription=None,  # billing.SubscriptionBrain: the hosted plan's
        # brain (platform keys + per-tenant monthly allowance); None on
        # every self-hosted install
        model_usage=None,  # billing.ModelUsageStore: per-tenant durable books
        stripe_webhooks=None,  # gateway.StripeWebhookVerifier: real Stripe
        # events land at /v1/webhooks/stripe only when this is configured
        google_signin: GoogleSignIn | None = None,  # "Continue with Google"
        identity_links: IdentityLinkStore | None = None,  # email/IdP -> account
        mail=None,  # mail.MailSender: verification + reset codes go out here
        mail_codes=None,  # mail.MailCodeStore: hashed one-time codes
        direct_messages=None,  # social.DirectMessageStore: friends talking
        assistant_history=None,  # social.AssistantHistoryStore: one OoLu
        # thread per account, shared by every signed-in device
        legal_dir=None,  # where the operator's terms.md/privacy.md live;
        # marked templates answer until those files exist
        local_files_root=None,  # the DESKTOP's own disk for the chat's
        # find_local_files tool; a multi-user host never sets this
        value_patcher=None,  # orchestrator.ValuePatcher: fills creative inputs
        isolation=None,  # worker.IsolationPolicy: powers /v1/worker-health
        docker_available: bool = True,
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
        # A model's opinion over producer picks — advisory (a prior over
        # the same posteriors), and its metered cost rides the preview's
        # planning_cost so budgets judge advice as spend.
        self._proposal_model = proposal_model
        # Fills declared creative inputs at run submission (user values
        # outrank it; defaults outlast it). Its metered cost joins the
        # budget-gated estimate: creative help is spend too.
        self._value_patcher = value_patcher
        # (tenant, principal) -> the LINKED wallet's remaining balance, or
        # None. A partial view of the user's assets by design: budgets never
        # cap on it, they only flag it for review.
        self._wallet_lookup = wallet_lookup
        # Reserved contracts held for approval: durable (they survive a
        # restart), tenant-scoped. The compiled artifact is process-local —
        # whichever process decides recompiles once.
        self._holds = PendingContractStore(durable.conn)
        self._compiled_holds: dict[str, tuple] = {}
        self._payout_store = payout_store
        self._payout_adapter = payout_adapter
        self._disputes = disputes
        self._webhook_verifier = webhook_verifier
        # Local user accounts (self-hosted multi-user): /v1/auth/* routes
        # answer only when this is configured — installs fronted by a real
        # IdP keep a 404 there and lose nothing.
        self._accounts = accounts
        self._desk = desk
        self._kyc = kyc
        self._hygiene = hygiene
        self._files = files
        self._settings = settings_node
        self._payments = payments
        self._launch_guard = launch_guard
        self._subscriptions = subscriptions
        self._api_keys = api_keys
        self._webhook_endpoints = webhook_endpoints
        self._notifier = notifier
        # The chat surface is the product face; it must work on every
        # install, so a missing assistant degrades to the model-less
        # default (rules + message-as-intent), never to a 404.
        self._chat = chat or ChatAssistant()
        # The brain behind chat: per-tenant routers over the keyring,
        # rebuilt when keys change. No keyring → chat stays model-less.
        self._model_keys = model_keys
        self._model_meter = model_meter
        self._model_transport = model_transport
        self._subscription = subscription
        self._model_usage = model_usage
        self._stripe_webhooks = stripe_webhooks
        self._model_routers: dict[str, ChatModelRouter] = {}
        self._google = google_signin
        self._identity_links = identity_links
        self._mail = mail
        self._mail_codes = mail_codes
        self._direct_messages = direct_messages
        self._assistant_history = assistant_history
        self._legal_dir = legal_dir
        self._local_files_root = local_files_root
        # What may run where, per trust level — rendered by the shell's
        # health screen from the policy that is actually enforced.
        from ..worker.policy import IsolationPolicy

        self._isolation = isolation or IsolationPolicy()
        self._docker_available = docker_available
        self._vault = vault or SecretVault()
        self._config = config or GatewayConfig()
        self._idem = idempotency or durable.idempotency
        self._clock = clock or (lambda: datetime.now(UTC))
        # For the metrics surface: how long this process has answered.
        self._started_at = self._clock()
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
            session, scopes = self._session_and_scopes(
                request.bearer_token(), request.now or self._clock()
            )
            if scopes is not None and not scope_allows(
                scopes, request.method, request.path
            ):
                # API keys reach the machine surface only — everything
                # else is absent by construction, whatever the key holds.
                raise GatewayError(
                    403, "forbidden", "outside this API key's scopes"
                )
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

    def _session_and_scopes(
        self, token: str | None, now: datetime
    ) -> tuple[Session, frozenset[str] | None]:
        """One auth door, two credential kinds: an API key yields a
        service session plus its scope set; an identity token yields a
        user session and None (no scope ceiling)."""
        if token and token.startswith(KEY_PREFIX):
            if self._api_keys is None:
                raise GatewayError(401, "unauthorized", "API keys are not enabled")
            record = self._api_keys.authenticate(token)
            if record is None:
                raise GatewayError(401, "unauthorized", "unknown or revoked API key")
            session = Session(
                principal_id=record.principal_id,
                principal_kind=PrincipalKind.SERVICE,
                tenant_id=record.tenant_id,
                issued_at=now,
                expires_at=now + timedelta(minutes=15),
                amr=["api_key"],
                source_issuer="oolu/api-keys",
            )
            return session, frozenset(record.scopes)
        return self._session_for(token, now), None

    def _session_for(self, token: str | None, now: datetime) -> Session:
        if not token:
            raise GatewayError(401, "unauthorized", "missing bearer token")
        if token.startswith(KEY_PREFIX):
            # Streams and other direct callers accept keys through the
            # same door as HTTP routes.
            return self._session_and_scopes(token, now)[0]
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
                "detail": _event_detail(r.payload),
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
        r.add("POST", "/v1/chat", self._chat_turn)
        # The account's own OoLu thread — what a fresh device loads.
        r.add("GET", "/v1/chat/history", self._chat_history)
        # Friends: person-to-person messages between accounts on this
        # host. Lookup is exact (username or e-mail) — never a directory.
        r.add("GET", "/v1/friends", self._friends_list)
        r.add("POST", "/v1/friends/lookup", self._friends_lookup)
        r.add("GET", "/v1/friends/{peer}/messages", self._friend_messages)
        r.add("POST", "/v1/friends/{peer}/messages", self._friend_send)
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
        # Operational counters are the operator's, not every member's: the
        # bootstrap admin's "*" covers it; grant metrics:read for a
        # monitoring account that can read nothing else.
        r.add(
            "GET",
            "/v1/metrics",
            self._metrics_endpoint,
            requires_permission="metrics:read",
        )
        r.add("GET", "/v1/worker-health", self._worker_health)
        # The legal surface: public, stable URLs. Terms and privacy are
        # the operator's files (templates answer until then); the Node
        # Policy is code-owned — the hygiene machinery enforces it.
        r.add("GET", "/v1/legal/terms", self._legal_terms, public=True)
        r.add("GET", "/v1/legal/privacy", self._legal_privacy, public=True)
        r.add("GET", "/v1/legal/node-policy", self._legal_node_policy, public=True)
        # The data-subject's two rights, self-serve: everything as one
        # JSON document, and erasure that says exactly what it removed.
        r.add("GET", "/v1/account/export", self._account_export)
        r.add("POST", "/v1/account/delete", self._account_delete)
        r.add("GET", "/v1/nodeplace", self._list_own_nodes)
        r.add("GET", "/v1/api-keys", self._api_keys_list)
        r.add("POST", "/v1/api-keys", self._api_keys_create)
        r.add("DELETE", "/v1/api-keys/{key_id}", self._api_keys_revoke)
        r.add("GET", "/v1/webhook-endpoints", self._webhooks_list)
        r.add("POST", "/v1/webhook-endpoints", self._webhooks_add)
        r.add(
            "DELETE",
            "/v1/webhook-endpoints/{endpoint_id}",
            self._webhooks_remove,
        )
        r.add("GET", "/v1/payment-methods", self._payment_methods_list)
        r.add("POST", "/v1/payment-methods", self._payment_methods_add)
        r.add(
            "DELETE", "/v1/payment-methods/{pm_ref}", self._payment_methods_remove
        )
        r.add(
            "POST",
            "/v1/payment-methods/{pm_ref}/default",
            self._payment_methods_default,
        )
        r.add("GET", "/v1/payments/status", self._payments_status)
        r.add("GET", "/v1/settings", self._settings_list)
        r.add("PUT", "/v1/settings", self._settings_update)
        # The subscription lifecycle: a commitment, not a settings knob.
        # Choose from free; changing terms means cancel first (the credit
        # for unused time is the deduction the next choose applies).
        r.add("GET", "/v1/subscription", self._subscription_view)
        r.add("POST", "/v1/subscription/choose", self._subscription_choose)
        r.add("POST", "/v1/subscription/cancel", self._subscription_cancel)
        # Model keys: the BYO-key door. Secrets go in; only fingerprints
        # ever come back out. Deliberately NOT a setting — the settings
        # catalog is visible data.
        r.add("GET", "/v1/keys/model", self._model_keys_list)
        r.add("POST", "/v1/keys/model", self._model_keys_add)
        r.add("POST", "/v1/keys/model/test", self._model_keys_test)
        r.add("DELETE", "/v1/keys/model/{provider}", self._model_keys_remove)
        # This month's model usage for the caller's tenant, plus the plan's
        # included allowance when a hosted brain exists here.
        r.add("GET", "/v1/usage/model", self._model_usage_view)
        r.add("GET", "/v1/files", self._files_list)
        r.add("POST", "/v1/files", self._files_create)
        r.add("GET", "/v1/files/{file_id}", self._files_get)
        r.add("PUT", "/v1/files/{file_id}", self._files_update)
        r.add("DELETE", "/v1/files/{file_id}", self._files_delete)
        r.add("GET", "/v1/work/nodes", self._work_nodes)
        r.add("POST", "/v1/work/nodes/{node_id}/account", self._work_account)
        r.add("GET", "/v1/work/nodes/{node_id}/activity", self._work_activity)
        r.add("GET", "/v1/work/nodes/{node_id}/kyc", self._kyc_status)
        r.add("POST", "/v1/work/nodes/{node_id}/kyc", self._kyc_apply)
        r.add("POST", "/v1/work/nodes/{node_id}/kyc/decide", self._kyc_decide)
        # The reviewer's inbox: pending applications, permission-gated (the
        # bootstrap admin's "*" covers it; a dedicated reviewer role grants
        # kyc:review without the rest of admin).
        r.add(
            "GET",
            "/v1/kyc/reviews",
            self._kyc_reviews,
            requires_permission="kyc:review",
        )
        r.add("GET", "/v1/work/policy", self._node_policy)
        r.add("GET", "/v1/work/hygiene", self._hygiene_inspect)
        r.add("POST", "/v1/work/hygiene/sweep", self._hygiene_sweep)
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
        r.add("GET", "/v1/runs/contract/holds", self._list_contract_holds)
        r.add("GET", "/v1/runs/contract/holds/events", self._hold_events)
        r.add(
            "POST",
            "/v1/runs/contract/holds/{pending_id}/reply",
            self._reply_contract_hold,
        )
        r.add(
            "POST",
            "/v1/runs/contract/holds/{pending_id}",
            self._decide_contract_hold,
        )
        r.add("GET", "/v1/earnings", self._earnings_balance)
        r.add("GET", "/v1/earnings/entries", self._earnings_entries)
        r.add("GET", "/v1/payout-accounts", self._get_payout_account)
        r.add("POST", "/v1/payout-accounts", self._create_payout_account)
        r.add("GET", "/v1/disputes/{event_id}", self._list_disputes)
        r.add("POST", "/v1/webhooks/processor", self._processor_webhook, public=True)
        # Real Stripe deliveries (Stripe-Signature over the raw payload);
        # answers 404 until the operator configures the endpoint secret.
        r.add("POST", "/v1/webhooks/stripe", self._stripe_webhook, public=True)
        # Local accounts (self-hosted multi-user). Login is public by
        # nature; management requires stored users:manage authority (the
        # bootstrap admin's role holds "*").
        r.add("POST", "/v1/auth/login", self._auth_login, public=True)
        # What a client needs to know about this host before signing in:
        # the paired online server, and which sign-in doors exist.
        r.add("GET", "/v1/client-config", self._client_config, public=True)
        # Self-serve e-mail registration (hosts opt in). With a mail
        # sender configured, registration is verification-first: the code
        # proves the address before the first sign-in, and password reset
        # rides the same codes.
        r.add("POST", "/v1/auth/register", self._auth_register, public=True)
        r.add("POST", "/v1/auth/verify", self._auth_verify, public=True)
        r.add("POST", "/v1/auth/reset/request", self._reset_request, public=True)
        r.add("POST", "/v1/auth/reset/confirm", self._reset_confirm, public=True)
        # Sign in with Google (RFC 8252): the app begins and polls; only
        # the browser's leg touches Google. All three answer 404 when no
        # Google client is configured on this host.
        r.add("GET", "/v1/auth/google/start", self._google_start, public=True)
        r.add("GET", "/v1/auth/google/callback", self._google_callback, public=True)
        r.add("POST", "/v1/auth/google/finish", self._google_finish, public=True)
        # Attaching Google to the CALLER's account needs the caller.
        r.add("POST", "/v1/auth/google/link", self._google_link)
        r.add(
            "GET",
            "/v1/auth/users",
            self._auth_list_users,
            requires_permission="users:manage",
        )
        r.add(
            "POST",
            "/v1/auth/users",
            self._auth_create_user,
            requires_permission="users:manage",
        )
        r.add(
            "POST",
            "/v1/auth/users/{username}/disabled",
            self._auth_set_disabled,
            requires_permission="users:manage",
        )

    # ------------------------------------------------------------------ #
    # Handlers.                                                           #
    # ------------------------------------------------------------------ #
    def _openapi(self, request, session, params) -> Response:
        return json_response(200, build_openapi())

    def _health(self, request, session, params) -> Response:
        return json_response(200, {"status": "ok"})

    def _chat_turn(self, request, session, params) -> Response:
        """One conversational turn with the OoLu assistant.

        The user-facing surface: the assistant answers, and when the turn
        is work it starts a plain (non-marketplace) run whose progress the
        client folds back into the conversation. The conversation itself is
        client-held — the request carries the recent history — so this
        route stays stateless over the same durable run store as /v1/runs.
        """
        body = request.body or {}
        message = body.get("message")
        if not message or not isinstance(message, str):
            raise GatewayError(400, "invalid_request", "message is required")
        history = body.get("history") or []
        if not isinstance(history, list):
            raise GatewayError(400, "invalid_request", "history must be a list")
        # The assistant's hands: the caller's own files, tenant-bound —
        # and, inside a node's interact window, that node's own desk.
        tools = None
        context_note = None
        if self._files is not None:
            node_id = body.get("node_id")
            if node_id:
                tools, context_note = self._node_chat_tools(
                    request, session, str(node_id)
                )
            else:
                tools = GatewayChatTools(
                    self._files,
                    tenant=session.tenant_id,
                    principal=session.principal_id,
                    durable=self._durable,
                    desk=self._desk,
                    settings=self._settings,
                    local_root=self._local_files_root,
                )
        # OoLu's voice follows its mood: the client sends the avatar's
        # current mood, and the turn is coloured to match the face.
        mood_note = mood_directive(body.get("mood"))
        context_note = "\n".join(n for n in (context_note, mood_note) if n) or None
        turn = self._chat.respond(
            message,
            history=[h for h in history if isinstance(h, dict)][-20:],
            sender=session.principal_id,
            tools=tools,
            model=self._tenant_model(session.tenant_id),
            context=context_note,
        )
        run = None
        say = turn.say
        if turn.task:
            try:
                run = self._start_intent_run(session, turn.task)
                self._metrics["chat_runs"] += 1
                # The run may have already failed DURING execution (submit
                # runs synchronously to the first pause or terminal phase).
                # The auto-build check must fire here too — not only on the
                # planning-time refusal below — so a failed execution names
                # the failing node and the consent switch that would let
                # OoLu plan and write code itself.
                say = self._describe_run_failure(say, run)
            except GatewayError as exc:
                if exc.code != "cannot_execute":
                    raise
                # The engine refused the plan: the assistant says so in the
                # conversation instead of the client showing a raw error —
                # and when auto-build could close the gap, it asks for the
                # consent switch instead of silently building.
                say = f"I can't run that on this machine yet — {exc.message}."
                if self._settings is not None and not bool(
                    self._settings.effective(session.tenant_id).get(
                        AUTOBUILD_CONSENT_KEY, False
                    )
                ):
                    say += f" If you want me to auto-build what's missing: {AUTOBUILD_HINT}"
        # The conversation survives the device: turns land in the per-
        # account history so every signed-in client sees one thread. The
        # node-interact window is that node's context, not this thread —
        # only the main conversation is recorded.
        if self._assistant_history is not None and not body.get("node_id"):
            self._assistant_history.append(
                tenant=session.tenant_id,
                principal=session.principal_id,
                kind="user",
                body=message,
            )
            self._assistant_history.append(
                tenant=session.tenant_id,
                principal=session.principal_id,
                kind="assistant",
                body=say,
            )
            if run:
                self._assistant_history.append(
                    tenant=session.tenant_id,
                    principal=session.principal_id,
                    kind="run",
                    body=str(run["run_id"]),
                )
        return json_response(
            200,
            {
                "reply": say,
                "source": turn.source,
                "actions": turn.actions,
                "run_id": run["run_id"] if run else None,
                "run": run,
            },
        )

    def _chat_history(self, request, session, params) -> Response:
        """The account's OoLu thread, oldest first — what a fresh device
        loads so every client shows the same conversation."""
        if self._assistant_history is None:
            raise GatewayError(404, "not_found", "chat history is not kept here")
        return json_response(
            200,
            {
                "items": self._assistant_history.history(
                    tenant=session.tenant_id, principal=session.principal_id
                )
            },
        )

    # ------------------------------------------------------------------ #
    # Friends: people talking to people on the same host.                 #
    # ------------------------------------------------------------------ #
    def _require_direct_messages(self):
        if self._direct_messages is None:
            raise GatewayError(
                404,
                "not_found",
                "friends live on a server — OoLu Global, or your own"
                " private network server",
            )
        return self._direct_messages

    def _friend_or_404(self, session, username: str) -> str:
        """The peer must be a real, enabled account in the caller's own
        tenant. You address people by exact name — there is no browsing."""
        username = str(username or "").strip()
        account = (
            self._accounts.user(username) if self._accounts is not None else None
        )
        if (
            account is None
            or account.tenant_id != session.tenant_id
            or account.disabled
        ):
            raise GatewayError(404, "not_found", "no one by that name here")
        if username == session.principal_id:
            raise GatewayError(
                400, "invalid_request", "that's you — notes to self live in Files"
            )
        return username

    def _friends_list(self, request, session, params) -> Response:
        store = self._require_direct_messages()
        return json_response(
            200,
            {
                "items": store.conversations(
                    tenant=session.tenant_id, principal=session.principal_id
                )
            },
        )

    def _friends_lookup(self, request, session, params) -> Response:
        """Find a person by EXACT username or e-mail — never a directory.
        A public host holds strangers; browsing the roster is not a thing."""
        self._require_direct_messages()
        query = str((request.body or {}).get("query", "")).strip()
        if not query:
            raise GatewayError(400, "invalid_request", "who are you looking for?")
        username = query
        if "@" in query and self._identity_links is not None:
            link = self._identity_links.lookup("email", query.lower())
            if link is None:
                raise GatewayError(404, "not_found", "no one by that address here")
            username = link["username"]
        username = self._friend_or_404(session, username)
        return json_response(200, {"username": username})

    def _friend_messages(self, request, session, params) -> Response:
        """The thread with one person — and opening it reads it."""
        store = self._require_direct_messages()
        peer = self._friend_or_404(session, params["peer"])
        store.mark_read(
            tenant=session.tenant_id, reader=session.principal_id, peer=peer
        )
        items = [
            {
                "message_id": m.message_id,
                "from": m.sender,
                "text": m.body,
                "file_id": m.file_id,
                "at": m.sent_at.isoformat(),
                "mine": m.sender == session.principal_id,
                "read": m.read_at is not None,
            }
            for m in store.between(
                tenant=session.tenant_id, me=session.principal_id, peer=peer
            )
        ]
        return json_response(200, {"peer": peer, "items": items})

    def _friend_send(self, request, session, params) -> Response:
        store = self._require_direct_messages()
        peer = self._friend_or_404(session, params["peer"])
        body = request.body or {}
        file_id = body.get("file_id")
        if file_id is not None:
            # The reference must be a real file the sender can see — the
            # recipient opens it through the same tenant-guarded store.
            if self._files is None or self._files.get(
                str(file_id), tenant=session.tenant_id
            ) is None:
                raise GatewayError(404, "not_found", "no such file to attach")
        try:
            message = store.send(
                tenant=session.tenant_id,
                sender=session.principal_id,
                recipient=peer,
                body=str(body.get("text", "")),
                file_id=str(file_id) if file_id else None,
            )
        except ValueError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        return json_response(
            201,
            {
                "message_id": message.message_id,
                "from": message.sender,
                "text": message.body,
                "file_id": message.file_id,
                "at": message.sent_at.isoformat(),
                "mine": True,
                "read": False,
            },
        )

    def _node_chat_tools(self, request, session, node_id: str):
        """The interact window's hands: one node's desk, gateway-walled.

        Every callable goes through the gateway's own handlers or stores,
        so tenant scope, approve authority, budget re-checks, audit, and
        the auto-build consent all apply exactly as they do on the routes.
        Returns ``(NodeChatTools, context_note)`` — the note tells the
        model where it is standing and which extra tools exist there.
        """
        desk = self._require_desk()
        entries = {
            e.node_id: e
            for e in desk.overview(
                principal=session.principal_id, tenant=session.tenant_id
            )
        }
        entry = entries.get(node_id)
        if entry is None:
            raise GatewayError(404, "not_found", "no such node on your desk")
        reason = f"audit-node:{node_id}"

        def holds_list() -> list[dict]:
            if self._holds is None:
                return []
            self._sweep_holds(request)
            return [
                {
                    "pending_id": record.pending_id,
                    "name": str(record.contract.get("name", "contract")),
                    "submitted_by": record.consumer_principal,
                    "created_at": record.created_at.isoformat(),
                }
                for record in self._holds.list(tenant=session.tenant_id)
                if reason in record.reserved
            ]

        def _via_handler(handler, pending_id: str, payload: dict) -> str:
            call = Request(
                method="POST",
                path="/internal",
                headers={},
                query={},
                body=payload,
                now=request.now,
            )
            try:
                handler(call, session, {"pending_id": pending_id})
            except GatewayError as exc:
                return f"error: {exc.message}"
            return "done"

        def holds_decide(pending_id: str, approved: bool, signature: str) -> str:
            payload: dict = {"approved": bool(approved)}
            if signature:
                payload["signature"] = signature
            return _via_handler(self._decide_contract_hold, pending_id, payload)

        def holds_reply(pending_id: str, message: str) -> str:
            return _via_handler(
                self._reply_contract_hold, pending_id, {"message": message}
            )

        def builder(goal: str) -> str:
            goal = (goal or "").strip()
            if not goal:
                return "error: tell me what the node should do"
            if self._settings is None or not bool(
                self._settings.effective(session.tenant_id).get(
                    AUTOBUILD_CONSENT_KEY, False
                )
            ):
                return f"error: auto-build is off — {AUTOBUILD_HINT}"
            # A node IS its function: the sentence must first read as
            # executable work, and the model must actually write the
            # execution function — otherwise nothing is created, because
            # an empty node called by the global machinery is unnecessary.
            if obviously_chat(goal):
                return (
                    "error: that reads as conversation, not an executable "
                    "task — a node is its function, so there is nothing "
                    "to build"
                )
            nodeplace = self._require_nodeplace()
            # ONE node per goal, forever: the skill id derives from the
            # goal itself, so rebuilding the same sentence finds the node
            # that already answers for it — every execution then lands in
            # THAT node's log instead of minting a twin.
            skill_id = self._function_skill_id(session.tenant_id, goal)
            existing = next(
                (
                    n
                    for n in nodeplace.list_own_nodes(
                        noder_principal=session.principal_id,
                        tenant_id=session.tenant_id,
                    )
                    if n.skill_id == skill_id
                ),
                None,
            )
            if existing is not None:
                return (
                    f"That node already exists — “{concise_name(goal)}” "
                    f"({existing.node_id[:8]}). No copy was made: running "
                    "it again lands every execution in its own log."
                )
            author = self._node_function_author(session.tenant_id)
            if author is None:
                return (
                    "error: building a node means writing its execution "
                    "function, and no model is configured to write it — "
                    "add a model key (or a local model) in Settings"
                )
            script, io, refusal = author_node_function(author, goal)
            if script is None:
                return f"error: {refusal}"
            name = concise_name(goal)
            skill = ReusableSkill.model_validate(
                {
                    "id": skill_id,
                    "name": name,
                    "description": goal,
                    "signature": {"application": "script", "adapter": "script"},
                    # The node's declared interface: what it consumes, as
                    # induced parameters — the same vocabulary the route
                    # assembler chains on.
                    "parameters": [
                        {
                            "name": item["name"],
                            "value_type": item["type"],
                            "required": True,
                        }
                        for item in io.get("inputs", [])
                    ],
                    # The node's OWN function: a script action the script
                    # runtime executes (verified before trusted, per node).
                    "actions": [
                        {
                            "correlation_id": "function",
                            "adapter": "script",
                            "operation": "run",
                            "parameters": {
                                "goal": goal,
                                "script": script,
                                "node_key": f"node:{skill_id}",
                            },
                        }
                    ],
                }
            )
            consumes = [
                Slot(name=item["name"], value_type=item["type"], role="input")
                for item in io.get("inputs", [])
            ]
            produces = [
                Slot(name=item["name"], value_type=item["type"], role="result")
                for item in io.get("outputs", [])
            ]
            try:
                result = nodeplace.contribute(
                    noder_principal=session.principal_id,
                    tenant_id=session.tenant_id,
                    skill=skill,
                    semver="1.0.0",
                    title=name,
                    summary=goal,
                    consumes=consumes or None,
                    produces=produces or None,
                )
                under = entry.account.is_supernode
                desk.create_account(
                    result.node.node_id,
                    principal=session.principal_id,
                    tenant=session.tenant_id,
                    supernode_id=node_id if under else None,
                    authority_level=1 if under else None,
                    policy_version=NODE_POLICY_VERSION,
                )
            except (ContributionError, OwnershipError, ValueError) as exc:
                return f"error: {exc}"
            new_id = result.node.node_id
            placing = (
                "under this Supernode — it starts UNCLAIMED: share its node "
                "id only with the person who should onboard it"
                if under
                else "on your desk, with you as its responsible"
            )
            interface = (
                "consumes "
                + (", ".join(f"{c.name}:{c.value_type}" for c in consumes) or "nothing")
                + " → produces "
                + ", ".join(f"{p.name}:{p.value_type}" for p in produces)
            )
            return (
                f"Built “{name}” ({new_id[:8]}) WITH its own execution "
                f"function ({interface}), {placing}. It starts "
                "needs-verification and becomes a callable, routable step "
                "on this node's path as its runs verify."
            )

        health = entry.health
        verified = health.verified_successes + health.verified_failures
        reliability = (
            f"{health.score * 100:.1f}% reliable over {verified} verified runs"
            if health.score is not None
            else "no verified runs yet"
        )
        context_note = (
            f"You are inside the interact window of the user's node "
            f"'{entry.title}' ({node_id[:8]}, status {entry.status}, "
            f"automation {reliability}). Help them accelerate this node's "
            "work: decide or sign its held requests, reply to requesters, "
            "and (with their auto-build consent) build missing execution "
            "nodes on its path. Extra tools available ONLY here:\n"
            '  {"tool": "node_holds", "args": {}}\n'
            '  {"tool": "decide_hold", "args": {"pending_id": "<id>", '
            '"approved": true, "signature": "<typed name, optional>"}}\n'
            '  {"tool": "reply_hold", "args": {"pending_id": "<id>", '
            '"message": "<text>"}}\n'
            '  {"tool": "build_node", "args": {"goal": "<what it must do>"}}\n'
            "Never decide or sign a held request the user did not ask you "
            "to. When automation fails, give the user the error code so "
            "they can fix it later."
        )
        tools = NodeChatTools(
            self._files,
            tenant=session.tenant_id,
            principal=session.principal_id,
            durable=self._durable,
            desk=self._desk,
            settings=self._settings,
            node={
                "node_id": node_id,
                "title": entry.title,
                "status": entry.status,
                "reliability": reliability,
            },
            holds_list=holds_list,
            holds_decide=holds_decide,
            holds_reply=holds_reply,
            builder=builder,
        )
        return tools, context_note

    @staticmethod
    def _describe_run_failure(say: str, run: dict | None) -> str:
        """Fold an execution failure into the assistant's reply: the exact
        failing node, then the auto-build hint the run view already carries
        when consent is off (or the rebuild's own refusal when it ran)."""
        if not run:
            return say
        if run.get("phase") != "failed" and run.get("awaiting") != "incident":
            return say
        failure = run.get("failure") or {}
        if failure.get("node_label"):
            say += f" The run hit a problem at node '{failure['node_label']}'"
            if failure.get("error"):
                say += f": {failure['error']}"
            say += "."
            if failure.get("code"):
                say += (
                    f" Error code {failure['code']} — saved with the run "
                    "so you can fix it later."
                )
        elif run.get("failure_reason"):
            say += f" The run failed — {run['failure_reason']}."
        if failure.get("rebuild_refusal"):
            say += f" {failure['rebuild_refusal']}"
        autobuild = run.get("autobuild") or {}
        if autobuild.get("hint"):
            say += f" {autobuild['hint']}"
        return say

    @staticmethod
    def _function_goal_key(text: str) -> str:
        return re.sub(r"\s+", " ", str(text or "").strip().casefold())

    def _function_skill_id(self, tenant: str, goal: str) -> str:
        import hashlib

        digest = hashlib.sha256(
            f"{tenant}|{self._function_goal_key(goal)}".encode()
        ).hexdigest()[:16]
        return f"fn-{digest}"

    def _resolve_node_function(self, session, intent: str) -> dict | None:
        """The node that already answers for this exact goal, if the user
        built one: its stored function becomes the run's route, so a
        re-run executes the node's OWN code — never a re-plan onto some
        other hand — and its executions accumulate in one log."""
        if self._nodeplace is None:
            return None
        skill_id = self._function_skill_id(session.tenant_id, intent)
        try:
            nodes = self._nodeplace.list_own_nodes(
                noder_principal=session.principal_id,
                tenant_id=session.tenant_id,
            )
        except Exception:  # noqa: BLE001 - resolution is best-effort
            return None
        node = next((n for n in nodes if n.skill_id == skill_id), None)
        if node is None:
            return None
        version = self._nodeplace.latest_version(node.node_id)
        if version is None:
            return None
        try:
            skill = ReusableSkill.model_validate_json(
                version.sanitized_skill_json
            )
        except Exception:  # noqa: BLE001
            return None
        action = next(
            (a for a in skill.actions if a.adapter == "script"), None
        )
        script = (action.parameters or {}).get("script") if action else None
        if not script:
            return None
        return {
            "node_id": node.node_id,
            "skill_id": skill_id,
            "title": skill.name,
            "goal": skill.description,
            "script": str(script),
            "node_key": str(
                (action.parameters or {}).get("node_key")
                or f"node:{skill_id}"
            ),
        }

    def _start_intent_run(self, session, intent: str, *, max_recovery: int = 1) -> dict:
        """Submit a plain intent as a run: the non-marketplace core of
        ``_submit_run``, shared with the chat surface."""
        tenant_runs = sum(
            1
            for s in self._durable.runs.list()
            if s.contract.metadata.get("tenant_id") == session.tenant_id
        )
        if tenant_runs >= self._config.max_runs_per_tenant:
            raise GatewayError(429, "quota_exceeded", "tenant run quota exceeded")
        metadata: dict = {"tenant_id": session.tenant_id}
        # A goal the user already built a node for runs THAT node's own
        # function — the route is the stored code, not a fresh plan.
        function = self._resolve_node_function(session, intent)
        if function is not None:
            metadata["node_function"] = function
        contract = TaskContract(
            intent=intent,
            submitted_by=session.principal_id,
            metadata=metadata,
        )
        try:
            state = self._durable.submit(
                contract, max_recovery_attempts=max_recovery
            )
        except OrchestratorError as exc:
            # A refused plan (e.g. preflight: the planned route needs a
            # capability no executor here provides) is an honest answer
            # about this machine, not a server crash.
            raise GatewayError(422, "cannot_execute", str(exc)) from exc
        self._metrics["runs_submitted"] += 1
        return self._run_dict(state)

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
            metadata: dict = {"tenant_id": session.tenant_id}
            if node_version_id is None:
                function = self._resolve_node_function(session, intent)
                if function is not None:
                    metadata["node_function"] = function
            contract = TaskContract(
                intent=intent,
                submitted_by=session.principal_id,
                metadata=metadata,
            )
            try:
                state = self._durable.submit(
                    contract, max_recovery_attempts=max_recovery
                )
            except OrchestratorError as exc:
                # Same honesty as the chat surface: a plan this machine
                # cannot execute is a 422 with the reason, not a 500.
                raise GatewayError(422, "cannot_execute", str(exc)) from exc
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
                    {
                        "seq": r.seq,
                        "event_type": r.event_type,
                        "at": r.at.isoformat(),
                        "detail": _event_detail(r.payload),
                    }
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
        counters = dict(self._metrics)
        counters["uptime_seconds"] = max(
            0,
            int(
                (
                    (request.now or self._clock()) - self._started_at
                ).total_seconds()
            ),
        )
        return json_response(200, counters)

    # ------------------------------------------------------------------ #
    # The legal surface: public, stable, operator-owned words.            #
    # ------------------------------------------------------------------ #
    def _legal_terms(self, request, session, params) -> Response:
        from ..legal import legal_document

        return Response(
            status=200,
            body=legal_document("terms", legal_dir=self._legal_dir),
            content_type="text/markdown; charset=utf-8",
        )

    def _legal_privacy(self, request, session, params) -> Response:
        from ..legal import legal_document

        return Response(
            status=200,
            body=legal_document("privacy", legal_dir=self._legal_dir),
            content_type="text/markdown; charset=utf-8",
        )

    def _legal_node_policy(self, request, session, params) -> Response:
        return json_response(
            200, {"version": NODE_POLICY_VERSION, "text": NODE_POLICY}
        )

    # ------------------------------------------------------------------ #
    # The data-subject's rights: export everything, erase what's yours.   #
    # ------------------------------------------------------------------ #
    def _account_export(self, request, session, params) -> Response:
        """Everything this host holds about the caller, as one JSON
        document. Sections appear when the matching store exists; a
        section this host doesn't keep simply isn't there."""
        tenant, principal = session.tenant_id, session.principal_id
        export: dict = {
            "exported_at": (request.now or self._clock()).isoformat(),
            "tenant": tenant,
            "principal": principal,
        }
        if self._accounts is not None:
            account = self._accounts.user(principal)
            if account is not None:
                export["account"] = {
                    "username": account.username,
                    "roles": sorted(account.roles),
                    "disabled": account.disabled,
                    "created_at": str(account.created_at),
                }
        if self._identity_links is not None:
            export["identity_links"] = self._identity_links.links_for(principal)
        if self._settings is not None:
            export["settings"] = self._settings.effective(tenant)
        if self._assistant_history is not None:
            export["chat"] = self._assistant_history.history(
                tenant=tenant, principal=principal, limit=10_000
            )
        if self._direct_messages is not None:
            export["messages"] = {
                conversation["peer"]: [
                    {
                        "from": m.sender,
                        "text": m.body,
                        "file_id": m.file_id,
                        "at": m.sent_at.isoformat(),
                    }
                    for m in self._direct_messages.between(
                        tenant=tenant,
                        me=principal,
                        peer=conversation["peer"],
                        limit=10_000,
                    )
                ]
                for conversation in self._direct_messages.conversations(
                    tenant=tenant, principal=principal
                )
            }
        if self._files is not None:
            # The Life drawer. Node drawers belong to nodes (shared work
            # records), so they are not part of a personal export.
            export["files"] = [
                {
                    "name": f.name,
                    "folder": f.folder,
                    "media_type": f.media_type,
                    "updated_at": f.updated_at.isoformat(),
                    "content": f.content,
                }
                for f in self._files.list(tenant=tenant)
            ]
        export["runs"] = [
            self._run_dict(s)
            for s in self._durable.runs.list(limit=10_000)
            if s.contract.metadata.get("tenant_id") == tenant
        ]
        if self._model_usage is not None:
            export["model_usage_this_month"] = self._model_usage.view(tenant)
        if self._billing is not None:
            export["earnings"] = [
                entry.model_dump(mode="json")
                for entry in self._billing.entries(principal)
            ]
        if self._payments is not None:
            try:
                export["payment_profile"] = self._payments.profile(
                    principal
                ).model_dump(mode="json")
            except Exception:  # noqa: BLE001 - a dead vault never blocks export
                pass
        if self._payout_store is not None:
            payout = self._payout_store.get_account(principal)
            if payout is not None:
                export["payout_account"] = payout.model_dump(mode="json")
        return json_response(200, export)

    def _account_delete(self, request, session, params) -> Response:
        """Erasure, honestly described: the password proves the owner (a
        stolen session must not be able to destroy an account), the
        per-person stores are wiped, the account is disabled forever
        (never reissued — a freed name would let a stranger inherit a
        reputation), and the response says exactly what was and was not
        removed."""
        accounts = self._require_accounts()
        password = str((request.body or {}).get("password", ""))
        try:
            accounts.login(
                session.principal_id, password, now=request.now or self._clock()
            )
        except AuthenticationError as exc:
            raise GatewayError(
                403,
                "forbidden",
                "deleting the account takes your password — a signed-in"
                " device alone is not enough",
            ) from exc
        tenant, principal = session.tenant_id, session.principal_id
        erased: dict[str, int] = {}
        # The address first — the links still know it.
        email = (
            self._identity_links.email_of(principal)
            if self._identity_links is not None
            else None
        )
        if self._direct_messages is not None:
            erased["messages"] = self._direct_messages.erase_principal(
                tenant=tenant, principal=principal
            )
        if self._assistant_history is not None:
            erased["chat_turns"] = self._assistant_history.erase(
                tenant=tenant, principal=principal
            )
        if self._identity_links is not None:
            erased["identity_links"] = self._identity_links.unlink_all(principal)
        if email and self._mail_codes is not None:
            erased["mail_codes"] = self._mail_codes.forget(email)
        if self._payments is not None and self._payments.forget(principal):
            erased["payment_profile"] = 1
        accounts.set_disabled(principal, True)
        self._durable.audit.append(
            "account.erased",
            {
                "run_id": f"account:{principal}",
                "tenant": tenant,
                "principal": principal,
                "erased": erased,
            },
        )
        return json_response(
            200,
            {
                "account": "disabled",
                "erased": erased,
                "notes": [
                    "the username stays reserved and disabled forever —"
                    " a freed name would let a stranger inherit its trust",
                    "your messages were removed from BOTH sides of every"
                    " conversation (the store keeps one shared copy)",
                    "files live in the shared drawer — delete yours in"
                    " Files before deleting the account if you want them"
                    " gone",
                    "append-only records the service must keep (the"
                    " tamper-evident audit chain, financial ledgers) are"
                    " retained; they are minimal and pseudonymous",
                    "already-issued sign-in tokens expire on their own"
                    " schedule; no new sign-in will succeed",
                ],
            },
        )

    def _worker_health(self, request, session, params) -> Response:
        from ..worker.policy import execution_labels

        return json_response(
            200,
            {
                "docker_available": self._docker_available,
                "labels": execution_labels(self._isolation),
            },
        )

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
                # An explicit title is honored verbatim; the FALLBACK is
                # condensed to keywords so a skill named by a whole task
                # sentence never becomes a sentence-long listing title.
                title=str(body.get("title") or concise_name(skill.name)),
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
                inputs=self._parse_inputs(body.get("inputs")),
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

    # ------------------------------------------------------------------ #
    # User files: documents and sheets in the durable database.           #
    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # API keys + webhook endpoints: the public execution API's controls.  #
    # ------------------------------------------------------------------ #
    def _require_api_keys(self, session) -> ApiKeyService:
        if self._api_keys is None:
            raise GatewayError(404, "not_found", "API keys are not enabled")
        if "api_key" in session.amr:
            # A key cannot mint, list, or revoke keys — management belongs
            # to interactive identities only.
            raise GatewayError(403, "forbidden", "keys cannot manage keys")
        return self._api_keys

    @staticmethod
    def _api_key_dict(record) -> dict:
        return {
            "key_id": record.key_id,
            "name": record.name,
            "scopes": list(record.scopes),
            "created_at": record.created_at.isoformat(),
            "revoked_at": (
                record.revoked_at.isoformat() if record.revoked_at else None
            ),
            "last_used_at": (
                record.last_used_at.isoformat() if record.last_used_at else None
            ),
        }

    def _api_keys_list(self, request, session, params) -> Response:
        service = self._require_api_keys(session)
        return json_response(
            200,
            {
                "items": [
                    self._api_key_dict(r)
                    for r in service.list(tenant=session.tenant_id)
                ]
            },
        )

    def _api_keys_create(self, request, session, params) -> Response:
        service = self._require_api_keys(session)
        body = request.body or {}
        name = body.get("name")
        if not name or not isinstance(name, str):
            raise GatewayError(400, "invalid_request", "name is required")
        scopes = body.get("scopes")
        if scopes is not None and not isinstance(scopes, list):
            raise GatewayError(400, "invalid_request", "scopes must be a list")
        try:
            record, secret = service.issue(
                tenant=session.tenant_id,
                principal=session.principal_id,
                name=name,
                scopes=scopes,
            )
        except ApiKeyError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        # The secret appears in THIS response and nowhere else, ever.
        return json_response(201, {**self._api_key_dict(record), "secret": secret})

    def _api_keys_revoke(self, request, session, params) -> Response:
        service = self._require_api_keys(session)
        if not service.revoke(params["key_id"], tenant=session.tenant_id):
            raise GatewayError(404, "not_found", "no such active key")
        return json_response(200, {"revoked": True})

    def _require_webhooks(self, session) -> WebhookEndpointStore:
        if self._webhook_endpoints is None:
            raise GatewayError(404, "not_found", "webhooks are not enabled")
        if "api_key" in session.amr:
            raise GatewayError(403, "forbidden", "keys cannot manage webhooks")
        return self._webhook_endpoints

    def _webhooks_list(self, request, session, params) -> Response:
        store = self._require_webhooks(session)
        return json_response(
            200,
            {
                "items": [
                    {
                        "endpoint_id": e.endpoint_id,
                        "url": e.url,
                        "created_at": e.created_at.isoformat(),
                    }
                    for e in store.list(tenant=session.tenant_id)
                ]
            },
        )

    def _webhooks_add(self, request, session, params) -> Response:
        store = self._require_webhooks(session)
        body = request.body or {}
        url = body.get("url")
        if (
            not url
            or not isinstance(url, str)
            or not url.startswith(("https://", "http://"))
        ):
            raise GatewayError(400, "invalid_request", "a valid url is required")
        endpoint = WebhookEndpoint(
            tenant_id=session.tenant_id,
            url=url.strip(),
            secret="whsec_" + uuid4().hex,
        )
        store.add(endpoint)
        # The signing secret appears in THIS response and nowhere else.
        return json_response(
            201,
            {
                "endpoint_id": endpoint.endpoint_id,
                "url": endpoint.url,
                "secret": endpoint.secret,
            },
        )

    def _webhooks_remove(self, request, session, params) -> Response:
        store = self._require_webhooks(session)
        if not store.remove(params["endpoint_id"], tenant=session.tenant_id):
            raise GatewayError(404, "not_found", "no such endpoint")
        return json_response(200, {"removed": True})

    # ------------------------------------------------------------------ #
    # Payment methods: card on file (pre-launch: test vault only).        #
    # ------------------------------------------------------------------ #
    def _require_payments(self) -> PaymentMethodsService:
        if self._payments is None:
            raise GatewayError(404, "not_found", "payments are not enabled")
        return self._payments

    def _payment_profile_dict(self, profile) -> dict:
        return {
            "mode": self._payments.mode,
            "default_pm": profile.default_pm,
            "cards": [
                {
                    "pm_ref": c.pm_ref,
                    "brand": c.brand,
                    "last4": c.last4,
                    "exp_month": c.exp_month,
                    "exp_year": c.exp_year,
                }
                for c in profile.cards
            ],
        }

    def _payment_methods_list(self, request, session, params) -> Response:
        payments = self._require_payments()
        return json_response(
            200, self._payment_profile_dict(payments.profile(session.principal_id))
        )

    def _payment_methods_add(self, request, session, params) -> Response:
        """Save a card. Pre-launch: a named TEST card only — the route has
        no field that could carry a real number. Live (later): the body
        would carry a client-confirmed SetupIntent's payment method."""
        payments = self._require_payments()
        body = request.body or {}
        brand = body.get("brand")
        if not brand or not isinstance(brand, str):
            raise GatewayError(400, "invalid_request", "brand is required")
        try:
            card = payments.add_test_card(session.principal_id, brand)
        except PaymentError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        return json_response(
            201,
            {
                "pm_ref": card.pm_ref,
                "brand": card.brand,
                "last4": card.last4,
                "mode": payments.mode,
            },
        )

    def _payment_methods_remove(self, request, session, params) -> Response:
        payments = self._require_payments()
        removed = payments.remove_card(session.principal_id, params["pm_ref"])
        if not removed:
            raise GatewayError(404, "not_found", "no such payment method")
        return json_response(200, {"removed": True})

    def _payment_methods_default(self, request, session, params) -> Response:
        payments = self._require_payments()
        if not payments.set_default(session.principal_id, params["pm_ref"]):
            raise GatewayError(404, "not_found", "no such payment method")
        return json_response(200, {"default_pm": params["pm_ref"]})

    def _payments_status(self, request, session, params) -> Response:
        """Whether real charging is open, and why not: the pre-launch
        switch, price settlement, and verification — spelled out."""
        payments = self._require_payments()
        guard = self._launch_guard
        class_key = request.query.get("class_key", "")
        if guard is None:
            state = {"open": False, "mode": payments.mode, "reasons": [
                "no launch guard configured"
            ]}
        else:
            state = guard.status(class_key).model_dump(mode="json")
        state["vault_mode"] = payments.mode
        return json_response(200, state)

    # ------------------------------------------------------------------ #
    # The settings node: bounded configuration, no code path.             #
    # ------------------------------------------------------------------ #
    def _require_settings(self) -> SettingsNode:
        if self._settings is None:
            raise GatewayError(404, "not_found", "settings are not enabled")
        return self._settings

    def _settings_list(self, request, session, params) -> Response:
        node = self._require_settings()
        return json_response(
            200, {"items": node.describe(session.tenant_id)}
        )

    def _settings_update(self, request, session, params) -> Response:
        """Apply setting changes through the node's declared catalog only.

        The body is ``{"changes": {key: value}}``; every key must be a
        catalogued setting and every value within its bounds, or the whole
        batch is refused (400). There is no route that writes an arbitrary
        key — configuration cannot escape the schema.
        """
        node = self._require_settings()
        body = request.body or {}
        changes = body.get("changes")
        if not isinstance(changes, dict) or not changes:
            raise GatewayError(400, "invalid_request", "changes object is required")
        try:
            node.set_many(session.tenant_id, changes)
        except SettingError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        return json_response(200, {"items": node.describe(session.tenant_id)})

    # ------------------------------------------------------------------ #
    # Model keys: the BYO-key door and the per-tenant brain behind chat.  #
    # ------------------------------------------------------------------ #
    def _require_model_keys(self) -> ModelKeyring:
        if self._model_keys is None:
            raise GatewayError(404, "not_found", "model keys are not enabled")
        return self._model_keys

    def _tenant_model(self, tenant: str) -> ChatModelRouter | None:
        """The tenant's chat brain, or None to stay model-less.

        Routers are cached per tenant (adapters keep capability caches) and
        dropped whenever the tenant's keys change. Settings are read through
        closures at call time, so a settings change needs no invalidation.
        """
        if self._model_keys is None:
            return None
        settings = self._settings

        def _effective(key: str, fallback):
            if settings is None:
                return fallback
            return settings.effective(tenant).get(key, fallback)

        # No key normally means no brain — EXCEPT when the default model
        # is the machine's own local server (needs no key), or when this
        # host carries the hosted plan's brain (platform keys serve
        # tenants whose source is "subscription").
        source_now = str(_effective("model.source", "subscription"))
        hosted_brain = (
            source_now == "subscription"
            and self._subscription is not None
            and self._subscription.configured()
        )
        if (
            not self._model_keys.providers(tenant)
            and source_now != "local"
            and not hosted_brain
        ):
            return None
        router = self._model_routers.get(tenant)
        if router is None:
            router = ChatModelRouter(
                self._model_keys,
                tenant,
                transport=self._model_transport,
                meter=self._model_meter,
                subscription=self._subscription,
                budget=lambda: float(_effective("budget.model_cap", 0.0) or 0.0),
                currency=lambda: str(_effective("account.currency", "USD")),
                preference=lambda: str(_effective("model.provider", "auto")),
                tier=lambda: str(_effective("model.tier", "fast")),
                source=lambda: str(_effective("model.source", "subscription")),
                local_url=lambda: str(_effective("model.local_url", "")),
                local_model=lambda: str(_effective("model.local_model", "")),
                web_search=lambda: bool(_effective("model.web_search", True)),
            )
            self._model_routers[tenant] = router
        return router

    def _node_function_author(self, tenant: str):
        """The model that writes a new node's execution function — the
        tenant's own chat brain by default; a seam so tests (or a future
        dedicated authoring model) can supply their own."""
        return self._tenant_model(tenant)

    def _model_keys_list(self, request, session, params) -> Response:
        keyring = self._require_model_keys()
        return json_response(
            200, {"items": keyring.providers(session.tenant_id)}
        )

    def _model_usage_view(self, request, session, params) -> Response:
        """This month's model consultations for the caller's tenant, plus
        the hosted plan's allowance and remaining balance when this host
        has a subscription brain."""
        if self._model_usage is None:
            raise GatewayError(404, "not_found", "model usage is not tracked here")
        tenant = session.tenant_id
        view: dict = {"items": self._model_usage.view(tenant)}
        if self._subscription is not None and self._subscription.configured():
            allowance = self._subscription.allowance_for(tenant)
            spent = self._subscription.month_spend(tenant)
            view["subscription"] = {
                "allowance_usd": allowance,
                "spent_usd": spent,
                "remaining_usd": max(0.0, allowance - spent),
            }
        return json_response(200, view)

    def _model_keys_add(self, request, session, params) -> Response:
        """Take a pasted key into the encrypted keyring; answer with only a
        fingerprint. The secret never appears in a response, a log line, a
        setting, or an error — this route is the one door in."""
        keyring = self._require_model_keys()
        body = request.body or {}
        provider = body.get("provider")
        key = body.get("key")
        if provider not in PROVIDERS:
            allowed = ", ".join(PROVIDERS)
            raise GatewayError(
                400, "invalid_request", f"provider must be one of: {allowed}"
            )
        if not isinstance(key, str) or len(key.strip()) < 8:
            raise GatewayError(
                400, "invalid_request", "that doesn't look like an API key"
            )
        mark = keyring.store(session.tenant_id, provider, key)
        # The next chat turn must see the new key, not a cached adapter.
        self._model_routers.pop(session.tenant_id, None)
        self._metrics["model_keys_added"] += 1
        # Make the added key ACTUALLY the model. The default source
        # ("subscription") is built for the OoLu plan's hosted brain,
        # which no self-hosted/desktop install has — so a key added while
        # still on that default would only ever be a silent fallback,
        # never the user's chosen provider. Flip to "own-api" (and point
        # the provider preference at the key just added) so the key the
        # user pasted is the model the user gets. A deliberate "local"
        # choice is left untouched.
        source_switched = False
        if self._settings is not None:
            current = str(
                self._settings.effective(session.tenant_id).get(
                    "model.source", "subscription"
                )
            )
            if current == "subscription":
                self._settings.set(session.tenant_id, "model.source", "own-api")
                self._settings.set(
                    session.tenant_id, "model.provider", provider
                )
                source_switched = True
        return json_response(
            201,
            {
                "provider": provider,
                "fingerprint": mark,
                "source_switched": source_switched,
            },
        )

    def _model_keys_test(self, request, session, params) -> Response:
        """Prove the configured model actually answers — one real call.

        The definitive answer to "is my key working?": builds the tenant's
        live router (the same one chat uses, honoring model.source and the
        provider/tier settings), makes one tiny completion, and reports
        the model that answered — or the exact reason it could not, so a
        billed-but-silent misconfiguration surfaces as words, not a
        mystery.
        """
        self._require_model_keys()
        router = self._tenant_model(session.tenant_id)
        if router is None:
            return json_response(
                200,
                {
                    "ok": False,
                    "error": "no model is configured — add a key above, or "
                    "set the default model to a local server in Settings",
                },
            )
        try:
            reply = router.reply(
                [
                    {
                        "role": "system",
                        "content": "Reply with exactly the word: pong.",
                    },
                    {"role": "user", "content": "ping"},
                ]
            )
        except ModelBudgetExceeded as exc:
            return json_response(200, {"ok": False, "error": str(exc)})
        except ModelUnavailable as exc:
            return json_response(200, {"ok": False, "error": str(exc)})
        return json_response(
            200,
            {
                "ok": True,
                "reply": reply.strip()[:200],
                "source": str(
                    self._settings.effective(session.tenant_id).get(
                        "model.source", "subscription"
                    )
                )
                if self._settings is not None
                else "subscription",
            },
        )

    def _model_keys_remove(self, request, session, params) -> Response:
        keyring = self._require_model_keys()
        provider = params.get("provider", "")
        if not keyring.remove(session.tenant_id, provider):
            raise GatewayError(404, "not_found", f"no {provider} key is stored")
        self._model_routers.pop(session.tenant_id, None)
        return json_response(200, {"removed": provider})

    # ------------------------------------------------------------------ #
    # The subscription lifecycle (the account console's backend).         #
    # ------------------------------------------------------------------ #
    def _require_subscriptions(self) -> SubscriptionService:
        if self._subscriptions is None:
            raise GatewayError(404, "not_found", "subscriptions are not enabled")
        return self._subscriptions

    def _subscription_view(self, request, session, params) -> Response:
        service = self._require_subscriptions()
        return json_response(200, service.view(session.tenant_id))

    def _subscription_choose(self, request, session, params) -> Response:
        service = self._require_subscriptions()
        body = request.body or {}
        try:
            result = service.choose(
                session.tenant_id,
                str(body.get("plan", "")),
                str(body.get("cycle", "monthly")),
            )
        except SubscriptionError as exc:
            raise GatewayError(409, "conflict", str(exc)) from exc
        self._metrics["subscription_chosen"] += 1
        return json_response(200, result)

    def _subscription_cancel(self, request, session, params) -> Response:
        service = self._require_subscriptions()
        try:
            result = service.cancel(session.tenant_id)
        except SubscriptionError as exc:
            raise GatewayError(409, "conflict", str(exc)) from exc
        self._metrics["subscription_cancelled"] += 1
        return json_response(200, result)

    def _require_files(self) -> UserFileStore:
        if self._files is None:
            raise GatewayError(404, "not_found", "user files are not enabled")
        return self._files

    @staticmethod
    def _file_meta(file: UserFile) -> dict:
        return {
            "file_id": file.file_id,
            "node_id": file.node_id,
            "name": file.name,
            "folder": file.folder,
            "media_type": file.media_type,
            "size": file.size,
            "created_at": file.created_at.isoformat(),
            "updated_at": file.updated_at.isoformat(),
        }

    def _files_list(self, request, session, params) -> Response:
        store = self._require_files()
        node_id = request.query.get("node_id") or None
        return json_response(
            200,
            {
                "items": [
                    self._file_meta(f)
                    for f in store.list(tenant=session.tenant_id, node_id=node_id)
                ]
            },
        )

    def _files_create(self, request, session, params) -> Response:
        store = self._require_files()
        body = request.body or {}
        name = body.get("name")
        if not name or not isinstance(name, str):
            raise GatewayError(400, "invalid_request", "name is required")
        try:
            folder = normalize_folder(body.get("folder"))
        except ValueError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        file = UserFile(
            tenant_id=session.tenant_id,
            node_id=(str(body["node_id"]) if body.get("node_id") else None),
            name=name.strip(),
            folder=folder,
            media_type=str(body.get("media_type") or _media_type_for(name)),
            content=str(body.get("content") or ""),
        )
        try:
            store.save(file)
        except FileTooLargeError as exc:
            raise GatewayError(413, "too_large", str(exc)) from exc
        return json_response(
            201, {**self._file_meta(file), "content": file.content}
        )

    def _load_file(self, params, session) -> UserFile:
        store = self._require_files()
        file = store.get(params["file_id"], tenant=session.tenant_id)
        if file is None:
            raise GatewayError(404, "not_found", "no such file")
        return file

    def _files_get(self, request, session, params) -> Response:
        file = self._load_file(params, session)
        return json_response(200, {**self._file_meta(file), "content": file.content})

    def _files_update(self, request, session, params) -> Response:
        store = self._require_files()
        file = self._load_file(params, session)
        body = request.body or {}
        try:
            folder = (
                normalize_folder(body["folder"])
                if "folder" in body and body["folder"] is not None
                else file.folder
            )
        except ValueError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        updated = file.model_copy(
            update={
                "name": (
                    str(body["name"]).strip() if body.get("name") else file.name
                ),
                "folder": folder,
                "content": (
                    str(body["content"])
                    if "content" in body and body["content"] is not None
                    else file.content
                ),
                "updated_at": request.now or self._clock(),
            }
        )
        try:
            store.save(updated)
        except FileTooLargeError as exc:
            raise GatewayError(413, "too_large", str(exc)) from exc
        return json_response(
            200, {**self._file_meta(updated), "content": updated.content}
        )

    def _files_delete(self, request, session, params) -> Response:
        store = self._require_files()
        self._load_file(params, session)
        store.delete(params["file_id"], tenant=session.tenant_id)
        return json_response(200, {"deleted": True})

    def _require_desk(self) -> WorkDesk:
        if self._desk is None:
            raise GatewayError(404, "not_found", "the work desk is not enabled")
        return self._desk

    def _work_nodes(self, request, session, params) -> Response:
        """The Work environment's node account list: every node the caller
        answers for, with account, cumulative earnings, and health."""
        desk = self._require_desk()
        entries = desk.overview(
            principal=session.principal_id, tenant=session.tenant_id
        )
        return json_response(
            200, {"items": [e.model_dump(mode="json") for e in entries]}
        )

    _FIXED_ACCOUNT_TRAITS = (
        "policy_version",
        "audit_mode",
        "allow_autodev_data",
        "is_supernode",
        "supernode_id",
        "authority_level",
    )

    def _work_account(self, request, session, params) -> Response:
        """The account door, honoring what is fixed at creation.

        Three shapes: ``{"onboard": true}`` takes responsibility with NO
        choices; a body against a node with no account CREATES it, fixing
        its regime (supernode, under-supernode, authority level, audit,
        auto-growing) forever — for everyone, the Supernode's humans
        included; anything else is an UPDATE limited to the mutable slice —
        a fixed trait in an update body is refused loudly, never merged.
        """
        desk = self._require_desk()
        body = request.body or {}
        level = body.get("authority_level")
        try:
            if body.get("onboard"):
                account = desk.onboard_account(
                    params["node_id"],
                    principal=session.principal_id,
                    tenant=session.tenant_id,
                )
            elif desk.account_for(params["node_id"]) is None:
                if not bool(body.get("accept_policy")):
                    # Agreed UPFRONT, or not created at all: the policy is
                    # what authorizes clone/fraud/zombie enforcement later.
                    raise GatewayError(
                        409,
                        "policy_required",
                        "creating a node means agreeing to the Node Policy "
                        f"first ({NODE_POLICY_VERSION}): {NODE_POLICY}",
                    )
                account = desk.create_account(
                    params["node_id"],
                    principal=session.principal_id,
                    tenant=session.tenant_id,
                    policy_version=NODE_POLICY_VERSION,
                    is_supernode=bool(body.get("is_supernode", False)),
                    supernode_id=body.get("supernode_id") or None,
                    audit_mode=bool(body.get("audit_mode", False)),
                    allow_autodev_data=bool(
                        body.get("allow_autodev_data", True)
                    ),
                    authority_level=int(level) if level is not None else None,
                    admin=body.get("admin"),
                )
            else:
                fixed = [k for k in self._FIXED_ACCOUNT_TRAITS if k in body]
                if fixed:
                    raise GatewayError(
                        409,
                        "conflict",
                        "fixed at creation and cannot be changed: "
                        + ", ".join(fixed),
                    )
                account = desk.update_account(
                    params["node_id"],
                    principal=session.principal_id,
                    tenant=session.tenant_id,
                    status=body.get("status"),
                    admin=body.get("admin"),
                )
        except OwnershipError as exc:
            raise GatewayError(403, "forbidden", str(exc)) from exc
        except ContributionError as exc:
            raise GatewayError(404, "not_found", str(exc)) from exc
        except (ValueError, ValidationError) as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        return json_response(200, account.model_dump(mode="json"))

    def _work_activity(self, request, session, params) -> Response:
        """The node's execution feed: bound runs expanded into audit steps.

        Every item names the node that EXECUTED it (a Supernode's feed
        aggregates its members', so the human reads who did what, not just
        that something ran), and each fetch materializes the node's daily
        execution log file — the full-fidelity record kept for legal use.
        """
        desk = self._require_desk()
        node_id = params["node_id"]
        entries = {
            e.node_id: e
            for e in desk.overview(
                principal=session.principal_id, tenant=session.tenant_id
            )
        }
        entry = entries.get(node_id)
        try:
            feed = desk.activity(node_id, tenant=session.tenant_id)
        except ContributionError as exc:
            raise GatewayError(404, "not_found", str(exc)) from exc
        title = entry.title if entry else node_id[:8]
        items = [
            {**r.model_dump(mode="json"), "node_title": title} for r in feed
        ]
        if entry is not None and entry.account.is_supernode:
            for member in entries.values():
                if member.account.supernode_id != node_id:
                    continue
                try:
                    member_feed = desk.activity(
                        member.node_id, tenant=session.tenant_id
                    )
                except ContributionError:
                    continue
                items.extend(
                    {**r.model_dump(mode="json"), "node_title": member.title}
                    for r in member_feed
                )
            items.sort(
                key=lambda r: max((s["at"] for s in r["steps"]), default=""),
                reverse=True,
            )
            items = items[:20]
        self._save_daily_node_log(request, session, node_id, items)
        return json_response(200, {"items": items})

    # What each node's daily execution log files live under.
    _LOG_FOLDER = "logs"
    _LOG_NAME_RE = re.compile(r"^execution-(\d{4}-\d{2}-\d{2})\.log$")

    def _save_daily_node_log(
        self, request, session, node_id: str, items: list[dict]
    ) -> None:
        """Materialize today's execution log file for the node and prune
        logs past the legal retention window.

        The file is the full-fidelity record (ISO timestamps, run ids,
        executing node, raw event types) — the UI simplifies, the file
        does not. Lines merge idempotently, so repeated fetches never
        duplicate an entry, and pruning follows the
        ``account.log_retention_days`` setting.
        """
        if self._files is None:
            return
        now = request.now or self._clock()
        today = now.date().isoformat()
        lines: set[str] = set()
        for item in items:
            for step in item.get("steps", []):
                at = str(step.get("at", ""))
                if not at.startswith(today):
                    continue
                lines.add(
                    f"{at}\t{item.get('run_id', '')}\t{step.get('seq', '')}\t"
                    f"{item.get('node_title', '')}\t{step.get('event_type', '')}"
                )
        existing = {
            f.name: f
            for f in self._files.list(tenant=session.tenant_id, node_id=node_id)
            if f.folder == self._LOG_FOLDER
        }
        if lines:
            name = f"execution-{today}.log"
            current = existing.get(name)
            if current is not None:
                lines |= {
                    line
                    for line in current.content.splitlines()
                    if line and not line.startswith("#")
                }
            content = (
                f"# Execution log — {today} — kept for legal use\n"
                + "\n".join(sorted(lines))
            )
            if current is not None:
                if current.content != content:
                    self._files.save(current.model_copy(update={"content": content}))
            else:
                self._files.save(
                    UserFile(
                        tenant_id=session.tenant_id,
                        node_id=node_id,
                        name=name,
                        folder=self._LOG_FOLDER,
                        media_type="text/plain",
                        content=content,
                    )
                )
        retention = 180
        if self._settings is not None:
            retention = int(
                float(
                    self._settings.effective(session.tenant_id).get(
                        "account.log_retention_days", 180
                    )
                    or 180
                )
            )
        for name, file in existing.items():
            match = self._LOG_NAME_RE.match(name)
            if match is None:
                continue
            try:
                aged = (now.date() - date.fromisoformat(match.group(1))).days
            except ValueError:
                continue
            if aged > retention:
                self._files.delete(file.file_id, tenant=session.tenant_id)

    # ------------------------------------------------------------------ #
    # Supernode KYC: verified legal entities earn global trust.           #
    # ------------------------------------------------------------------ #
    def _require_kyc(self):
        if self._kyc is None:
            raise GatewayError(404, "not_found", "KYC is not enabled here")
        return self._kyc

    def _kyc_status(self, request, session, params) -> Response:
        kyc = self._require_kyc()
        record = kyc.status_for(params["node_id"])
        if record is not None and record.tenant != session.tenant_id:
            record = None  # another tenant's application does not exist here
        return json_response(
            200,
            {
                "application": (
                    record.model_dump(mode="json") if record else None
                ),
                # What ranking actually multiplies by — own verification
                # or the nearest verified Supernode above.
                "trust_multiplier": kyc.trust_multiplier(params["node_id"]),
                # KYC binds only on the Global service; an Edge install's
                # Supernodes need no verification and no subscription.
                "required": bool(self._config.global_service),
            },
        )

    def _kyc_apply(self, request, session, params) -> Response:
        """A Supernode obeys the KYC policy: apply as a legal entity.

        The deterministic screen runs here — a personal mailbox is refused
        with a 400 before anything is stored; trusted company domains are
        fast-tracked; the paying-plan gate answers 402. KYC binds only on
        the GLOBAL service, where a verified Supernode serves the whole
        ecosystem with a higher trust score; an Edge install (this device
        or a private network) refuses the application as unnecessary."""
        kyc = self._require_kyc()
        if not self._config.global_service:
            raise GatewayError(
                409,
                "conflict",
                "KYC applies to Supernodes serving the Global ecosystem — "
                "an Edge install needs no verification and no subscription",
            )
        body = request.body or {}
        try:
            record = kyc.apply(
                params["node_id"],
                tenant=session.tenant_id,
                principal=session.principal_id,
                legal_name=str(body.get("legal_name", "")),
                company_email=str(body.get("company_email", "")),
                registration_no=str(body.get("registration_no", "")),
            )
        except SubscriptionRequired as exc:
            raise GatewayError(402, "subscription_required", str(exc)) from exc
        except OwnershipError as exc:
            raise GatewayError(403, "forbidden", str(exc)) from exc
        except ContributionError as exc:
            raise GatewayError(404, "not_found", str(exc)) from exc
        except ValueError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        self._durable.audit.append(
            "kyc.applied",
            {
                "run_id": f"kyc:{record.node_id}",
                "node_id": record.node_id,
                "legal_name": record.legal_name,
                "screen": record.screen.value,
                "applicant": session.principal_id,
            },
        )
        return json_response(201, record.model_dump(mode="json"))

    def _kyc_decide(self, request, session, params) -> Response:
        """A human reviewer's verdict — approve authority required, the
        decision audited. The screen sorted the queue; a person decides."""
        kyc = self._require_kyc()
        if self._approval is None:
            raise GatewayError(404, "not_found", "approval authority is not configured")
        current = kyc.status_for(params["node_id"])
        if current is None or current.tenant != session.tenant_id:
            raise GatewayError(404, "not_found", "no KYC application here")
        body = request.body or {}
        if "approved" not in body:
            raise GatewayError(
                400, "invalid_request", "approved (true or false) is required"
            )
        try:
            self._approval.approve(
                session,
                run_id=f"kyc:{params['node_id']}",
                policy="kyc.review",
                requester_id=current.applicant,
                required_assurance=int(body.get("required_assurance", 1)),
                now=request.now or self._clock(),
            )
        except AuthorizationError as exc:
            raise GatewayError(403, "forbidden", str(exc)) from exc
        try:
            record = kyc.decide(
                params["node_id"],
                reviewer=session.principal_id,
                approved=bool(body["approved"]),
                note=str(body.get("note", "")),
            )
        except ContributionError as exc:
            raise GatewayError(404, "not_found", str(exc)) from exc
        except ValueError as exc:
            raise GatewayError(409, "conflict", str(exc)) from exc
        self._durable.audit.append(
            "kyc.decided",
            {
                "run_id": f"kyc:{record.node_id}",
                "node_id": record.node_id,
                "status": record.status.value,
                "multiplier": record.multiplier,
                "reviewer": session.principal_id,
                "note": record.decision_note,
            },
        )
        return json_response(200, record.model_dump(mode="json"))

    def _kyc_reviews(self, request, session, params) -> Response:
        """The reviewer's inbox: applications awaiting a verdict, fast-
        tracked first, oldest first. Tenant-scoped like the decide route —
        a reviewer sees their own tenant's queue."""
        kyc = self._require_kyc()
        pending = [
            record.model_dump(mode="json")
            for record in kyc.pending()
            if record.tenant == session.tenant_id
        ]
        return json_response(200, {"items": pending})

    # ------------------------------------------------------------------ #
    # Node hygiene: the policy agreed upfront, and its enforcement.       #
    # ------------------------------------------------------------------ #
    def _node_policy(self, request, session, params) -> Response:
        return json_response(
            200, {"version": NODE_POLICY_VERSION, "text": NODE_POLICY}
        )

    def _require_hygiene(self):
        if self._hygiene is None:
            raise GatewayError(404, "not_found", "hygiene is not enabled here")
        return self._hygiene

    def _hygiene_inspect(self, request, session, params) -> Response:
        """Detect only: what the sweep would do, without doing it."""
        hygiene = self._require_hygiene()
        return json_response(
            200,
            {"items": [f.model_dump(mode="json") for f in hygiene.inspect()]},
        )

    def _hygiene_sweep(self, request, session, params) -> Response:
        """Enforce the Node Policy: revoke clones, restrict fraud and
        zombies. A platform move — approve authority required — and every
        action lands in the audit trail."""
        hygiene = self._require_hygiene()
        if self._approval is None:
            raise GatewayError(404, "not_found", "approval authority is not configured")
        try:
            self._approval.approve(
                session,
                run_id="hygiene:sweep",
                policy="hygiene.sweep",
                requester_id="",
                now=request.now or self._clock(),
            )
        except AuthorizationError as exc:
            raise GatewayError(403, "forbidden", str(exc)) from exc
        acted = hygiene.sweep()
        for finding in acted:
            self._durable.audit.append(
                f"hygiene.{finding.action}",
                {
                    "run_id": f"hygiene:{finding.node_id}",
                    "node_id": finding.node_id,
                    "kind": finding.kind.value,
                    "evidence": finding.evidence,
                    "by": session.principal_id,
                },
            )
        return json_response(
            200, {"items": [f.model_dump(mode="json") for f in acted]}
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
    def _parse_inputs(raw) -> list | None:
        if raw is None:
            return None
        if not isinstance(raw, list):
            raise GatewayError(400, "invalid_request", "inputs must be a list")
        from ..skills.contract import ValueInput

        try:
            return [ValueInput.model_validate(item) for item in raw]
        except Exception as exc:
            raise GatewayError(400, "invalid_request", f"bad input: {exc}") from exc

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
            # A model's opinion enters picks as a prior; what the advice
            # cost rides the preview and the budget verdict below.
            proposal_model=self._proposal_model_for(session),
            cost_weight=self._cost_weight(body),
            budget=self._budget_policy(body),
            spend_lookup=lambda goal_class: self._spend_history(session, goal_class),
            wallet_balance=self._wallet_balance(session),
        )
        return json_response(200, preview.model_dump(mode="json"))

    # ------------------------------------------------------------------ #
    # Budget signals: what the caller declared, what the user has done,   #
    # and what the (possibly partial) linked wallet holds.                #
    # ------------------------------------------------------------------ #
    def _proposal_model_for(self, session):
        """The proposal model for one request. An explicitly configured
        model wins; with none, the calling tenant's own recorded runs
        advise (free, evidence-only) — constructed per request because
        the evidence pool is the TENANT's history, never a neighbor's."""
        if self._proposal_model is not None:
            return self._proposal_model
        if self._trace_store is None:
            return None
        from ..orchestrator.proposals import TraceProposalModel

        return TraceProposalModel(self._trace_store, context=session.tenant_id)

    @staticmethod
    def _cost_weight(body: dict) -> float:
        raw = body.get("cost_weight", 0.0)
        try:
            weight = float(raw)
        except (TypeError, ValueError):
            raise GatewayError(
                400, "invalid_request", "cost_weight must be a number"
            ) from None
        if weight < 0.0:
            raise GatewayError(400, "invalid_request", "cost_weight must be >= 0")
        return weight

    @staticmethod
    def _budget_policy(body: dict) -> BudgetPolicy | None:
        raw = body.get("budget")
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise GatewayError(400, "invalid_request", "budget must be an object")
        try:
            return BudgetPolicy.model_validate(raw)
        except Exception as exc:
            raise GatewayError(400, "invalid_request", f"bad budget: {exc}") from exc

    def _spend_history(
        self, session, goal_class: str | None = None
    ) -> list[float] | None:
        if self._attribution is None:
            return None
        return self._attribution.consumer_spend(
            session.tenant_id, session.principal_id, goal_class=goal_class
        )

    def _wallet_balance(self, session) -> float | None:
        if self._wallet_lookup is None:
            return None
        return self._wallet_lookup(session.tenant_id, session.principal_id)

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
        # Creative inputs fill BEFORE anything compiles: user-provided
        # values outrank the patcher, the patcher outranks declared
        # defaults — and a held reserved contract stores the CONCRETE
        # values, so an approver decides on what will actually run.
        user_inputs = body.get("inputs") or {}
        if not isinstance(user_inputs, dict):
            raise GatewayError(400, "invalid_request", "inputs must be an object")
        patch_cost = 0.0
        try:
            manifest = inputs_manifest(contract)
            if manifest:
                filled = patch_or_defaults(
                    self._value_patcher, goal=contract.name, manifest=manifest
                )
                patch_cost = filled.cost
                contract = bind_inputs(contract, {**filled.values, **user_inputs})
        except ValueError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        try:
            compiled = compile_contract(contract)
        except ValueError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        reserved = reserved_operations(compiled)
        children = (
            contract.body.nodes
            if isinstance(contract.body, SubgraphBody)
            else [contract]
        )
        if self._hygiene is not None:
            # The Node Policy's restriction is real: a restricted (or
            # revoked) node refuses new contract runs outright.
            blocked = self._hygiene.restricted_versions(
                [c.id for c in children]
            )
            if blocked:
                raise GatewayError(
                    409,
                    "restricted",
                    "restricted under the Node Policy (clone/fraud/zombie) "
                    "and cannot take new runs: " + ", ".join(sorted(blocked)),
                )
        if self._desk is not None:
            # An audit node never runs unattended: its presence holds the
            # contract for a manual commit exactly like a reserved action.
            reserved = sorted(
                {*reserved, *self._desk.audit_holds_for([c.id for c in children])}
            )
        if reserved:
            # Not a dead end: hold it durably, tenant-scoped, for an
            # authorized approver (POST /v1/runs/contract/holds/{id}).
            policy = self._budget_policy(body)

            def hold() -> dict:
                pending_id = uuid4().hex
                now = request.now or self._clock()
                ttl = self._config.contract_hold_ttl_seconds
                expires_at = now + timedelta(seconds=ttl) if ttl is not None else None
                self._holds.add(
                    PendingContractRecord(
                        pending_id=pending_id,
                        contract=contract.model_dump(mode="json"),
                        reserved=reserved,
                        consumer_tenant=session.tenant_id,
                        consumer_principal=session.principal_id,
                        budget_cap=policy.hard_cap if policy else None,
                        review_threshold=(policy.review_threshold if policy else None),
                        review_acknowledged=bool(
                            body.get("review_acknowledged", False)
                        ),
                        created_at=now,
                        expires_at=expires_at,
                    )
                )
                self._compiled_holds[pending_id] = (contract, compiled)
                self._metrics["contract_holds"] += 1
                # The event approvers are notified by (the holds SSE
                # stream is derived from these audit records).
                self._durable.audit.append(
                    "contract.held",
                    {
                        "pending_id": pending_id,
                        "tenant": session.tenant_id,
                        "submitted_by": session.principal_id,
                        "name": contract.name,
                        "reserved": reserved,
                        "expires_at": (
                            expires_at.isoformat() if expires_at is not None else None
                        ),
                    },
                )
                return {
                    "pending_id": pending_id,
                    "status": "awaiting_approval",
                    "reserved": reserved,
                    "expires_at": (
                        expires_at.isoformat() if expires_at is not None else None
                    ),
                }

            key = request.header("idempotency-key")
            held = (
                self._idem.run(
                    f"gw:contract-hold:{session.tenant_id}:{key}",
                    hold,
                    scope="gateway",
                )
                if key
                else hold()
            )
            return json_response(202, held)

        # Budget gate BEFORE anything commits: estimate in preview mode,
        # judge it against the cap, the review threshold, the tenant's own
        # spending behavior (within this plan's class of goal), and the
        # (possibly partial) linked wallet.
        estimate = estimate_contract_gross(
            contract, assembler=assembler, price_book=book
        )
        verdict = assess_budget(
            # Creative help is spend too: the patcher's metered model
            # call rides the same budget gate as the market gross.
            estimate.gross + patch_cost,
            policy=self._budget_policy(body),
            spend_history=self._spend_history(session),
            class_history=(
                self._spend_history(session, estimate.goal_class)
                if estimate.goal_class is not None
                else None
            ),
            goal_class=estimate.goal_class,
            wallet_balance=self._wallet_balance(session),
        )
        try:
            enforce_budget(
                verdict,
                review_acknowledged=bool(body.get("review_acknowledged", False)),
            )
        except BudgetExceededError as exc:
            raise GatewayError(402, "budget_exceeded", str(exc)) from exc
        except ReviewRequiredError as exc:
            raise GatewayError(409, "review_required", str(exc)) from exc

        # Children of nodes that forbid data reuse are excluded from trace
        # learning BEFORE anything runs.
        trace_exclude: frozenset[str] = frozenset()
        if self._desk is not None:
            children = (
                contract.body.nodes
                if isinstance(contract.body, SubgraphBody)
                else [contract]
            )
            blocked = self._desk.autodev_blocked([c.id for c in children])
            trace_exclude = frozenset(
                c.name for c in children if c.id in blocked
            )

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
                trace_exclude=trace_exclude,
            )
            self._metrics["contract_runs"] += 1
            payload = result.model_dump(mode="json")
            payload["budget"] = verdict.model_dump(mode="json")
            payload["patch_cost"] = patch_cost
            return payload

        key = request.header("idempotency-key")
        result = (
            self._idem.run(
                f"gw:contract:{session.tenant_id}:{key}", submit, scope="gateway"
            )
            if key
            else submit()
        )
        return json_response(200, result)

    def _sweep_holds(self, request) -> set[str]:
        """Lazily expire stale holds; every sweep is audited per hold."""
        swept = self._holds.sweep_expired(request.now or self._clock())
        for record in swept:
            self._compiled_holds.pop(record.pending_id, None)
            self._metrics["contract_holds_expired"] += 1
            self._durable.audit.append(
                "contract.expired",
                {
                    "pending_id": record.pending_id,
                    "tenant": record.consumer_tenant,
                    "submitted_by": record.consumer_principal,
                    "reserved": record.reserved,
                },
            )
        return {record.pending_id for record in swept}

    def _hold_events(self, request, session, params) -> Response:
        """SSE snapshot of the tenant's hold lifecycle — the approver's feed.

        Same snapshot semantics as the per-run event stream: derived from
        the audit log, so held/approved/declined/expired all surface in
        order and nothing is invented for the transport. Each frame carries
        ``id: <seq>``; pass ``?after=<seq>`` to resume past what you have
        already seen (SSE Last-Event-ID semantics). The request itself
        sweeps, so an expiry becomes an event, never silence.
        """
        self._sweep_holds(request)
        try:
            after = int(request.query.get("after", "0"))
        except ValueError as exc:
            raise GatewayError(
                400, "invalid_request", "after must be an integer seq"
            ) from exc
        frames = []
        for record in self._durable.audit.records():
            if record.event_type not in _HOLD_EVENT_TYPES:
                continue
            if record.payload.get("tenant") != session.tenant_id:
                continue
            if record.seq <= after:
                continue
            frames.append(
                f"id: {record.seq}\nevent: {record.event_type}\ndata: "
                + json.dumps(record.payload)
                + "\n"
            )
        return Response(
            status=200, body="\n".join(frames) + "\n", content_type="text/event-stream"
        )

    def _list_contract_holds(self, request, session, params) -> Response:
        """Reserved contracts held for approval — the caller's tenant only."""
        self._sweep_holds(request)
        items = [
            {
                "pending_id": record.pending_id,
                "name": str(record.contract.get("name", "contract")),
                "reserved": record.reserved,
                "submitted_by": record.consumer_principal,
                "created_at": record.created_at.isoformat(),
                "expires_at": (
                    record.expires_at.isoformat()
                    if record.expires_at is not None
                    else None
                ),
                "replies": self._holds.replies(record.pending_id),
            }
            for record in self._holds.list(tenant=session.tenant_id)
        ]
        return json_response(200, {"items": items})

    def _reply_contract_hold(self, request, session, params) -> Response:
        """Type and send an answer on a held request — the third option
        beside allowing and rejecting: the human in control talks back to
        whoever submitted it, without deciding yet."""
        record = self._holds.get(params["pending_id"])
        if record is None or record.consumer_tenant != session.tenant_id:
            raise GatewayError(404, "not_found", "no such held contract")
        body = request.body or {}
        message = str(body.get("message", "")).strip()
        if not message:
            raise GatewayError(400, "invalid_request", "message is required")
        moment = request.now or self._clock()
        self._holds.add_reply(
            record.pending_id,
            author=session.principal_id,
            message=message,
            at=moment,
        )
        self._durable.audit.append(
            "contract.hold.reply",
            {
                "pending_id": record.pending_id,
                "tenant": record.consumer_tenant,
                "by": session.principal_id,
                "message": message,
            },
        )
        return json_response(
            200,
            {
                "pending_id": record.pending_id,
                "replies": self._holds.replies(record.pending_id),
            },
        )

    def _decide_contract_hold(self, request, session, params) -> Response:
        """Decide a held reserved contract — approval mints from identity.

        Tenant-scoped (another tenant's hold is a 404, never a 403 that
        leaks its existence). Approval requires approve authority in the
        hold's tenant, re-runs the budget gate on the SUBMITTER's terms and
        histories (prices may have moved while held; approval grants the
        reserved actions, not the money), and executes with the run bound
        to the ORIGINAL submitter — the approver authorizes, never earns
        the consumer seat. Declining removes the hold. Both outcomes are
        audited with the decider's principal.
        """
        swept = self._sweep_holds(request)
        if params["pending_id"] in swept:
            raise GatewayError(410, "expired", "the hold expired before it was decided")
        record = self._holds.get(params["pending_id"])
        if record is None or record.consumer_tenant != session.tenant_id:
            raise GatewayError(404, "not_found", "no such held contract")
        body = request.body or {}
        if "approved" not in body:
            raise GatewayError(
                400, "invalid_request", "approved (true or false) is required"
            )
        pending_id = record.pending_id
        if not bool(body["approved"]):
            self._holds.remove(pending_id)
            self._compiled_holds.pop(pending_id, None)
            self._durable.audit.append(
                "contract.declined",
                {
                    "pending_id": pending_id,
                    "tenant": record.consumer_tenant,
                    "by": session.principal_id,
                },
            )
            return json_response(200, {"pending_id": pending_id, "status": "declined"})
        if self._contract_runner is None:
            raise GatewayError(404, "not_found", "contract execution is not enabled")
        assembler, book = self._require_market()
        if self._attribution is None:
            raise GatewayError(404, "not_found", "market economics are not enabled")
        if self._approval is None:
            raise GatewayError(404, "not_found", "approval authority is not configured")
        cached = self._compiled_holds.get(pending_id)
        if cached is None:  # a hold from before a restart: recompile once
            parsed = NodeContract.model_validate(record.contract)
            cached = (parsed, compile_contract(parsed))
            self._compiled_holds[pending_id] = cached
        parsed, compiled = cached
        try:
            approval = self._approval.approve(
                session,
                run_id=pending_id,
                policy=parsed.name,
                requester_id=record.consumer_principal or "",
                required_assurance=int(body.get("required_assurance", 1)),
                now=request.now or self._clock(),
            )
        except AuthorizationError as exc:
            raise GatewayError(403, "forbidden", str(exc)) from exc
        estimate = estimate_contract_gross(parsed, assembler=assembler, price_book=book)
        verdict = assess_budget(
            estimate.gross,
            policy=BudgetPolicy(
                hard_cap=record.budget_cap,
                review_threshold=record.review_threshold,
            ),
            spend_history=self._attribution.consumer_spend(
                record.consumer_tenant, record.consumer_principal
            ),
            class_history=(
                self._attribution.consumer_spend(
                    record.consumer_tenant,
                    record.consumer_principal,
                    goal_class=estimate.goal_class,
                )
                if estimate.goal_class is not None
                else None
            ),
            goal_class=estimate.goal_class,
            wallet_balance=(
                self._wallet_lookup(record.consumer_tenant, record.consumer_principal)
                if self._wallet_lookup is not None
                else None
            ),
        )
        try:
            enforce_budget(verdict, review_acknowledged=record.review_acknowledged)
        except BudgetExceededError as exc:
            raise GatewayError(402, "budget_exceeded", str(exc)) from exc
        except ReviewRequiredError as exc:
            raise GatewayError(409, "review_required", str(exc)) from exc
        result = execute_contract(
            parsed,
            compiled,
            runner=self._contract_runner,
            assembler=assembler,
            price_book=book,
            attribution=self._attribution,
            audit=self._durable.audit,
            consumer_tenant=record.consumer_tenant,
            consumer_principal=record.consumer_principal,
            trace_store=self._trace_store,
            trace_context=record.consumer_tenant,
        )
        self._holds.remove(pending_id)
        self._compiled_holds.pop(pending_id, None)
        self._metrics["contract_runs"] += 1
        self._durable.audit.append(
            "contract.approved",
            {
                "pending_id": pending_id,
                "tenant": record.consumer_tenant,
                "run_id": result.run_id,
                "approval_id": approval.id,
                "by": session.principal_id,
                "reserved": record.reserved,
                # A deliberate, typed signature (audit nodes/Supernodes);
                # plain allows carry None. Either way `by` names the human.
                "signature": str(body.get("signature") or "") or None,
            },
        )
        payload = result.model_dump(mode="json")
        payload["pending_id"] = pending_id
        payload["budget"] = verdict.model_dump(mode="json")
        return json_response(200, payload)

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

    def _stripe_webhook(self, request, session, params) -> Response:
        """Real Stripe deliveries: Stripe-Signature over the raw payload.

        The oolu_event_id / oolu_batch_id our adapters attach as charge and
        transfer metadata come back on these events — that is how a refund
        finds the metering event it reverses and a payout confirmation
        finds its batch. Unknown event types are acknowledged (200) so
        Stripe stops retrying them; only bad signatures are refused."""
        if self._stripe_webhooks is None or self._disputes is None:
            raise GatewayError(404, "not_found", "Stripe webhooks are not enabled")
        body = request.body or {}
        raw = (
            request.raw
            if request.raw is not None
            else json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        )
        try:
            self._stripe_webhooks.verify(
                raw,
                request.header("stripe-signature"),
                now=request.now or self._clock(),
            )
        except WebhookError as exc:
            raise GatewayError(400, "invalid_webhook", str(exc)) from exc
        event_id = str(body.get("id") or "")
        if not event_id:
            raise GatewayError(400, "invalid_webhook", "event id is required")
        event_object = (body.get("data") or {}).get("object") or {}
        metadata = event_object.get("metadata") or {}

        def process() -> dict:
            event_type = str(body.get("type", ""))
            result: dict = {"handled": event_type}
            if event_type in ("charge.refunded", "charge.dispute.created"):
                oolu_event = metadata.get("oolu_event_id")
                if oolu_event:
                    self._disputes.refund(event_id=oolu_event, reason=event_type)
                    result["clawback_event_id"] = oolu_event
                else:
                    result["ignored"] = "no oolu_event_id metadata on charge"
            elif (
                event_type in ("transfer.paid", "transfer.failed", "payout.paid",
                               "payout.failed")
                and self._payout_store is not None
            ):
                batch = self._payout_store.get_batch(
                    str(metadata.get("oolu_batch_id", ""))
                )
                if batch is not None:
                    status = (
                        PayoutStatus.PAID
                        if event_type.endswith(".paid")
                        else PayoutStatus.FAILED
                    )
                    self._payout_store.update_batch(
                        batch.model_copy(
                            update={
                                "status": status,
                                "provider_ref": event_object.get("id"),
                            }
                        )
                    )
                    result["batch_id"] = batch.batch_id
                else:
                    result["ignored"] = "no matching payout batch"
            return result

        result = self._idem.run(f"stripe:{event_id}", process, scope="webhooks")
        return json_response(200, result)

    # ------------------------------------------------------------------ #
    # Local accounts: self-hosted multi-user login and management.        #
    # ------------------------------------------------------------------ #
    def _require_accounts(self):
        if self._accounts is None:
            raise GatewayError(404, "not_found", "local accounts are not configured")
        return self._accounts

    def _auth_login(self, request, session, params) -> Response:
        """Username + password in, a short-lived bearer token out.

        Public by nature; the account service equalizes timing between
        unknown users and wrong passwords, keeps the failure message
        uniform (no account enumeration), and locks a username briefly
        after repeated failures.
        """
        accounts = self._require_accounts()
        body = request.body or {}
        username = str(body.get("username", ""))
        password = str(body.get("password", ""))
        if not username or not password:
            raise GatewayError(
                400, "invalid_request", "username and password are required"
            )
        try:
            result = accounts.login(
                username, password, now=request.now or self._clock()
            )
        except AuthenticationError as exc:
            raise GatewayError(401, "unauthorized", str(exc)) from exc
        # A verification-first host holds the door until the address is
        # proven. Accounts without an e-mail link (the bootstrap admin,
        # operator-created users) are exempt — they never registered.
        if self._mail is not None and self._mail_codes is not None:
            email = (
                self._identity_links.email_of(username)
                if self._identity_links is not None
                else None
            )
            if email and not self._mail_codes.is_verified(email, "verify"):
                raise GatewayError(
                    403,
                    "verification_required",
                    "verify your e-mail first — we sent a code when you "
                    "registered (or use 'Forgot password?' to get a new one)",
                )
        return json_response(
            200,
            {
                "token": result.token,
                "expires_at": result.expires_at.isoformat(),
                "tenant": result.tenant_id,
                "principal": result.principal,
            },
        )

    # ------------------------------------------------------------------ #
    # Client config + self-serve registration.                            #
    # ------------------------------------------------------------------ #
    def _client_config(self, request, session, params) -> Response:
        """What a client should know before any sign-in: the online server
        this install pairs with (so the sign-in screen doesn't have to ask)
        and which doors this host offers. Public, secret-free."""
        return json_response(
            200,
            {
                "server": self._config.server_url,
                "google": self._google is not None,
                "registration": bool(
                    self._config.open_registration and self._accounts is not None
                ),
                # Whether registering here ends with a code-entry step.
                "verification": bool(
                    self._mail is not None and self._mail_codes is not None
                ),
            },
        )

    def _auth_register(self, request, session, params) -> Response:
        """Create an account from e-mail + password, where the host allows.

        The e-mail is recorded as an identity link so the same address
        cannot register twice; *verification* of the address arrives with
        the mail-sender milestone — until then hosts opt in knowingly via
        --open-registration (pre-launch testing)."""
        accounts = self._require_accounts()
        if not self._config.open_registration:
            raise GatewayError(
                404, "not_found", "registration is not open on this host"
            )
        body = request.body or {}
        email = str(body.get("email", "")).strip().lower()
        password = str(body.get("password", ""))
        if not _EMAIL_RE.match(email):
            raise GatewayError(400, "invalid_request", "a valid e-mail is required")
        if len(password) < 8:
            raise GatewayError(
                400, "invalid_request", "passwords need at least 8 characters"
            )
        if self._identity_links is not None and self._identity_links.lookup(
            "email", email
        ):
            raise GatewayError(
                409, "conflict", "this e-mail is already registered — sign in instead"
            )
        username = self._fresh_username(email, accounts)
        tenant = self._config.registration_tenant
        try:
            accounts.create_user(
                username, password, tenant=tenant, granted_by="self-registration"
            )
        except ValueError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        if self._identity_links is not None:
            self._identity_links.link(
                provider="email", subject=email, tenant=tenant,
                username=username, email=email, at=self._clock(),
            )
        # Verification-first where a mail sender exists: the account is
        # created but no token is minted until the code proves the address.
        if self._mail is not None and self._mail_codes is not None:
            code = self._mail_codes.issue(email, "verify")
            self._mail.send(
                to=email,
                subject="Your OoLu verification code",
                body=f"Your OoLu verification code is {code}. It expires in "
                "30 minutes. If you didn't sign up, ignore this mail.",
            )
            self._metrics["registrations"] += 1
            return json_response(
                201, {"verification_required": True, "email": email}
            )
        result = accounts.login(username, password, now=self._clock())
        self._metrics["registrations"] += 1
        return json_response(
            201,
            {
                "token": result.token,
                "expires_at": result.expires_at.isoformat(),
                "tenant": result.tenant_id,
                "principal": result.principal,
            },
        )

    @staticmethod
    def _fresh_username(email: str, accounts) -> str:
        base = username_from_email(email)
        candidate = base
        for suffix in range(2, 100):
            if accounts.user(candidate) is None:
                return candidate
            candidate = f"{base}-{suffix}"
        raise GatewayError(409, "conflict", "could not derive a free username")

    def _auth_verify(self, request, session, params) -> Response:
        """Prove the registered address: code + password → first token.

        The code alone never signs anyone in — the password rides along so
        a leaked inbox is not a leaked account.
        """
        accounts = self._require_accounts()
        if self._mail_codes is None:
            raise GatewayError(404, "not_found", "verification is not enabled")
        body = request.body or {}
        email = str(body.get("email", "")).strip().lower()
        code = str(body.get("code", "")).strip()
        password = str(body.get("password", ""))
        link = (
            self._identity_links.lookup("email", email)
            if self._identity_links is not None
            else None
        )
        if link is None or not self._mail_codes.redeem(email, "verify", code):
            raise GatewayError(
                400, "invalid_request", "that code is wrong or expired"
            )
        try:
            result = accounts.login(link["username"], password, now=self._clock())
        except AuthenticationError as exc:
            raise GatewayError(401, "unauthorized", str(exc)) from exc
        return json_response(
            200,
            {
                "token": result.token,
                "expires_at": result.expires_at.isoformat(),
                "tenant": result.tenant_id,
                "principal": result.principal,
            },
        )

    def _reset_request(self, request, session, params) -> Response:
        """Start a password reset. Always 202 — an unknown address looks
        exactly like a known one, so nothing enumerates accounts."""
        if self._mail is None or self._mail_codes is None:
            raise GatewayError(404, "not_found", "password reset is not enabled")
        body = request.body or {}
        email = str(body.get("email", "")).strip().lower()
        link = (
            self._identity_links.lookup("email", email)
            if self._identity_links is not None and _EMAIL_RE.match(email)
            else None
        )
        if link is not None:
            code = self._mail_codes.issue(email, "reset")
            self._mail.send(
                to=email,
                subject="Your OoLu password reset code",
                body=f"Your OoLu password reset code is {code}. It expires "
                "in 30 minutes. If you didn't ask for it, ignore this mail.",
            )
        return json_response(202, {"status": "sent"})

    def _reset_confirm(self, request, session, params) -> Response:
        """Finish a reset: a redeemed code sets the new password — and
        counts as address verification (control of the inbox was proven)."""
        accounts = self._require_accounts()
        if self._mail_codes is None:
            raise GatewayError(404, "not_found", "password reset is not enabled")
        body = request.body or {}
        email = str(body.get("email", "")).strip().lower()
        code = str(body.get("code", "")).strip()
        password = str(body.get("password", ""))
        if len(password) < 8:
            raise GatewayError(
                400, "invalid_request", "passwords need at least 8 characters"
            )
        link = (
            self._identity_links.lookup("email", email)
            if self._identity_links is not None
            else None
        )
        if link is None or not self._mail_codes.redeem(email, "reset", code):
            raise GatewayError(
                400, "invalid_request", "that code is wrong or expired"
            )
        accounts.change_password(link["username"], password)
        # Inbox control proven: the address counts as verified too.
        self._mail_codes.mark_verified(email, "verify")
        return json_response(200, {"status": "password_changed"})

    # ------------------------------------------------------------------ #
    # Sign in with Google.                                                #
    # ------------------------------------------------------------------ #
    def _require_google(self) -> GoogleSignIn:
        if self._google is None:
            raise GatewayError(
                404,
                "not_found",
                "Google sign-in is not configured on this host "
                "(set OOLU_GOOGLE_CLIENT_ID)",
            )
        return self._google

    def _google_redirect_uri(self, request) -> str:
        """Where Google sends the browser back: this same gateway.

        Derived from the Host header (the loopback bind on the desktop);
        an online host would front this with TLS and its own hostname."""
        host = request.header("host") or "127.0.0.1:8765"
        scheme = "https" if request.header("x-forwarded-proto") == "https" else "http"
        return f"{scheme}://{host}/v1/auth/google/callback"

    def _google_start(self, request, session, params) -> Response:
        google = self._require_google()
        begun = google.begin(self._google_redirect_uri(request))
        return json_response(200, begun)

    def _google_link(self, request, session, params) -> Response:
        """Attach Google to the signed-in account: the local-mode upgrade
        path. Same browser flow; on completion the flow logs into THIS
        account instead of creating one."""
        google = self._require_google()
        begun = google.begin(
            self._google_redirect_uri(request),
            link_to=(session.tenant_id, session.principal_id),
        )
        return json_response(200, begun)

    def _google_callback(self, request, session, params) -> Response:
        """The browser's landing: complete the exchange, show a plain page.

        The page never carries the session token — the app collects that
        through finish() on its own channel."""
        google = self._require_google()
        try:
            principal = google.callback(request.query)
            page = (
                "<!doctype html><meta charset='utf-8'><title>OoLu</title>"
                "<body style='font-family:system-ui;margin:3rem'>"
                f"<h2>Signed in as {_escape(principal)}.</h2>"
                "<p>You can close this window and return to OoLu.</p>"
            )
            return Response(status=200, body=page, content_type="text/html; charset=utf-8")
        except SignInError as exc:
            page = (
                "<!doctype html><meta charset='utf-8'><title>OoLu</title>"
                "<body style='font-family:system-ui;margin:3rem'>"
                f"<h2>Sign-in failed.</h2><p>{_escape(str(exc))}</p>"
                "<p>Close this window and try again from OoLu.</p>"
            )
            return Response(status=400, body=page, content_type="text/html; charset=utf-8")

    def _google_finish(self, request, session, params) -> Response:
        """The app's poll: pending until the browser leg lands, then the
        session token exactly once."""
        google = self._require_google()
        body = request.body or {}
        state = str(body.get("state", ""))
        if not state:
            raise GatewayError(400, "invalid_request", "state is required")
        try:
            return json_response(200, google.finish(state))
        except SignInError as exc:
            raise GatewayError(404, "not_found", str(exc)) from exc

    @staticmethod
    def _user_view(user) -> dict:
        return {
            "username": user.username,
            "roles": list(user.roles),
            "disabled": user.disabled,
            "created_at": user.created_at.isoformat(),
        }

    def _auth_list_users(self, request, session, params) -> Response:
        accounts = self._require_accounts()
        return json_response(
            200,
            {"items": [self._user_view(u) for u in accounts.users(session.tenant_id)]},
        )

    def _auth_create_user(self, request, session, params) -> Response:
        """Admins provision users in THEIR OWN tenant only — the tenant is
        taken from the session, never from the request body."""
        accounts = self._require_accounts()
        body = request.body or {}
        username = str(body.get("username", ""))
        password = str(body.get("password", ""))
        roles = body.get("roles", [])
        if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
            raise GatewayError(400, "invalid_request", "roles must be a string list")
        try:
            user = accounts.create_user(
                username,
                password,
                tenant=session.tenant_id,
                roles=tuple(roles),
                granted_by=session.principal_id,
            )
        except ValueError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        return json_response(201, self._user_view(user))

    def _auth_set_disabled(self, request, session, params) -> Response:
        accounts = self._require_accounts()
        body = request.body or {}
        if not isinstance(body.get("disabled"), bool):
            raise GatewayError(
                400, "invalid_request", "disabled (true or false) is required"
            )
        user = accounts.user(params["username"])
        # A user in another tenant is indistinguishable from a missing one.
        if user is None or user.tenant_id != session.tenant_id:
            raise GatewayError(404, "not_found", "user not found")
        accounts.set_disabled(user.username, body["disabled"])
        return json_response(200, self._user_view(accounts.user(user.username)))

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
            "user_retries": state.user_retries,
            "plan": _plan_view(state),
            "no_route": _no_route_view(state),
            "failure": _failure_view(state),
            "autobuild": self._autobuild_view(state),
        }

    def _autobuild_view(self, state: RunState) -> dict | None:
        """The auto-build consent check, run on EVERY failed/incident run —
        planning-time refusals and execution failures alike — so the switch
        that would unblock the run is always named at the moment it matters."""
        failing = state.phase is Phase.FAILED or (
            state.pause is not None and state.pause.kind is PauseKind.INCIDENT
        )
        if not failing or self._settings is None:
            return None
        tenant = str(state.contract.metadata.get("tenant_id", ""))
        consent = bool(
            self._settings.effective(tenant).get(AUTOBUILD_CONSENT_KEY, False)
        )
        return {
            "consent": consent,
            "hint": None if consent else AUTOBUILD_HINT,
        }
