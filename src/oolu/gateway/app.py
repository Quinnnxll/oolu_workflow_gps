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
import logging
import random
import re
import secrets
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from html import escape as _escape
from uuid import uuid4

from pydantic import ValidationError

from ..author import NodeAuthorAgent
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
    BUILDER_OFFER_NOTE,
    GROWTH_BUILD_INSTEAD,
    GROWTH_OFFER,
    GROWTH_REUSE_OFFER,
    REP_NEEDS_INFO_ASK,
    REP_WAITING_NOTE,
    WEB_SEARCH_NOTE,
    WEB_TASK_NOTE,
    ChatAssistant,
    ChatTurn,
    GatewayChatTools,
    ModelBudgetExceeded,
    ModelUnavailable,
    NodeChatTools,
    author_node_function,
    consent_answer,
    messaging_intent,
    mood_directive,
    obviously_chat,
    units_directive,
)
from ..durable.files import (
    FileTooLargeError,
    UserFile,
    UserFileStore,
    normalize_folder,
)
from ..durable.hooks import NodeHookStore
from ..durable.idempotency import IdempotencyLedger
from ..durable.offers import GrowthOfferStore
from ..durable.service import DurableWorkflowService
from ..identity.accounts import PendingPasswordStore
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
from ..mail import SendThrottle
from ..metering.attribution import AttributionStore
from ..metering.models import MeteringEvent
from ..metering.store import MeteringLedger
from ..naming import NEAR_GOAL_SIMILARITY, concise_name, goal_similarity
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
    SafetyViolation,
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
    stamp_egress_grants,
    utility,
)
from ..orchestrator import (
    DagRouteRunner,
    GoalSpec,
    OrchestratorError,
    patch_or_defaults,
)
from ..orchestrator.rebuild import AUTOBUILD_CONSENT_KEY, AUTOBUILD_HINT
from ..orchestrator.state import (
    PauseKind,
    Phase,
    ResumeInput,
    RunState,
    TaskContract,
)
from ..projectgraph import (
    FINDING_SEVERITIES,
    GraphProposal,
    GraphScopes,
    PatchOp,
    ProjectGraphStore,
    TransactionKernel,
    build_finding,
    path_covered,
)
from ..providers.chatmodel import CHAT_PURPOSE, ChatModelRouter
from ..providers.keyring import PROVIDERS, ModelKeyring
from ..providers.vault import SecretVault
from ..representative import pair_exchanges as pair_representative_exchanges
from ..runtime.bundle import BundleError, BundleStore
from ..seats import SEATS, DeskFiles, SeatViolation
from ..settings_node import SettingError, SettingsNode
from ..skills.contract import (
    ContractEdge,
    NodeContract,
    Slot,
    SubgraphBody,
    derive_data_edges,
)
from ..skills.inputs import bind_inputs, inputs_manifest
from ..skills.models import ActionEvent, ExecutionStatus, ReusableSkill
from ..skills.ports import ActionExecutor
from ..social import MAX_MESSAGE_CHARS
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

# The most a node-webhook caller may hand the fired run: a webhook carries
# an event, not a dataset — big payloads belong in the drawer's blob door.
_MAX_HOOK_PAYLOAD = 65_536

# How long a deleted node stays revivable before the purge makes the
# delete real — the accidental-delete safety window.
NODE_REVIVAL_DAYS = 7.0

# An explicit "build me a node ..." request in general chat. It REQUIRES the
# word "node" so a plain "build me a report" stays ordinary work — only a
# genuine node-build request is routed to the real builder (never the model,
# which cannot create a node and must not narrate that it did).
_NODE_BUILD_RE = re.compile(
    r"^\s*"
    # optional polite / addressing lead-in
    r"(?:(?:hey\s+)?oolu[,:]?\s+)?"
    r"(?:(?:please|can\s+you|could\s+you|would\s+you|will\s+you|"
    r"i(?:'d| would)?\s+(?:like|want)\s+you\s+to|i\s+want\s+to)\s+)?"
    r"(?:please\s+)?"
    r"(?:build|create|make|add|set\s+up)\s+(?:me\s+)?"
    r"(?:a|an|the|another)\s+node\b"
    r"\s*(?:for|that|to|which|:)?\s*(?P<goal>.*)$",
    re.IGNORECASE | re.DOTALL,
)


def explicit_node_build_goal(message: str | None) -> str | None:
    """The goal in an explicit node-build request, or ``None`` if it isn't
    one. Empty goal (bare "build me a node") returns ``""`` so the builder can
    answer "tell me what the node should do" instead of the model guessing."""
    match = _NODE_BUILD_RE.match(message or "")
    if match is None:
        return None
    return match.group("goal").strip(" .!?")


def _drawer_function(function: dict) -> dict:
    """The drawer's ``src/main.py`` IS the node's function when present.

    Building writes the authored function there; from then on the FILE is
    the home the runs read first, so a human (or a seated model) editing
    it edits the node — the version's JSON snapshot is the fallback for
    nodes whose drawer copy was deleted. The promoted file leaves the
    staged-files set (it becomes ``user_script.py`` itself, not a
    sibling), and the cache keys on the script's own fingerprint, so an
    edit takes effect on its very next run — still through the same
    safety screen and sandbox verification as any other code."""
    from ..runtime.polyglot import polyglot_entry, polyglot_wrapper

    files = dict(function.get("files") or {})
    main = files.pop("main.py", None)
    updated = {**function}
    if main:
        updated["script"] = str(main)
    else:
        # Mainstream languages behind the one contract: a drawer whose
        # entry is main.js / main.c / main.cpp / main.sh runs through a
        # generated Python wrapper that drives the toolchain in the same
        # sandbox and speaks emit_result for it. The source stays STAGED
        # (it is the program the wrapper runs), and the cache still keys
        # on the wrapper+files fingerprint, so an edit takes effect on
        # its next run.
        entry = polyglot_entry(files)
        if entry is not None:
            updated["script"] = polyglot_wrapper(entry)
    if files:
        updated["files"] = files
    else:
        updated.pop("files", None)
    return updated


def _tz_minutes(raw) -> int:
    """The client's timezone offset, minutes east of UTC — clamped to the
    real world's ±14 h and never trusted to be a number."""
    try:
        return max(-14 * 60, min(14 * 60, int(raw or 0)))
    except (TypeError, ValueError):
        return 0

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
    # Self-serve e-mail registration is ON by default — a server exists
    # to take accounts. Operators running a closed install turn it off
    # explicitly (--no-open-registration).
    open_registration: bool = True
    # Which tenant self-served accounts land in.
    registration_tenant: str = "main"
    # How long finished history stays on the books before the retention
    # pass trims it: terminal runs (the dead Noder threads nobody
    # revives), finished queue tasks, delivered outbox rows, and the
    # audit chain's oldest prefix (attested, so the chain still
    # verifies). 0 turns retention off. Live and paused work is never
    # touched — retention trims history, not work.
    retention_days: float = 45.0
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


# The file types the drawer speaks natively — the formats developers,
# creators, and engineers actually exchange. Text stays text; everything
# else rides as a data URL and is typed honestly by extension so viewers,
# players, and the download door all know what they are holding.
_MEDIA_TYPES: dict[str, str] = {
    ".py": "text/x-python",
    ".csv": "text/csv",
    ".tsv": "text/csv",
    ".json": "application/json",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".pdf": "application/pdf",
    ".docx": (
        "application/vnd.openxmlformats-officedocument"
        ".wordprocessingml.document"
    ),
    ".xlsx": (
        "application/vnd.openxmlformats-officedocument"
        ".spreadsheetml.sheet"
    ),
    ".pptx": (
        "application/vnd.openxmlformats-officedocument"
        ".presentationml.presentation"
    ),
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".mp4": "video/mp4",
    ".mp3": "audio/mpeg",
}


def _media_type_for(name: str) -> str:
    lowered = name.lower()
    for suffix, media_type in _MEDIA_TYPES.items():
        if lowered.endswith(suffix):
            return media_type
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
        metering: MeteringLedger | None = None,  # verified-run evidence:
        # personal runs through a node's own function record here, so a
        # built node can verify LOCALLY instead of waiting for a
        # marketplace binding that personal use never creates
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
        bundle_store: BundleStore | None = None,  # content-addressed src trees:
        # freeze a node's src/ tree once and ship its id, not its bytes
        bundle_tiers: list | None = None,  # warm/materialized accelerators the
        # sweep purges alongside dead manifests (the only remover on a
        # fleet-shared materialized root)
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
        metrics_store=None,  # telemetry.investor.MetricsSnapshotStore
        values=None,  # values.ValueStore: the exact-value reference layer
        provenance=None,  # nodeplace.NodeProvenance: immutable commits,
        # sealed releases, revocation — the build policy's ledgers
        stripe_webhooks=None,  # gateway.StripeWebhookVerifier: real Stripe
        # events land at /v1/webhooks/stripe only when this is configured
        google_signin: GoogleSignIn | None = None,  # "Continue with Google"
        identity_links: IdentityLinkStore | None = None,  # email/IdP -> account
        mail=None,  # mail.MailSender: verification + reset codes go out here
        mail_codes=None,  # mail.MailCodeStore: hashed one-time codes
        sms=None,  # sms.SmsSender: "continue with phone" codes + passwords
        totp=None,  # identity.TotpStore: the payment second factor
        payment_authorizations=None,  # billing.PaymentAuthorizationStore:
        # the order/booking consent gate (amount consent + TOTP)
        direct_messages=None,  # social.DirectMessageStore: friends talking
        friendships=None,  # social.FriendshipStore: requests, blocks, and
        # the stranger-message preference
        assistant_history=None,  # social.AssistantHistoryStore: one OoLu
        # thread per account, shared by every signed-in device
        representative=None,  # representative.RepresentativeEngine: drafts
        # replies in the account's own voice — never sends on its own
        reminders=None,  # reminders.ReminderStore: rows with a clock,
        # created deterministically and surfaced by the client's poll
        lessons=None,  # lessons.LessonStore: guided demonstrations —
        # goal + ordered steps + paired run logs — that build nodes
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
        self._metering = metering
        self._contract_runner = (
            DagRouteRunner(contract_executors) if contract_executors else None
        )
        # The raw hands too: the node author's verify gate borrows the
        # script executor directly for its sandbox dry-run.
        self._contract_executors = dict(contract_executors or {})
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
        self._bundle_store = bundle_store
        self._bundle_tiers = list(bundle_tiers or [])
        # The sweep's recurring Routine (durable, single-row, fleet-shared).
        from ..runtime.sweep import SweepScheduleStore

        self._sweep_schedule = SweepScheduleStore(durable.conn)
        # The tick's cheap gate: at most one due-check per minute per host.
        self._sweep_gate = 0.0
        # Retention's own gate: at most one pruning pass per hour per host.
        self._retention_gate = 0.0
        # Competitor intelligence, constructed on first use.
        self._competitors = None
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
        self._metrics_store = metrics_store
        self._values = values
        self._provenance = provenance
        self._stripe_webhooks = stripe_webhooks
        # Keyed (tenant, purpose): the conversation and the node author
        # ride separate routers so their consultations enter the books
        # under their own purposes — one brain, two accountable seats.
        self._model_routers: dict[tuple[str, str], ChatModelRouter] = {}
        # Standing growth offers (the n8n-style trigger): a chat task that
        # failed for want of a working function asks, in the conversation,
        # whether to build the missing node. One offer per person, and it
        # stands for exactly one message — the very next turn answers it.
        # The value is (kind, goal, original_goal): "build" builds and runs
        # ``goal``; "reuse" runs the near-match node's own ``goal`` (the
        # twin guard's reuse-first door), keeping the user's
        # ``original_goal`` so a "no" can roll into a distinct build offer;
        # "build_distinct" is that follow-up — the user already said this
        # is different work, so the twin guard steps aside. DURABLE on the
        # runtime's own connection: the question OoLu asked must survive a
        # restart, and the yes must land whichever process serves it.
        self._growth_offers = GrowthOfferStore(durable.conn)
        # Node webhooks: an outside system's door to ONE node's own
        # function — token-credentialed, owner-minted, digest-stored.
        self._node_hooks = NodeHookStore(durable.conn)
        # Forgot-password's staged key: the e-mailed password waits here
        # beside the real one — nobody is locked out by a stranger's
        # request — and the outbound doors are paced per address.
        self._pending_passwords = PendingPasswordStore(durable.conn)
        self._send_throttle = SendThrottle(durable.conn)
        # The Global Project Graph: typed, revisioned truth, changed ONLY
        # through the transaction kernel — every verdict lands in the
        # hash-chained audit log (docs/industrial-vertical-plan.md, 1–2).
        self._project_graph = ProjectGraphStore(durable.conn)
        self._graph_kernel = TransactionKernel(
            self._project_graph, audit=durable.audit.append
        )
        self._google = google_signin
        self._identity_links = identity_links
        self._mail = mail
        self._mail_codes = mail_codes
        self._sms = sms
        self._totp = totp
        self._payment_authorizations = payment_authorizations
        self._direct_messages = direct_messages
        self._friendships = friendships
        self._assistant_history = assistant_history
        self._representative = representative
        self._reminders = reminders
        self._lessons = lessons
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
        # The Routine's lazy tick, the same idiom as hold expiry: ordinary
        # traffic advances the clock. Gated to one due-check per minute per
        # host; the claim makes a whole fleet fire exactly once per due
        # interval; a tick failure never reaches the client.
        self._maybe_scheduled_sweep(request)
        try:
            response = self._route(request)
        except GatewayError as exc:
            self._metrics["errors"] += 1
            response = json_response(
                exc.status, {"error": {"code": exc.code, "message": exc.message}}
            )
        except Exception as exc:  # noqa: BLE001 — the last-resort net
            # A bug must never reach clients as a bare text/plain 500 that
            # breaks their JSON parsing. The full traceback goes to the
            # server log (docker compose logs oolu); the body names the
            # exception class so an operator can find it there.
            self._metrics["errors"] += 1
            logging.getLogger("oolu.gateway").exception(
                "unhandled error on %s %s", request.method, request.path
            )
            response = json_response(
                500,
                {
                    "error": {
                        "code": "internal",
                        "message": f"the server hit a bug"
                        f" ({exc.__class__.__name__}) — the server log has"
                        " the full story",
                    }
                },
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
    def authorize_chat_stream(self, request) -> Session:
        """Authenticate a chat-stream request the same way a normal route
        does — validated bearer token → session, honoring API-key scopes.
        Raises :class:`GatewayError`; the ASGI binding turns it into an
        error response before any stream headers are sent."""
        session, scopes = self._session_and_scopes(
            request.bearer_token(), request.now or self._clock()
        )
        if scopes is not None and not scope_allows(scopes, "POST", "/v1/chat"):
            raise GatewayError(403, "forbidden", "outside this API key's scopes")
        return session

    def chat_stream_run(self, request, session: "Session", emit) -> Response:
        """Run one chat turn, streaming the model's reasoning to ``emit`` as
        it thinks. Returns the same Response the blocking /v1/chat would — the
        binding sends it as the terminal ``done`` frame."""
        return self._chat_turn(request, session, {}, emit=emit)

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
        # Friend requests: finding someone sends a request they decide,
        # never an unsolicited message. Blocks and the stranger-message
        # preference live here too.
        r.add("GET", "/v1/friends/requests", self._friend_requests_list)
        r.add("POST", "/v1/friends/requests", self._friend_request_send)
        r.add(
            "POST", "/v1/friends/requests/{peer}", self._friend_request_decide
        )
        r.add("GET", "/v1/friends/settings", self._friend_settings_get)
        r.add("PUT", "/v1/friends/settings", self._friend_settings_put)
        r.add("GET", "/v1/friends/{peer}/messages", self._friend_messages)
        r.add("POST", "/v1/friends/{peer}/messages", self._friend_send)
        # The owner's own name note for a friend — how people remembered
        # each other before software: "Anna from the conference".
        r.add("PUT", "/v1/friends/{peer}/alias", self._friend_alias_put)
        r.add("PUT", "/v1/friends/{peer}/prefs", self._friend_prefs_put)
        r.add("DELETE", "/v1/friends/{peer}", self._friend_delete)
        r.add("PUT", "/v1/runs/{run_id}/prefs", self._run_prefs_put)
        r.add("POST", "/v1/work/nodes/{node_id}/assign", self._work_assign)
        r.add("PUT", "/v1/work/nodes/{node_id}/prefs", self._work_node_prefs_put)
        # The representative: drafts in the account's own voice. Drafts
        # are proposed, listed, and decided — nothing sends without the
        # user's word (docs/representative-plan.md, Phase 0).
        # Reminders: rows with a clock. The client's poll is the tick —
        # a ripe reminder surfaces as OoLu's own message and is marked
        # delivered exactly once.
        r.add("GET", "/v1/reminders", self._reminders_list)
        r.add("POST", "/v1/reminders", self._reminders_create)
        r.add(
            "POST",
            "/v1/reminders/{reminder_id}/delivered",
            self._reminder_delivered,
        )
        r.add("GET", "/v1/representative", self._representative_status)
        r.add("PUT", "/v1/representative", self._representative_configure)
        r.add("GET", "/v1/representative/drafts", self._representative_drafts)
        r.add("POST", "/v1/representative/drafts", self._representative_draft)
        r.add("POST", "/v1/representative/sweep", self._representative_sweep)
        r.add(
            "POST",
            "/v1/representative/drafts/{draft_id}",
            self._representative_decide,
        )
        r.add(
            "PUT",
            "/v1/representative/peers/{peer}",
            self._representative_peer_rule,
        )
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
        # Two-factor enrollment: the second lock on spending money.
        r.add("GET", "/v1/2fa", self._totp_status)
        r.add("POST", "/v1/2fa/enroll", self._totp_enroll)
        r.add("POST", "/v1/2fa/confirm", self._totp_confirm)
        r.add("DELETE", "/v1/2fa", self._totp_disable)
        # Order/booking payment consent: OoLu may spend money only through
        # this gate — the exact amount, re-confirmed, plus a TOTP code.
        r.add("GET", "/v1/payment-authorizations", self._payment_auths_list)
        r.add(
            "POST", "/v1/payment-authorizations", self._payment_auth_request
        )
        r.add(
            "POST",
            "/v1/payment-authorizations/{auth_id}",
            self._payment_auth_decide,
        )
        r.add("DELETE", "/v1/keys/model/{provider}", self._model_keys_remove)
        # This month's model usage for the caller's tenant, plus the plan's
        # included allowance when a hosted brain exists here.
        r.add("GET", "/v1/usage/model", self._model_usage_view)
        # The Global Project Graph: proposals in, truth out.
        r.add("POST", "/v1/graph/{project_id}/proposals", self._graph_propose)
        r.add("GET", "/v1/graph/{project_id}/proposals", self._graph_ledger)
        r.add("GET", "/v1/graph/{project_id}/objects", self._graph_objects)
        r.add(
            "GET",
            "/v1/graph/{project_id}/objects/{object_id}",
            self._graph_object,
        )
        r.add("POST", "/v1/graph/{project_id}/scopes", self._graph_grant)
        r.add("POST", "/v1/graph/{project_id}/findings", self._graph_find)
        r.add("GET", "/v1/graph/{project_id}/findings", self._graph_findings)
        r.add("GET", "/v1/files", self._files_list)
        r.add("POST", "/v1/files", self._files_create)
        # The blob door: raw bytes in (no base64, no JSON envelope), raw
        # bytes out — the shapes real PDFs, decks, and videos travel in.
        r.add("POST", "/v1/files/upload", self._files_upload)
        r.add("GET", "/v1/files/{file_id}", self._files_get)
        r.add("GET", "/v1/files/{file_id}/content", self._files_content)
        r.add("PUT", "/v1/files/{file_id}", self._files_update)
        r.add("DELETE", "/v1/files/{file_id}", self._files_delete)
        r.add("GET", "/v1/work/nodes", self._work_nodes)
        r.add("POST", "/v1/work/nodes/{node_id}/account", self._work_account)
        r.add("GET", "/v1/work/nodes/{node_id}/activity", self._work_activity)
        # The Supernode's template button: preview resolves the org
        # structure (deterministic-first) and apply imports the member
        # nodes — role by role, each with its essential function.
        r.add("GET", "/v1/work/nodes/{node_id}/template", self._org_template)
        r.add(
            "POST",
            "/v1/work/nodes/{node_id}/template",
            self._org_template_apply,
        )
        # The Supernode owner's SOP dial: where a member stands in the
        # org's execution order (serial by number, parallel on ties,
        # on-demand when unset). Mutable, owner-gated.
        r.add("POST", "/v1/work/nodes/{node_id}/order", self._work_order)
        # Imitate: a guided lesson recorded in the node's own window —
        # the user names the goal, describes each step, runs the real
        # work through the node (the execution logs pair automatically),
        # and the finished demonstration builds a capable node.
        r.add("GET", "/v1/work/nodes/{node_id}/imitate", self._imitate_status)
        r.add("POST", "/v1/work/nodes/{node_id}/imitate", self._imitate_start)
        r.add(
            "POST",
            "/v1/work/nodes/{node_id}/imitate/step",
            self._imitate_step,
        )
        r.add(
            "POST",
            "/v1/work/nodes/{node_id}/imitate/stop",
            self._imitate_stop,
        )
        # The node's webhook: the owner mints ONE token-credentialed URL;
        # an outside system POSTing to it fires the node's own function
        # with the payload staged as a file. Minting again rotates the
        # token; the fire door is public because the token IS the door.
        # Real deletion with an undo window: DELETE tombstones the node
        # (off the desk, off its Supernode's roster, out of resolution,
        # listing revoked); the administrator may revive it within the
        # window; the retention pass purges it for good after.
        r.add("DELETE", "/v1/work/nodes/{node_id}", self._work_node_delete)
        r.add(
            "POST", "/v1/work/nodes/{node_id}/revive", self._work_node_revive
        )
        r.add(
            "GET",
            "/v1/work/nodes/{node_id}/deleted-members",
            self._work_deleted_members,
        )
        # Node provenance: the drawer's immutable commit history, the
        # sealed releases verification produced, and the revocation door
        # — a vulnerable release is revoked, never silently modified.
        r.add("GET", "/v1/work/nodes/{node_id}/commits", self._node_commits)
        r.add("GET", "/v1/work/nodes/{node_id}/releases", self._node_releases)
        r.add(
            "POST",
            "/v1/work/nodes/{node_id}/releases/{release_id}/revoke",
            self._node_release_revoke,
        )
        r.add("GET", "/v1/work/nodes/{node_id}/hook", self._node_hook_status)
        r.add("POST", "/v1/work/nodes/{node_id}/hook", self._node_hook_mint)
        r.add("DELETE", "/v1/work/nodes/{node_id}/hook", self._node_hook_revoke)
        r.add(
            "POST",
            "/v1/hooks/nodes/{node_id}/{token}",
            self._node_hook_fire,
            public=True,
        )
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
        # The bundle inventory: every frozen tree, its size and age, and
        # whether a live node still freezes to it.
        r.add(
            "GET",
            "/v1/work/bundles",
            self._bundle_inventory,
            requires_permission="hygiene:sweep",
        )
        # The bundle sweep: reclaim the content-addressed store's dead
        # frozen trees. GET is a dry run (the plan); POST applies it under
        # approve authority, like the hygiene sweep.
        r.add(
            "GET",
            "/v1/work/bundles/sweep",
            self._bundle_sweep_inspect,
            requires_permission="hygiene:sweep",
        )
        r.add("POST", "/v1/work/bundles/sweep", self._bundle_sweep_apply)
        # The sweep's recurring Routine: enabling is the approved, audited
        # standing consent; each due firing runs under it (fleet-safe: one
        # host wins the claim); revoking stops the next firing cold.
        r.add(
            "GET",
            "/v1/work/bundles/schedule",
            self._sweep_schedule_view,
            requires_permission="hygiene:sweep",
        )
        r.add("POST", "/v1/work/bundles/schedule", self._sweep_schedule_set)
        r.add("DELETE", "/v1/work/bundles/schedule", self._sweep_schedule_clear)
        # The sweep's history: every consent granted or revoked and every
        # firing, straight off the hash-chained audit log.
        r.add(
            "GET",
            "/v1/work/bundles/audit",
            self._bundle_sweep_audit,
            requires_permission="hygiene:sweep",
        )
        # The platform's finance monitor: what every account DRAWS (model
        # API spend against its allowance) and what every noder EARNS
        # (execution revenue) — one screen for the operator, read straight
        # off the books. GET is permission-gated like the other operator
        # reads; the give-back POST is an approved, audited platform move.
        r.add(
            "GET",
            "/v1/platform/finance",
            self._platform_finance,
            requires_permission="finance:view",
        )
        r.add("POST", "/v1/platform/usage/giveback", self._usage_giveback)
        # The investor metrics tracker: the live catalog view, the daily
        # snapshot tick, the charted history, and the manual-record door
        # for sources the app cannot see (commits, SEO, capital raises).
        r.add(
            "GET",
            "/v1/platform/metrics",
            self._metrics_view,
            requires_permission="metrics:view",
        )
        r.add(
            "GET",
            "/v1/platform/metrics/history",
            self._metrics_history,
            requires_permission="metrics:view",
        )
        # Phase 1 of the panel matrix: the executive summary (actual,
        # previous period, growth, target, status per headline metric)
        # and the weighted investor scorecard.
        r.add(
            "GET",
            "/v1/platform/metrics/summary",
            self._metrics_summary,
            requires_permission="metrics:view",
        )
        r.add(
            "GET",
            "/v1/platform/metrics/scorecard",
            self._metrics_scorecard,
            requires_permission="metrics:view",
        )
        # Phase 2: signup-month cohorts straight from the run books.
        r.add(
            "GET",
            "/v1/platform/metrics/cohorts",
            self._metrics_cohorts,
            requires_permission="metrics:view",
        )
        # Phase 3: competitor intelligence (append-only observations →
        # the strategic comparison), deterministic scenario modeling,
        # and the automated investor report.
        r.add(
            "GET",
            "/v1/platform/competitors",
            self._competitors_view,
            requires_permission="metrics:view",
        )
        r.add("PUT", "/v1/platform/competitors", self._competitors_record)
        r.add(
            "POST",
            "/v1/platform/metrics/scenario",
            self._metrics_scenario,
            requires_permission="metrics:view",
        )
        r.add(
            "GET",
            "/v1/platform/metrics/report",
            self._metrics_report,
            requires_permission="metrics:view",
        )
        r.add(
            "POST",
            "/v1/platform/metrics/snapshot",
            self._metrics_snapshot,
            requires_permission="metrics:view",
        )
        r.add("PUT", "/v1/platform/metrics/{key}", self._metrics_record)
        # The exact-value reference layer: a run's result outputs filed
        # as immutable refs, and the deterministic renderer that puts
        # exact stored values into a model-shaped response.
        r.add("GET", "/v1/runs/{run_id}/values", self._run_values)
        r.add("GET", "/v1/runs/{run_id}/lineage", self._run_lineage)
        r.add("POST", "/v1/values/render", self._values_render)
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
        # Continue with phone: an SMS code signs you in — and creates the
        # account (auto-generated password texted over) when the number
        # is new. Hosts without an SMS sender answer 404 and the app
        # hides the button.
        r.add("POST", "/v1/auth/phone/start", self._phone_start, public=True)
        r.add("POST", "/v1/auth/phone/verify", self._phone_verify, public=True)
        r.add("POST", "/v1/auth/reset/request", self._reset_request, public=True)
        r.add("POST", "/v1/auth/reset/confirm", self._reset_confirm, public=True)
        # The one-step forgot-password: the server generates a NEW password,
        # sets it, and e-mails it — no code round-trip. Alongside the
        # code-based reset above, not instead of it.
        r.add("POST", "/v1/auth/reset/password", self._reset_email_password, public=True)
        # Sign in with Google (RFC 8252): the app begins and polls; only
        # the browser's leg touches Google. All three answer 404 when no
        # Google client is configured on this host.
        r.add("GET", "/v1/auth/google/start", self._google_start, public=True)
        r.add("GET", "/v1/auth/google/callback", self._google_callback, public=True)
        r.add("POST", "/v1/auth/google/finish", self._google_finish, public=True)
        # Attaching Google to the CALLER's account needs the caller.
        r.add("POST", "/v1/auth/google/link", self._google_link)
        # A signed-in account sets its own sign-in password — the door a
        # Google-created account uses to ALSO become a username+password
        # login it can use next time.
        r.add("POST", "/v1/auth/password", self._auth_set_password)
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

    def _chat_turn(self, request, session, params, *, emit=None) -> Response:
        """One conversational turn with the OoLu assistant.

        ``emit`` (streaming only) is a callback the model's ⟨think⟩ reasoning
        deltas are pushed to as they are generated; the returned Response is
        unchanged, so the blocking /v1/chat and the streaming /v1/chat/stream
        share this one implementation.

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
        rep_hands = None
        if self._files is not None:
            node_id = body.get("node_id")
            if node_id:
                tools, context_note = self._node_chat_tools(
                    request, session, str(node_id)
                )
            else:
                rep_hands = self._representative_chat_hands(session)
                tools = GatewayChatTools(
                    self._files,
                    tenant=session.tenant_id,
                    principal=session.principal_id,
                    durable=self._durable,
                    desk=self._desk,
                    settings=self._settings,
                    accounts=self._accounts,
                    direct_messages=self._direct_messages,
                    local_root=self._local_files_root,
                    # The friend-memory hands: find a friend by name, by
                    # the owner's own name note, by what was said, or by
                    # roughly when the friendship began.
                    friendships=self._friendships,
                    # The representative's conversation-side hands: list
                    # what waits on the user, redraft with their answer,
                    # or lay a message to rest — all gateway-walled.
                    representative=rep_hands,
                    # The reminder hands: rows with a clock, resolved in
                    # the USER's timezone (the client sends its offset)
                    # and confirmed from the stored row.
                    reminders=self._reminder_chat_hands(
                        session,
                        now=request.now or self._clock(),
                        tz_offset_minutes=_tz_minutes(
                            body.get("tz_offset_minutes")
                        ),
                    ),
                )
        # Inside a fleet member's interact window, the model consultation
        # is the ORG's draw: it rides under the "node.interact" purpose,
        # so the usage books charge the Supernode owner's account line,
        # never the visiting member's conversation.
        interact_purpose = None
        if body.get("node_id"):
            fleet = self._fleet_supernode(str(body.get("node_id")))
            if fleet is not None:
                interact_purpose = "node.interact"
        # The seam stays call-compatible: tests (and hosts) that stub
        # _tenant_model with a one-argument brain keep working; the
        # purpose rides only when a fleet is actually being metered.
        router = (
            self._tenant_model(session.tenant_id, purpose=interact_purpose)
            if interact_purpose
            else self._tenant_model(session.tenant_id)
        )
        # WHO this consultation is drawn by: inside a fleet interact the
        # org's owner (the account the books charge), otherwise the
        # speaking user — so shared-tenant gauges stay per person.
        router = self._seat_actor(
            router,
            (fleet.responsible or session.principal_id)
            if interact_purpose
            else session.principal_id,
        )
        # When the model really can search (an Anthropic path with the
        # web-search door open), the turn says so — otherwise a keyed
        # install claims it "can't browse" the questions it could answer
        # inline. Either way the turn carries the ENGINE's web truth: web
        # tasks are buildable — a node's function reaches the web through
        # the granted, host-guarded hand — so no model refuses them as
        # beyond the machine.
        searches = getattr(router, "web_search_ready", None)
        search_note = (
            WEB_SEARCH_NOTE if searches is not None and searches() else None
        )
        web_task_note = WEB_TASK_NOTE
        # And the builder's truth, always on: the engine can BUILD — real
        # program files, guarded web/API/webhook hands, self-repair — and
        # the model should OFFER that for repeatable chores (words only;
        # work starts on the user's yes, never on the offer).
        builder_note = BUILDER_OFFER_NOTE
        # OoLu's voice follows its mood: the client sends the avatar's
        # current mood, and the turn is coloured to match the face.
        mood_note = mood_directive(body.get("mood"))
        # The reply speaks the units the user thinks in: their explicit
        # preference wins; "auto" reads the account's spending currency — the
        # same stored signal the representative uses, so both agree.
        effective = (
            # Personal-first: the account's own units/currency, falling
            # back to the tenant layer, then the catalog defaults.
            self._settings.effective(session.tenant_id, session.principal_id)
            if self._settings is not None
            else {}
        )
        units_note = units_directive(
            effective.get("account.units", "auto"),
            currency=effective.get("account.currency", "USD"),
        )
        # Drafted replies waiting on the user's own knowledge: the turn is
        # told, so OoLu can raise ONE of them when the moment fits — the
        # tasks are gathered here, in conversation, never in the drafts.
        rep_note = None
        if rep_hands is not None:
            waiting = rep_hands.waiting()
            if waiting:
                rep_note = REP_WAITING_NOTE.format(n=len(waiting))
        # The clock, for time-shaped asks ("at 3pm"): the model reads the
        # user's local time from here and passes exact values to the
        # create_reminder tool — it has no clock of its own.
        turn_now = request.now or self._clock()
        local_now = turn_now + timedelta(
            minutes=_tz_minutes(body.get("tz_offset_minutes"))
        )
        time_note = (
            f"Current time: {turn_now:%Y-%m-%d %H:%M} UTC; the user's "
            f"local time is {local_now:%Y-%m-%d %H:%M}."
        )
        context_note = (
            "\n".join(
                n
                for n in (
                    context_note,
                    search_note,
                    web_task_note,
                    builder_note,
                    mood_note,
                    units_note,
                    rep_note,
                    time_note,
                )
                if n
            )
            or None
        )
        run = None
        turn = None
        in_node = bool(body.get("node_id"))
        if not in_node:
            # A standing growth offer is answered BEFORE anything else:
            # the user's plain yes IS the consent it asked for — scoped to
            # that one goal, one build. It stands for exactly one message;
            # any other reply withdraws it, because consent detached from
            # the question it answered is not consent.
            offer = self._growth_offers.pop(
                session.tenant_id, session.principal_id
            )
            if offer is not None:
                kind, offered_goal, original_goal = offer
                answer = consent_answer(message)
                if answer == "yes" and kind == "reuse":
                    turn, run = self._reuse_node_and_run(session, offered_goal)
                elif answer == "yes":
                    turn, run = self._grow_node_and_run(
                        session,
                        offered_goal,
                        # The user already said this is different work: the
                        # twin guard asked, was answered, and steps aside.
                        allow_twin=kind == "build_distinct",
                    )
                elif answer == "no" and kind == "reuse":
                    # Different work after all — the plain build offer
                    # follows, standing for exactly one message like every
                    # offer, and marked so the twin guard honors the answer.
                    self._growth_offers.put(
                        session.tenant_id,
                        session.principal_id,
                        kind="build_distinct",
                        goal=original_goal,
                        original_goal=original_goal,
                    )
                    turn = ChatTurn(
                        say=GROWTH_BUILD_INSTEAD.format(
                            name=concise_name(original_goal), goal=original_goal
                        ),
                        source="tool",
                    )
                elif answer == "no":
                    turn = ChatTurn(
                        say="Okay — leaving it as is. Ask me again whenever "
                        "you want it built.",
                        source="tool",
                    )
        # An explicit "build me a node …" is executed by the REAL builder, not
        # narrated by the model: it writes the function and persists the node
        # to My nodes (or refuses in words), so the reply can never claim a
        # build that no code performed.
        if turn is None and not in_node and self._nodeplace is not None:
            build_goal = explicit_node_build_goal(message)
            if build_goal is not None:
                built = self._build_function_node(session, build_goal)
                if built.startswith("error:"):
                    turn = ChatTurn(
                        say=f"I couldn't build that node: {built[7:].strip()}",
                        source="tool",
                    )
                else:
                    turn = ChatTurn(
                        say=built, source="tool", actions=[{"tool": "build_node"}]
                    )
        if turn is None:
            recent = [h for h in history if isinstance(h, dict)][-20:]
            if emit is not None:
                # Stream the model's reasoning to the client as it thinks;
                # the finalized turn is still built from the complete text.
                turn = self._chat.respond_streaming(
                    message,
                    history=recent,
                    sender=session.principal_id,
                    tools=tools,
                    model=router,
                    context=context_note,
                    on_reasoning=lambda delta: emit(
                        {"type": "reasoning", "delta": delta}
                    ),
                )
            else:
                turn = self._chat.respond(
                    message,
                    history=recent,
                    sender=session.principal_id,
                    tools=tools,
                    model=router,
                    context=context_note,
                )
        say = turn.say
        if turn.task:
            try:
                # With standing consent, the missing node is built BEFORE
                # the run — function written, node on the desk, the route
                # through it — instead of firing a doomed run first and
                # offering afterwards. No consent, no silent build: the
                # growth offer below still asks.
                built = None
                if not in_node:
                    built = self._autobuild_before_run(session, turn.task)
                if built:
                    say = f"{say} {built}".strip()
                run = self._start_intent_run(session, turn.task)
                self._metrics["chat_runs"] += 1
                # The run may have already failed DURING execution (submit
                # runs synchronously to the first pause or terminal phase).
                # The growth check must fire here too — not only on the
                # planning-time refusal below — so a failed execution names
                # the failing node and offers to grow what's missing.
                say = self._describe_run_failure(
                    say, run, autobuild_hint=in_node
                )
                if not in_node:
                    say = self._offer_growth(say, session, turn.task, run=run)
            except GatewayError as exc:
                if exc.code not in ("cannot_execute", "release_revoked"):
                    raise
                # The engine refused the plan: the assistant says so in the
                # conversation instead of the client showing a raw error —
                # and when growing a node could close the gap, it asks for
                # the user's consent instead of silently building.
                say = f"I can't run that on this machine yet — {exc.message}."
                if not in_node:
                    say = self._offer_growth(say, session, turn.task, run=None)
                elif self._settings is not None and not self._autobuild_consented(
                    session.tenant_id, session.principal_id
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
                # The model's own thinking, when it showed it — the UI
                # renders it dimmed so the user sees the work, not noise.
                "reasoning": turn.reasoning,
                # OoLu asking for one of the DEVICE's senses (location /
                # camera / file): the client renders a grant button — the
                # user decides, never a silent sensor read.
                "device": turn.device,
                # A value OoLu is copying to the user's clipboard because they
                # asked (e.g. a masked node ID they want in full) — the client
                # writes it, so the ID never has to be spoken aloud.
                "copy": getattr(turn, "copy", None),
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
        """Conversations first — then every ACCEPTED friend who has not
        said anything yet. A friendship exists from the moment of
        acceptance; an empty thread is a fresh start, not an absence."""
        store = self._require_direct_messages()
        items = store.conversations(
            tenant=session.tenant_id, principal=session.principal_id
        )
        aliases: dict[str, str] = {}
        since: dict[str, str] = {}
        if self._friendships is not None:
            aliases = self._friendships.aliases(
                tenant=session.tenant_id, owner=session.principal_id
            )
            since = self._friendships.friends_since(
                tenant=session.tenant_id, me=session.principal_id
            )
            spoken = {item["peer"] for item in items}
            for peer in self._friendships.friends_of(
                tenant=session.tenant_id, me=session.principal_id
            ):
                if peer in spoken:
                    continue
                items.append(
                    {
                        "peer": peer,
                        "unread": 0,
                        "last_text": "",
                        "last_from": None,
                        "last_at": "",
                    }
                )
        prefs: dict[str, dict] = {}
        if self._friendships is not None:
            prefs = self._friendships.prefs(
                tenant=session.tenant_id,
                owner=session.principal_id,
                kind="friend",
            )
        for item in items:
            item["alias"] = aliases.get(item["peer"], "")
            item["since"] = since.get(item["peer"], "")
            pref = prefs.get(item["peer"], {})
            item["pinned"] = bool(pref.get("pinned"))
            item["muted"] = bool(pref.get("muted"))
            # Hidden is a MOMENT, not a state: anything said after the
            # stamp brings the thread back by itself.
            item["hidden"] = _hidden_now(
                pref.get("hidden_at"), item.get("last_at") or ""
            )
        # The reading order of a messenger: pinned first, then the most
        # recently spoken — the newer, the upper; silent fresh friendships
        # sort by when the friendship began. Two stable passes: recency
        # first, then pinned rises without disturbing it.
        items.sort(
            key=lambda i: str(i.get("last_at") or i.get("since") or ""),
            reverse=True,
        )
        items.sort(key=lambda i: not i["pinned"])
        return json_response(200, {"items": items})

    def _friend_prefs_put(self, request, session, params) -> Response:
        """How this conversation sits in MY list — pin, mute, hide. Each
        field moves only when the body names it."""
        from ..social import FriendshipError

        friends = self._require_friendships()
        body = request.body or {}

        def _flag(name: str) -> bool | None:
            return bool(body[name]) if name in body else None

        try:
            pref = friends.set_pref(
                tenant=session.tenant_id,
                owner=session.principal_id,
                kind="friend",
                key=params["peer"],
                pinned=_flag("pinned"),
                muted=_flag("muted"),
                hidden=_flag("hidden"),
            )
        except FriendshipError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        return json_response(200, {"peer": params["peer"], **pref})

    def _friend_delete(self, request, session, params) -> Response:
        """Unfriend: the friendship and my private margins go; no block is
        laid, and the messages stay where they are — a deleted friendship
        is not a shredded history."""
        friends = self._require_friendships()
        friends.remove(
            tenant=session.tenant_id,
            me=session.principal_id,
            other=params["peer"],
        )
        # The thread leaves the list too — hidden as it stands, so it
        # returns only if this person speaks again (their messages are
        # never shredded, and neither is the door back in).
        friends.set_pref(
            tenant=session.tenant_id,
            owner=session.principal_id,
            kind="friend",
            key=params["peer"],
            hidden=True,
        )
        return json_response(200, {"peer": params["peer"], "relationship": "none"})

    def _run_prefs_put(self, request, session, params) -> Response:
        """The Noder list's margins: pin, mute, hide one run thread. The
        run must be the caller's own — the same visibility wall the list
        itself enforces."""
        from ..social import FriendshipError

        friends = self._require_friendships()
        run_id = params["run_id"]
        state = self._durable.runs.get(run_id)
        if (
            state is None
            or state.contract.metadata.get("tenant_id") != session.tenant_id
            or state.contract.submitted_by != session.principal_id
        ):
            raise GatewayError(404, "not_found", "no such run of yours")
        body = request.body or {}

        def _flag(name: str) -> bool | None:
            return bool(body[name]) if name in body else None

        try:
            pref = friends.set_pref(
                tenant=session.tenant_id,
                owner=session.principal_id,
                kind="run",
                key=run_id,
                pinned=_flag("pinned"),
                muted=_flag("muted"),
                hidden=_flag("hidden"),
            )
        except FriendshipError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        return json_response(200, {"run_id": run_id, **pref})

    def _friends_lookup(self, request, session, params) -> Response:
        """Find a person by EXACT username or e-mail — never a directory.
        A public host holds strangers; browsing the roster is not a thing."""
        self._require_direct_messages()
        query = str((request.body or {}).get("query", "")).strip()
        if not query:
            raise GatewayError(400, "invalid_request", "who are you looking for?")
        username = query
        if "@" in query and self._identity_links is not None:
            # Search the e-mail column, not the email-provider subject, so
            # accounts that arrived through Google (which links its email
            # too) are found by address just like e-mail registrations.
            found = self._identity_links.username_by_email(query)
            if found is None:
                raise GatewayError(404, "not_found", "no one by that address here")
            username = found
        username = self._friend_or_404(session, username)
        # Tell the searcher where they stand with this person, so the UI
        # can offer the right next step (add / accept / already friends).
        relationship = "none"
        if self._friendships is not None:
            relationship = self._friendships.relationship(
                tenant=session.tenant_id, me=session.principal_id, other=username
            )
        return json_response(
            200, {"username": username, "relationship": relationship}
        )

    def _require_friendships(self):
        if self._friendships is None:
            raise GatewayError(
                404, "not_found", "friend requests are not enabled on this host"
            )
        return self._friendships

    def _friend_requests_list(self, request, session, params) -> Response:
        friends = self._require_friendships()
        return json_response(
            200,
            {
                "items": friends.incoming(
                    tenant=session.tenant_id, me=session.principal_id
                )
            },
        )

    def _friend_request_send(self, request, session, params) -> Response:
        from ..social import FriendshipError

        friends = self._require_friendships()
        target = self._friend_or_404(
            session, str((request.body or {}).get("username", ""))
        )
        try:
            relationship = friends.request(
                tenant=session.tenant_id,
                requester=session.principal_id,
                target=target,
            )
        except FriendshipError as exc:
            raise GatewayError(400, "cannot_request", str(exc)) from exc
        return json_response(200, {"username": target, "relationship": relationship})

    def _friend_request_decide(self, request, session, params) -> Response:
        from ..social import FriendshipError

        friends = self._require_friendships()
        peer = self._friend_or_404(session, params["peer"])
        action = str((request.body or {}).get("action") or "")
        try:
            if action == "accept":
                friends.accept(
                    tenant=session.tenant_id, me=session.principal_id, requester=peer
                )
            elif action == "decline":
                friends.decline(
                    tenant=session.tenant_id, me=session.principal_id, requester=peer
                )
            elif action == "block":
                friends.block(
                    tenant=session.tenant_id, me=session.principal_id, other=peer
                )
            elif action == "unblock":
                friends.unblock(
                    tenant=session.tenant_id, me=session.principal_id, other=peer
                )
            else:
                raise GatewayError(
                    400, "invalid_request", "action must be accept, decline, block,"
                    " or unblock"
                )
        except FriendshipError as exc:
            raise GatewayError(400, "cannot_decide", str(exc)) from exc
        return json_response(
            200,
            {
                "username": peer,
                "relationship": friends.relationship(
                    tenant=session.tenant_id, me=session.principal_id, other=peer
                ),
            },
        )

    def _friend_settings_get(self, request, session, params) -> Response:
        friends = self._require_friendships()
        return json_response(
            200,
            {
                "allow_nonfriend_messages": friends.allow_nonfriend(
                    tenant=session.tenant_id, principal=session.principal_id
                )
            },
        )

    def _friend_settings_put(self, request, session, params) -> Response:
        friends = self._require_friendships()
        allow = (request.body or {}).get("allow_nonfriend_messages")
        if not isinstance(allow, bool):
            raise GatewayError(
                400, "invalid_request", "allow_nonfriend_messages must be true/false"
            )
        friends.set_allow_nonfriend(
            tenant=session.tenant_id, principal=session.principal_id, allow=allow
        )
        return json_response(200, {"allow_nonfriend_messages": allow})

    def _friend_alias_put(self, request, session, params) -> Response:
        """Rename a friend the old way — 'Anna from the conference' — a
        private note only the owner ever sees. Empty clears it."""
        from ..social import FriendshipError

        friends = self._require_friendships()
        peer = self._friend_or_404(session, params["peer"])
        try:
            alias = friends.set_alias(
                tenant=session.tenant_id,
                owner=session.principal_id,
                peer=peer,
                alias=str((request.body or {}).get("alias", "")),
            )
        except FriendshipError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        return json_response(200, {"peer": peer, "alias": alias})

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
        # The recipient's gate: a block stops all mail, and a recipient who
        # only accepts friends turns a stranger's message into a nudge to
        # send a friend request first. Friends and open recipients are
        # unaffected — so nothing changes for anyone who leaves it open.
        if self._friendships is not None and not self._friendships.may_message(
            tenant=session.tenant_id, sender=session.principal_id, recipient=peer
        ):
            raise GatewayError(
                403,
                "not_friends",
                "this person only accepts messages from friends — send a"
                " friend request first",
            )
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
        self._representative_auto_reply(session, peer, message.body)
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

    def _representative_auto_reply(self, session, peer: str, inbound: str) -> None:
        """The RECIPIENT's representative may answer a friend message on
        its own — only in auto mode, only past the engine's earned-autonomy
        gate, and never in a way that can break the sender's request (an
        auto-reply is a bonus, not a step of delivery). The sender sees it
        on the next poll like any reply."""
        rep, store = self._representative, self._direct_messages
        if rep is None or store is None:
            return
        scope = f"{session.tenant_id}:{peer}"
        try:
            if rep.mode(scope) != "auto":
                return
            thread = store.between(
                tenant=session.tenant_id, me=peer, peer=session.principal_id
            )
            rep.ingest(
                scope,
                pair_representative_exchanges(
                    [(m.message_id, m.sender, m.body) for m in thread], me=peer
                ),
                peer=session.principal_id,
            )
            history = [
                {
                    "role": "assistant" if m.sender == peer else "user",
                    "content": m.body,
                }
                for m in thread[:-1][-12:]
            ]
            draft = rep.auto_reply(
                scope,
                conversation_id=session.principal_id,
                inbound_text=inbound,
                display_name=peer,
                history=history,
                model=self._seat_actor(
                    self._tenant_model(session.tenant_id),
                    session.principal_id,
                ),
            )
            if draft.status == "auto_sent" and draft.final_text:
                store.send(
                    tenant=session.tenant_id,
                    sender=peer,
                    recipient=session.principal_id,
                    body=draft.final_text,
                )
        except Exception:  # noqa: BLE001 — see docstring: a bonus, not a step
            return

    # ------------------------------------------------------------------ #
    # Reminders: the deterministic route for "remind me".                #
    # ------------------------------------------------------------------ #
    def _require_reminders(self):
        if self._reminders is None:
            raise GatewayError(
                404, "not_found", "reminders are not kept on this host"
            )
        return self._reminders

    def _reminders_list(self, request, session, params) -> Response:
        store = self._require_reminders()
        now = request.now or self._clock()
        return json_response(
            200,
            {
                "due": [
                    r.model_dump(mode="json")
                    for r in store.due(
                        tenant=session.tenant_id,
                        principal=session.principal_id,
                        now=now,
                    )
                ],
                "upcoming": [
                    r.model_dump(mode="json")
                    for r in store.upcoming(
                        tenant=session.tenant_id,
                        principal=session.principal_id,
                        now=now,
                    )
                ],
            },
        )

    def _reminders_create(self, request, session, params) -> Response:
        store = self._require_reminders()
        body = request.body or {}
        now = request.now or self._clock()
        due_at = None
        if body.get("in_minutes") is not None:
            try:
                due_at = now + timedelta(minutes=int(body["in_minutes"]))
            except (TypeError, ValueError):
                raise GatewayError(
                    400, "invalid_request", "in_minutes must be a whole number"
                ) from None
        elif body.get("due_at"):
            try:
                due_at = datetime.fromisoformat(str(body["due_at"]))
            except ValueError:
                raise GatewayError(
                    400, "invalid_request", "due_at must be an ISO timestamp"
                ) from None
        if due_at is None:
            raise GatewayError(
                400, "invalid_request", "say when — due_at or in_minutes"
            )
        try:
            reminder = store.add(
                tenant=session.tenant_id,
                principal=session.principal_id,
                text=str(body.get("text", "")),
                due_at=due_at,
            )
        except ValueError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        return json_response(201, reminder.model_dump(mode="json"))

    def _reminder_delivered(self, request, session, params) -> Response:
        store = self._require_reminders()
        marked = store.mark_delivered(
            params["reminder_id"],
            tenant=session.tenant_id,
            principal=session.principal_id,
        )
        if marked is None:
            raise GatewayError(
                404, "not_found", "no undelivered reminder by that id"
            )
        return json_response(200, marked.model_dump(mode="json"))

    def _reminder_chat_hands(self, session, *, now, tz_offset_minutes: int):
        """The chat's reminder hands, clock- and timezone-bound: the words
        every path speaks are read back from the STORED row — due time in
        the user's local clock — so the confirmation IS the real result."""
        store = self._reminders
        if store is None:
            return None
        offset = timedelta(
            minutes=max(-14 * 60, min(14 * 60, int(tz_offset_minutes or 0)))
        )
        tenant, principal = session.tenant_id, session.principal_id

        def _confirm(reminder) -> str:
            local_due = reminder.due_at + offset
            minutes = round(
                (reminder.due_at - now).total_seconds() / 60
            )
            when = (
                f"in {minutes} minute{'s' if minutes != 1 else ''}"
                if minutes < 90
                else f"in {round(minutes / 60)} hours"
            )
            return (
                f"Reminder set — {local_due:%H:%M} ({when}): "
                f"“{reminder.text}”. I'll bring it up here when it's time."
            )

        class _Hands:
            def reminder_in(self, text: str, minutes: int) -> str:
                try:
                    reminder = store.add(
                        tenant=tenant,
                        principal=principal,
                        text=text,
                        due_at=now + timedelta(minutes=int(minutes)),
                    )
                except (TypeError, ValueError) as exc:
                    return f"error: {exc}"
                return _confirm(reminder)

            def reminder_at(
                self, text: str, hour: int, minute: int, ampm: str | None
            ) -> str:
                if ampm == "pm" and hour < 12:
                    hour += 12
                if ampm == "am" and hour == 12:
                    hour = 0
                if not (0 <= hour <= 23 and 0 <= minute <= 59):
                    return "error: that is not a clock time"
                # The user's clock, not the server's: resolve in local
                # time, next occurrence, then store as UTC.
                local_now = now + offset
                local_due = local_now.replace(
                    hour=hour, minute=minute, second=0, microsecond=0
                )
                if local_due <= local_now:
                    local_due += timedelta(days=1)
                try:
                    reminder = store.add(
                        tenant=tenant,
                        principal=principal,
                        text=text,
                        due_at=local_due - offset,
                    )
                except ValueError as exc:
                    return f"error: {exc}"
                return _confirm(reminder)

            def reminder_list(self) -> str:
                upcoming = store.upcoming(
                    tenant=tenant, principal=principal, now=now
                )
                if not upcoming:
                    return "No reminders ahead."
                return "Your reminders:\n" + "\n".join(
                    f"• {(r.due_at + offset):%Y-%m-%d %H:%M} — {r.text}"
                    for r in upcoming
                )

        return _Hands()

    # ------------------------------------------------------------------ #
    # The representative: replies drafted in the account's own voice.    #
    # Phase 0 of docs/representative-plan.md — retrieval + persona few-  #
    # shot over the shared model, drafts only. Nothing on these routes   #
    # sends a message except the user's explicit send/edit decision.     #
    # ------------------------------------------------------------------ #
    def _require_representative(self):
        if self._representative is None:
            raise GatewayError(
                404, "not_found", "representative mode is not enabled on this host"
            )
        return self._representative

    @staticmethod
    def _shorten(text: str, limit: int) -> str:
        text = " ".join(str(text or "").split())
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

    @staticmethod
    def _representative_scope(session) -> str:
        return f"{session.tenant_id}:{session.principal_id}"

    def _representative_chat_hands(self, session):
        """The chat assistant's representative hands, session-bound.

        OoLu gathers what a reply needs by asking the USER, in their own
        conversation — these are the hands that close that loop: list the
        drafts waiting on information, redraft one with the user's answer
        (the fresh draft lands in the inbox for review — OoLu still never
        sends), or lay a message to rest as read. None when the
        representative is off or absent: the tools answer in words."""
        rep, store = self._representative, self._direct_messages
        if rep is None or store is None:
            return None
        scope = self._representative_scope(session)
        if rep.mode(scope) == "off":
            return None
        app = self

        class _Hands:
            def waiting(self) -> list[dict]:
                return [
                    {
                        "peer": d.conversation_id,
                        "message": d.inbound_text,
                        "questions": d.generated_text,
                    }
                    for d in rep.waiting(scope)
                ]

            def answer(self, peer: str, info: str) -> str:
                peer, info = (peer or "").strip(), (info or "").strip()
                if not peer or not info:
                    return "error: answering takes the friend's name and the information"
                try:
                    draft = app._draft_friend_reply(
                        session, peer, extra_context=info
                    )
                except GatewayError as exc:
                    return f"error: {exc.message}"
                except (ModelBudgetExceeded, ModelUnavailable) as exc:
                    return f"error: {exc}"
                if draft.status == "needs_info":
                    return (
                        "still missing something — the reply also needs: "
                        f"{draft.generated_text}"
                    )
                return (
                    f"drafted the reply to {peer} — it is waiting in the "
                    "drafts block for the user's review (nothing sent)"
                )

            def ignore(self, peer: str) -> str:
                peer = (peer or "").strip()
                if not peer:
                    return "error: say whose message to ignore"
                settled = rep.ignore_conversation(scope, peer)
                store.mark_read(
                    tenant=session.tenant_id,
                    reader=session.principal_id,
                    peer=peer,
                )
                return (
                    f"marked {peer}'s messages as read — no reply will be "
                    f"drafted ({settled} standing draft(s) settled)"
                )

        return _Hands()

    def _representative_status(self, request, session, params) -> Response:
        rep = self._require_representative()
        return json_response(200, rep.status(self._representative_scope(session)))

    def _representative_configure(self, request, session, params) -> Response:
        rep = self._require_representative()
        body = request.body or {}
        mode, about = body.get("mode"), body.get("about")
        if mode is not None and not isinstance(mode, str):
            raise GatewayError(400, "invalid_request", "mode must be a string")
        if about is not None and not isinstance(about, str):
            raise GatewayError(400, "invalid_request", "about must be a string")
        try:
            status = rep.configure(
                self._representative_scope(session), mode=mode, about=about
            )
        except ValueError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        return json_response(200, status)

    def _representative_drafts(self, request, session, params) -> Response:
        rep = self._require_representative()
        scope = self._representative_scope(session)
        return json_response(
            200,
            {
                "items": [draft.model_dump() for draft in rep.pending(scope)],
                # Waiting on the user: generated_text is the QUESTIONS —
                # answered in the OoLu conversation, one at a time.
                "waiting": [
                    draft.model_dump() for draft in rep.waiting(scope)
                ],
            },
        )

    def _draft_friend_reply(self, session, peer: str, *, extra_context=None):
        """The one drafting path: fold the thread into memory (idempotent
        by message id, register-tagged with the peer), then draft a reply
        to the latest unanswered message. Raises GatewayError(409) when
        the last word is already the user's; model errors propagate for
        the caller to map. Returns the Draft. ``extra_context`` is the
        user's own answer to an earlier needs-info question."""
        rep = self._require_representative()
        store = self._require_direct_messages()
        scope = self._representative_scope(session)
        thread = store.between(
            tenant=session.tenant_id, me=session.principal_id, peer=peer
        )
        rep.ingest(
            scope,
            pair_representative_exchanges(
                [(m.message_id, m.sender, m.body) for m in thread],
                me=session.principal_id,
            ),
            peer=peer,
        )
        if not thread or thread[-1].sender != peer:
            raise GatewayError(
                409,
                "nothing_to_answer",
                "the last word in that thread is yours — nothing to reply to",
            )
        history = [
            {
                "role": "assistant" if m.sender == session.principal_id else "user",
                "content": m.body,
            }
            for m in thread[:-1][-12:]
        ]
        return rep.draft(
            scope,
            conversation_id=peer,
            inbound_text=thread[-1].body,
            display_name=session.principal_id,
            history=history,
            model=self._seat_actor(
                    self._tenant_model(session.tenant_id),
                    session.principal_id,
                ),
            extra_context=extra_context,
        )

    def _representative_draft(self, request, session, params) -> Response:
        """Draft a reply to the latest unanswered message from a friend."""
        rep = self._require_representative()
        scope = self._representative_scope(session)
        if rep.mode(scope) == "off":
            raise GatewayError(
                409, "representative_off", "turn representative mode on first"
            )
        peer = self._friend_or_404(session, (request.body or {}).get("peer"))
        try:
            draft = self._draft_friend_reply(session, peer)
        except ModelBudgetExceeded as exc:
            raise GatewayError(402, "model_budget", str(exc)) from exc
        except ModelUnavailable as exc:
            raise GatewayError(503, "model_unavailable", str(exc)) from exc
        return json_response(201, draft.model_dump())

    def _representative_sweep(self, request, session, params) -> Response:
        """The busy person's pass: draft a reply for EVERY friend whose
        message is waiting, so the user only filters — send, edit, or
        discard. Idempotent per message: a message that ever had a draft
        (whatever its fate) is never drafted again, so polling this route
        costs nothing until someone actually says something new."""
        rep = self._require_representative()
        store = self._require_direct_messages()
        scope = self._representative_scope(session)
        if rep.mode(scope) == "off":
            raise GatewayError(
                409, "representative_off", "turn representative mode on first"
            )
        drafted: list[dict] = []
        model_error: str | None = None
        for convo in store.conversations(
            tenant=session.tenant_id, principal=session.principal_id
        ):
            if convo["unread"] <= 0 or convo["last_from"] != convo["peer"]:
                continue
            if rep.has_draft_for(scope, str(convo["peer"]), str(convo["last_text"])):
                continue
            try:
                draft = self._draft_friend_reply(session, str(convo["peer"]))
            except GatewayError:
                continue  # nothing to answer after all — the sweep moves on
            except (ModelBudgetExceeded, ModelUnavailable) as exc:
                # A dead model fails every remaining thread the same way:
                # stop asking, say so once, keep what was drafted.
                model_error = str(exc)
                break
            if draft.status != "needs_info":
                drafted.append(draft.model_dump())
        # A reply the model could not honestly write becomes OoLu's OWN
        # question to the user, in the user's conversation — never words
        # in the peer-facing draft. One ask per sweep: the tasks are dealt
        # with one by one through the conversation window, and nothing
        # forces an answer the moment the toggle flips.
        asked = None
        waiting_draft = rep.next_unnotified(scope)
        if waiting_draft is not None:
            text = REP_NEEDS_INFO_ASK.format(
                peer=waiting_draft.conversation_id,
                inbound=self._shorten(waiting_draft.inbound_text, 140),
                questions=waiting_draft.generated_text,
            )
            if self._assistant_history is not None:
                self._assistant_history.append(
                    tenant=session.tenant_id,
                    principal=session.principal_id,
                    kind="assistant",
                    body=text,
                )
            rep.mark_notified(scope, waiting_draft.draft_id)
            asked = {
                "draft_id": waiting_draft.draft_id,
                "peer": waiting_draft.conversation_id,
                "text": text,
            }
        return json_response(
            200,
            {
                "drafted": drafted,
                "pending": len(rep.pending(scope)),
                "waiting": len(rep.waiting(scope)),
                # OoLu's question for the user, freshly surfaced this
                # sweep (also appended to the conversation history).
                "asked": asked,
                "model_error": model_error,
            },
        )

    def _representative_peer_rule(self, request, session, params) -> Response:
        """Per-peer autonomy: "never auto-reply to my boss." Muting only
        forbids auto-send to that peer — drafting stays available, and the
        earned-autonomy gate still governs everyone else."""
        rep = self._require_representative()
        allowed = (request.body or {}).get("auto")
        if not isinstance(allowed, bool):
            raise GatewayError(400, "invalid_request", "auto must be true or false")
        try:
            status = rep.set_peer_auto(
                self._representative_scope(session), params["peer"], allowed=allowed
            )
        except ValueError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        return json_response(200, status)

    def _representative_decide(self, request, session, params) -> Response:
        """The user's word on a draft: send it, send it edited, or discard.

        Delivery is validated BEFORE the decision is recorded — a draft
        that can't reach its peer stays pending instead of being spent."""
        rep = self._require_representative()
        scope = self._representative_scope(session)
        body = request.body or {}
        action = str(body.get("action") or "")
        text = body.get("text")
        if text is not None and not isinstance(text, str):
            raise GatewayError(400, "invalid_request", "text must be a string")
        try:
            draft = rep.get(scope, params["draft_id"])
        except KeyError:
            raise GatewayError(404, "not_found", "no such draft") from None
        delivers = action in ("send", "edit")
        if delivers:
            store = self._require_direct_messages()
            self._friend_or_404(session, draft.conversation_id)
            outgoing = draft.generated_text if action == "send" else str(text or "")
            if len(outgoing) > MAX_MESSAGE_CHARS:
                raise GatewayError(
                    400, "invalid_request", "that message is too long to send"
                )
        try:
            draft = rep.decide(scope, draft.draft_id, action=action, text=text)
        except KeyError:
            raise GatewayError(404, "not_found", "no such draft") from None
        except ValueError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        if action == "ignore" and self._direct_messages is not None:
            # "No reply, let it rest" — the message is READ: it stops
            # counting as waiting, and the sweep never drafts it again.
            self._direct_messages.mark_read(
                tenant=session.tenant_id,
                reader=session.principal_id,
                peer=draft.conversation_id,
            )
        delivered = None
        if delivers and draft.final_text:
            message = store.send(
                tenant=session.tenant_id,
                sender=session.principal_id,
                recipient=draft.conversation_id,
                body=draft.final_text,
            )
            delivered = {
                "message_id": message.message_id,
                "at": message.sent_at.isoformat(),
            }
        return json_response(200, {**draft.model_dump(), "delivered": delivered})

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
            if not self._autobuild_consented(session.tenant_id, session.principal_id):
                return f"error: auto-build is off — {AUTOBUILD_HINT}"
            # A fleet stays a fleet: whatever a member builds remains
            # under the member's own Supernode; a Supernode builds under
            # itself, exactly as before.
            under = entry
            under_id = node_id
            if not entry.account.is_supernode and entry.account.supernode_id:
                parent = entries.get(entry.account.supernode_id)
                if parent is not None:
                    under, under_id = parent, parent.node_id
            return self._build_function_node(
                session, goal, under_entry=under, under_node_id=under_id
            )

        def reviser(change: str) -> str:
            change = (change or "").strip()
            if not change:
                return "error: tell me how the function should change"
            # The same consent door as building: model-written code enters
            # this node only under the standing auto-build consent, whether
            # the ask came typed or from the model's own initiative.
            if not self._autobuild_consented(session.tenant_id, session.principal_id):
                return f"error: auto-build is off — {AUTOBUILD_HINT}"
            return self._revise_node_function(session, node_id, entry, change)

        def _call_handler(handler, handler_params: dict, payload: dict):
            """(status, body) through a REAL handler — every wall it
            enforces (ownership, tenancy, fixed traits, audit) binds the
            chat hand exactly as it binds the button."""
            call = Request(
                method="POST",
                path="/internal",
                headers={},
                query={},
                body=payload,
                now=request.now,
            )
            try:
                answered = handler(call, session, handler_params)
            except GatewayError as exc:
                return exc.status, {"message": exc.message}
            return answered.status, answered.body or {}

        def member_creator(title: str, authority: int, is_supernode: bool) -> str:
            title = (title or "").strip()
            if not title:
                return "error: give the member a name"
            # Members are minted on the ORG's desk: this Supernode's, or
            # the fleet a member serves under — never a standalone node.
            target_id = node_id
            if not entry.account.is_supernode:
                if not entry.account.supernode_id:
                    return (
                        "error: only an org mints members — this node "
                        "stands alone, use + in the sidebar instead"
                    )
                target_id = entry.account.supernode_id
            try:
                authority = max(1, min(5, int(authority or 1)))
            except (TypeError, ValueError):
                authority = 1
            status, body = _call_handler(
                self._contribute,
                {},
                {
                    # The same empty-draft shape the + form mints: the
                    # function arrives from work or a later build.
                    "skill": {
                        "name": title,
                        "description": title,
                        "signature": {"application": "cli", "adapter": "cli"},
                        "actions": [
                            {
                                "correlation_id": "draft",
                                "adapter": "cli",
                                "operation": "run",
                            }
                        ],
                    },
                    "semver": "1.0.0",
                    "title": title,
                    "summary": title,
                },
            )
            if status >= 400:
                return f"error: {body.get('message', 'the node was refused')}"
            new_id = str(body.get("node_id") or "")
            status, body = _call_handler(
                self._work_account,
                {"node_id": new_id},
                {
                    "accept_policy": True,
                    "is_supernode": bool(is_supernode),
                    "supernode_id": target_id,
                    "audit_mode": False,
                    "allow_autodev_data": True,
                    "authority_level": authority,
                },
            )
            if status >= 400:
                return (
                    f"error: the node was created ({new_id[:8]}) but its "
                    f"org seat was refused: {body.get('message', 'refused')}"
                )
            return (
                f"Created member “{title}” ({new_id[:8]}) under the org at "
                f"L{authority} — it starts UNCLAIMED: share its node id "
                "only with the person who should onboard it."
            )

        def account_control(action: str, value: str) -> str:
            value = (value or "").strip()
            if not value:
                return "error: name the host or user"
            account = self._desk.account_for(node_id) if self._desk else None
            if account is None:
                return "error: this node has no account here"
            if action == "grant_host":
                standing = list(account.network_hosts)
                if value in standing:
                    return f"{value} is already granted"
                patch: dict = {"network_hosts": [*standing, value]}
                did = f"granted {value} — this node's functions may reach it"
            elif action == "block_host":
                standing = list(account.blocked_hosts)
                if value in standing:
                    return f"{value} is already blocked"
                patch = {"blocked_hosts": [*standing, value]}
                did = f"blocked host {value} for this org's whole fleet"
            elif action == "block_user":
                standing = list(account.blocked_users)
                if value in standing:
                    return f"{value} is already blocked"
                patch = {"blocked_users": [*standing, value]}
                did = f"blocked user {value} — their messages will not land"
            else:
                return f"error: unknown access action '{action}'"
            status, body = _call_handler(
                self._work_account, {"node_id": node_id}, patch
            )
            if status >= 400:
                return f"error: {body.get('message', 'refused')}"
            return did

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
            f"automation {reliability}).\n"
            "THIS NODE'S JOB on its route: PROCESS what the previous node "
            "(or a user) delivered — incoming activity lands as held "
            "requests on its desk and as files/messages in ITS drawer "
            "(folder messages/) — and PASS THE RESULTS ONWARD exactly as "
            "the route plans: signing or allowing a held request moves it "
            "to the next node by id; send_message delivers a result to a "
            "sibling node under the same Supernode, or to a friend, by "
            "name.\n"
            "You are the OPERATOR at this desk, not a chatbot: prefer "
            "DOING the work over discussing it. Your file tools here "
            "reach THIS node's own drawer — open what arrived, edit or "
            "produce the result with write_file, then pass it on. Decide "
            "or sign held requests when asked; reply to requesters; and "
            "(with the user's auto-build consent) build the missing "
            "execution nodes that AUTOMATE this step so it stops needing "
            "hands at all. build_node NEVER changes THIS node's code (a "
            "public-safety rule): it always creates a SEPARATE new node "
            "that expands the path, which can be merged in later once "
            "proven. When the user asks to change THIS node's OWN "
            "function, use revise_node: the seated author rewrites "
            "src/main.py under the same consent, the change is audited, "
            "and the node's next run executes the updated code. Extra "
            "tools available ONLY here:\n"
            '  {"tool": "node_holds", "args": {}}\n'
            '  {"tool": "decide_hold", "args": {"pending_id": "<id>", '
            '"approved": true, "signature": "<typed name, optional>"}}\n'
            '  {"tool": "reply_hold", "args": {"pending_id": "<id>", '
            '"message": "<text>"}}\n'
            '  {"tool": "build_node", "args": {"goal": "<what it must do>"}}\n'
            '  {"tool": "revise_node", "args": {"change": "<what must '
            'change in THIS node\'s function>"}}\n'
            '  {"tool": "create_folder", "args": {"path": "<folder path>"}}\n'
            '  {"tool": "create_member", "args": {"title": "<member '
            'name>", "authority": 1, "is_supernode": false}}\n'
            '  {"tool": "grant_host", "args": {"host": "api.example.com"}}\n'
            '  {"tool": "block_host", "args": {"host": "bad.example.com"}}\n'
            '  {"tool": "block_user", "args": {"user": "<principal>"}}\n'
            "write_file also takes an optional \"folder\" to upload into "
            "a folder of this node's drawer. create_member mints a new "
            "node under this org's Supernode (unclaimed until someone "
            "onboards it); grant_host/block_host move this node's egress "
            "consent, and block_user refuses a principal — all through "
            "the same walls and audit as the Access desk's own controls. "
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
            accounts=self._accounts,
            direct_messages=self._direct_messages,
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
            reviser=reviser,
            member_creator=member_creator,
            account_control=account_control,
        )
        return tools, context_note

    @staticmethod
    def _describe_run_failure(
        say: str, run: dict | None, *, autobuild_hint: bool = True
    ) -> str:
        """Fold an execution failure into the assistant's reply: the exact
        failing node, then the auto-build hint the run view already carries
        when consent is off (or the rebuild's own refusal when it ran).
        The main conversation passes ``autobuild_hint=False`` because the
        growth offer that follows is the better door to the same room."""
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
        if autobuild_hint and autobuild.get("hint"):
            say += f" {autobuild['hint']}"
        return say

    def _autobuild_consented(
        self, tenant: str, principal: str | None = None
    ) -> bool:
        """The ACCOUNT's 'Auto-build nodes on my paths' switch (personal-
        first, tenant layer as the shared default), honestly defaulted:
        no settings node means no consent was ever given."""
        if self._settings is None:
            return False
        return bool(
            self._settings.effective(tenant, principal).get(
                AUTOBUILD_CONSENT_KEY, False
            )
        )

    def _autobuild_before_run(self, session, goal: str) -> str | None:
        """The nodes and the route, built TOGETHER before the run — with
        standing consent ('Auto-build nodes on my paths').

        A task whose route has no node yet is doomed: triggering a
        workflow with no function inside it just fails and asks later.
        With the consent switch on, the missing node is built FIRST —
        the model writes its execution function, the node lands on the
        desk (My nodes) — and the run that follows routes through that
        function. Returns the build's words, or None when there was
        nothing to build (a node already answers, the goal is chat, no
        consent, no model) — every refusal falls back to the offer flow."""
        goal = (goal or "").strip()
        if (
            not goal
            or obviously_chat(goal)
            # A message to a friend is delivered, never built for.
            or messaging_intent(goal)
            or self._nodeplace is None
            or self._desk is None
            or not self._autobuild_consented(session.tenant_id, session.principal_id)
            or self._tenant_model(session.tenant_id) is None
            or self._resolve_node_function(session, goal) is not None
            # A near-twin is a QUESTION (reuse or build distinct?), never
            # a silent build — the growth offer handles it in words.
            or self._find_similar_function_node(session, goal) is not None
        ):
            return None
        result = self._build_function_node(session, goal)
        if result.startswith("error:"):
            return None  # the run + offer flow explains, as before
        return result

    def _offer_growth(
        self, say: str, session, goal: str, *, run: dict | None
    ) -> str:
        """The growth trigger, borrowed from n8n's editor: a workflow
        missing the node it needs proposes ADDING that node, instead of
        repeating the same refusal. A chat task that failed for want of a
        working function records a standing offer and asks in the
        conversation; the user's "yes" on the next message is the consent
        (one goal, one build). When nothing can be offered — no model to
        write the function, the goal is conversation, or its node already
        exists — the old Settings hint stays as the fallback."""
        if (
            run is not None
            and run.get("phase") != "failed"
            and run.get("awaiting") != "incident"
        ):
            return say
        goal = (goal or "").strip()
        can_offer = (
            bool(goal)
            and not obviously_chat(goal)
            # A message to a friend is never a node to offer.
            and not messaging_intent(goal)
            and self._nodeplace is not None
            and self._desk is not None
            and self._tenant_model(session.tenant_id) is not None
            and self._resolve_node_function(session, goal) is None
        )
        if can_offer:
            # The twin guard, reuse first: when a node already answers for
            # NEARLY this goal (same work, said differently), the offer is
            # to run THAT node — one node, one history — and only a "no"
            # rolls into the build offer. An exact match never reaches
            # here (_resolve_node_function already gated the offer).
            similar = self._find_similar_function_node(session, goal)
            if similar is not None:
                self._growth_offers.put(
                    session.tenant_id,
                    session.principal_id,
                    kind="reuse",
                    goal=similar["goal"],
                    original_goal=goal,
                )
                return say + GROWTH_REUSE_OFFER.format(
                    title=similar["title"], existing=similar["goal"]
                )
            self._growth_offers.put(
                session.tenant_id,
                session.principal_id,
                kind="build",
                goal=goal,
                original_goal=goal,
            )
            return say + GROWTH_OFFER.format(name=concise_name(goal), goal=goal)
        hint = (
            (run.get("autobuild") or {}).get("hint")
            if run is not None
            else (
                AUTOBUILD_HINT
                if self._settings is not None
                and not self._autobuild_consented(session.tenant_id, session.principal_id)
                else None
            )
        )
        if hint:
            say += f" If you want me to auto-build what's missing: {hint}"
        return say

    def _reuse_node_and_run(
        self, session, goal: str
    ) -> tuple[ChatTurn, dict | None]:
        """The reuse half of the twin guard: the user said yes to running
        the node that already answers for (nearly) this — the run routes
        through that node's OWN function, so the execution lands in its
        one log instead of a twin's."""
        function = self._resolve_node_function(session, goal)
        title = function["title"] if function is not None else concise_name(goal)
        try:
            run = self._start_intent_run(session, goal)
        except GatewayError as exc:
            if exc.code not in ("cannot_execute", "release_revoked"):
                raise
            return (
                ChatTurn(
                    say=f"I couldn't run “{title}” — {exc.message}.",
                    source="tool",
                ),
                None,
            )
        self._metrics["chat_runs"] += 1
        if function is not None:
            # Reuse chosen over duplication — the decision the build
            # policy wants on the log, not just in the moment.
            self._durable.audit.append(
                "node.reuse_decision",
                {
                    "decision": "reuse_directly",
                    "node_id": function["node_id"],
                    "goal": goal,
                    "by": session.principal_id,
                    "tenant": session.tenant_id,
                },
            )
        say = (
            f"Running “{title}” — the node that already answers for this; "
            "the execution lands in its own log."
        )
        say = self._describe_run_failure(say, run, autobuild_hint=False)
        return ChatTurn(say=say, source="tool"), run

    def _grow_node_and_run(
        self, session, goal: str, *, allow_twin: bool = False
    ) -> tuple[ChatTurn, dict | None]:
        """The consented half of the growth trigger: the user said yes, so
        build the node — the SAME gated path as the interact window's build
        (executable-work judgement, the written function, the contribute
        screen) — and immediately re-fire the task, which now routes through
        the node's own function."""
        result = self._build_function_node(session, goal, allow_twin=allow_twin)
        if result.startswith("error:"):
            return (
                ChatTurn(
                    say=f"I couldn't build it: {result[7:].strip()}",
                    source="tool",
                ),
                None,
            )
        actions = [{"tool": "build_node"}]
        try:
            run = self._start_intent_run(session, goal)
        except GatewayError as exc:
            if exc.code not in ("cannot_execute", "release_revoked"):
                raise
            return (
                ChatTurn(
                    say=f"{result} But running it still failed — {exc.message}.",
                    source="tool",
                    actions=actions,
                ),
                None,
            )
        self._metrics["chat_runs"] += 1
        say = result
        if run.get("awaiting") == "confirmation":
            # The standing wall, unchanged: model-written code re-earns the
            # human's confirmation before it runs.
            say += (
                " The run is queued and waiting on you — model-written code "
                "always re-earns your confirmation before it runs, so "
                "approve it on the task card."
            )
        say = self._describe_run_failure(say, run, autobuild_hint=False)
        # The loop actually closes: a completed run through the node's own
        # function IS its verification, and the account says so.
        function = self._resolve_node_function(session, goal)
        account = (
            self._desk.account_for(function["node_id"])
            if function is not None and self._desk is not None
            else None
        )
        if account is not None and account.status.value == "live":
            say += (
                " That run also VERIFIED the node — it is live now, and you "
                "can publish it to the nodeplace whenever you're ready."
            )
        return ChatTurn(say=say, source="tool", actions=actions), run

    def _build_function_node(
        self,
        session,
        goal: str,
        *,
        under_entry=None,
        under_node_id=None,
        allow_twin: bool = False,
        demonstrated: list[str] | None = None,
    ) -> str:
        """Create ONE node born WITH its own execution function — the shared
        core behind the interact window's ``build`` and the chat's growth
        trigger. Consent belongs to the CALLER (the settings switch there,
        the user's explicit yes here); every other gate — the executable-work
        judgement, the actually-written function, the contribute screen — is
        this one path, identical for both doors.

        Returns words: an ``error: …`` prefix means refusal."""
        goal = (goal or "").strip()
        if not goal:
            return "error: tell me what the node should do"
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
        if messaging_intent(goal):
            return (
                "error: that's a message to send, not a node to build — "
                "just tell me what to say and to whom (\"tell <friend> "
                "<the message>\") and I'll deliver it directly"
            )
        if self._nodeplace is None or self._desk is None:
            return "error: nodes are not enabled on this host"
        nodeplace = self._nodeplace
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
                # A deleted node never blocks rebuilding its goal.
                if n.skill_id == skill_id and not self._node_deleted(n.node_id)
            ),
            None,
        )
        if existing is not None:
            return (
                f"That node already exists — “{concise_name(goal)}” "
                f"({existing.node_id[:8]}). No copy was made: running "
                "it again lands every execution in its own log."
            )
        if not allow_twin:
            # The twin guard: near-identical goals ('csvs' vs 'csv files')
            # would mint two nodes with split histories. The refusal names
            # the node that already answers — the caller decides whether
            # to reuse it or say the goal more distinctly. ``allow_twin``
            # is the user's explicit "this is different work" answer.
            similar = self._find_similar_function_node(session, goal)
            if similar is not None:
                return (
                    "error: a node already answers for nearly this — "
                    f"“{similar['title']}” ({similar['node_id'][:8]}), "
                    f"built for “{similar['goal']}”. Running that goal "
                    "lands every execution in its one log; if this is "
                    "truly different work, say the goal more distinctly "
                    "and I'll build it."
                )
        else:
            # The user's explicit "this is different work" IS the reuse
            # decision — recorded with the node that was considered, so
            # a duplicate always carries its justification on the log.
            similar = self._find_similar_function_node(session, goal)
            if similar is not None:
                self._durable.audit.append(
                    "node.reuse_decision",
                    {
                        "decision": "create_new_node_with_justification",
                        "considered": [similar["node_id"]],
                        "considered_title": similar["title"],
                        "goal": goal,
                        "by": session.principal_id,
                        "tenant": session.tenant_id,
                    },
                )
        author = self._seat_actor(
            self._node_function_author(session.tenant_id),
            session.principal_id,
        )
        if author is None:
            return (
                "error: building a node means writing its execution "
                "function, and no model is configured to write it — "
                "add a model key (or a local model) in Settings"
            )
        # Writing the function is the expensive step; meter it so the user
        # sees what building the node actually drew (the resource question).
        meter = getattr(self, "_model_meter", None)
        spent_before = len(meter.charges()) if meter is not None else 0
        script, io, refusal = self._author_function(
            session, author, goal, demonstrated
        )
        if script is None:
            return f"error: {refusal}"
        cost_note = self._build_cost_note(meter, spent_before)
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
        under = under_entry is not None and under_entry.account.is_supernode
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
            self._desk.create_account(
                result.node.node_id,
                principal=session.principal_id,
                tenant=session.tenant_id,
                supernode_id=under_node_id if under else None,
                authority_level=1 if under else None,
                policy_version=NODE_POLICY_VERSION,
            )
        except (ContributionError, OwnershipError, ValueError) as exc:
            return f"error: {exc}"
        new_id = result.node.node_id
        # The function becomes a FILE the human can open: src/main.py in
        # the node's own drawer — written through the node.build SEAT, so
        # the write is scope-checked, attested, and audited like every
        # seated model act. The drawer copy is the function's HOME from
        # here on: runs read it first, so editing the file edits the node.
        if self._files is not None:
            desk_files = DeskFiles(
                self._files,
                tenant=session.tenant_id,
                node_id=new_id,
                seat=SEATS["node.build"],
                # Consent was the door that let this builder run at all —
                # the settings switch, the growth-offer "yes", or the
                # user's explicit "build me a node"; the caller attests.
                consented=True,
            )
            desk_files.write("src/main.py", script)
            self._durable.audit.append(
                "model.seat",
                {
                    "purpose": "node.build",
                    "tenant": session.tenant_id,
                    "by": session.principal_id,
                    "node_id": new_id,
                    "written": desk_files.written,
                },
            )
            # The birth commit: the authored function is the chain's root.
            self._file_node_commit(
                session.tenant_id,
                new_id,
                kind="build",
                instruction=goal,
                by=session.principal_id,
            )
        placing = (
            "under this Supernode — it starts UNCLAIMED: share its node "
            "id only with the person who should onboard it"
            if under
            else "on your desk, with you as its responsible"
        )
        # A function that reaches for the web needs the human's egress
        # consent to actually get there — say so at birth, not at the
        # first refused run.
        web_note = (
            (
                " Its function uses the web hand: grant the exact hosts it "
                "may reach on the node's account (network hosts) — until "
                "you do, every web call fails closed."
            )
            if "http_request" in script
            else ""
        )
        interface = (
            "consumes "
            + (", ".join(f"{c.name}:{c.value_type}" for c in consumes) or "nothing")
            + " → produces "
            + ", ".join(f"{p.name}:{p.value_type}" for p in produces)
        )
        if under_entry is not None:
            return (
                f"Built a NEW node “{name}” ({new_id[:8]}) WITH its own "
                f"execution function ({interface}), {placing}. This node "
                f"“{under_entry.title}” is unchanged — for public safety, build "
                "never edits an existing node's code; it adds a fresh node "
                "that expands the path. It starts needs-verification and "
                "becomes a callable, routable step as its runs verify; once "
                "proven, the two can be merged into one throughout solution."
                + web_note
                + cost_note
            )
        return (
            f"Built a NEW node “{name}” ({new_id[:8]}) WITH its own "
            f"execution function ({interface}), {placing}. It starts "
            "needs-verification and becomes a callable, routable step as "
            "its runs verify." + web_note + cost_note
        )

    def _revise_node_function(self, session, node_id: str, entry, change: str) -> str:
        """Rewrite THIS node's own execution function on the user's ask —
        the interact window's counterpart to build. The drawer's
        ``src/main.py`` is the function's home (runs read it first), so
        the revision lands there through the ``node.build`` seat and is
        audited like every seated model act; the caller already attested
        the auto-build consent. Returns words; ``error: …`` is refusal."""
        if obviously_chat(change):
            return (
                "error: that reads as conversation, not a change to the "
                "function — tell me what the code should do differently"
            )
        if self._files is None:
            return (
                "error: this host stores no node files, so there is no "
                "function to revise"
            )
        author = self._seat_actor(
            self._node_function_author(session.tenant_id),
            session.principal_id,
        )
        if author is None:
            return (
                "error: revising a node means rewriting its execution "
                "function, and no model is configured to write it — add "
                "a model key (or a local model) in Settings"
            )
        # The node's registry state: the latest version and its skill —
        # the fallback source of the current script, and the parent the
        # revised version derives from.
        version = current_skill = None
        if self._nodeplace is not None:
            try:
                version = self._nodeplace.latest_version(node_id)
                if version is not None:
                    current_skill = ReusableSkill.model_validate_json(
                        version.sanitized_skill_json
                    )
            except Exception:  # noqa: BLE001 - a broken record revises from blank
                version = current_skill = None
        current = (
            self._node_drawer_read(session, node_id, "src/main.py")
            or self._skill_script(current_skill)
            or ""
        )
        goal = (
            "Revise this node's execution function.\n"
            f"Node: {entry.title}\n"
            f"Requested change: {change}\n\n"
            "Rewrite the COMPLETE function with the change applied — the "
            "whole script, never a diff or a fragment.\n"
            "Current function (src/main.py):\n"
            f"```python\n{current}\n```"
        )
        meter = getattr(self, "_model_meter", None)
        spent_before = len(meter.charges()) if meter is not None else 0
        script, io, refusal = self._author_function(
            session,
            author,
            goal,
            None,
            read_file=lambda path: self._node_drawer_read(
                session, node_id, path
            ),
        )
        if script is None:
            return f"error: {refusal}"
        cost_note = self._build_cost_note(meter, spent_before)
        # The registry follows the revision: a NEW version on the SAME
        # node, derived from the one it replaces, carrying the revised
        # script and the revised interface — so the contract the goal
        # assembler plans over is the code that actually runs. This
        # happens BEFORE the drawer write: a version the safety screen
        # (or ownership) refuses leaves the node exactly as it was.
        version_note = ""
        if (
            self._nodeplace is not None
            and version is not None
            and current_skill is not None
        ):
            revised_skill = ReusableSkill.model_validate(
                {
                    "id": current_skill.id,
                    "name": current_skill.name,
                    "description": current_skill.description,
                    "signature": {"application": "script", "adapter": "script"},
                    "parameters": [
                        {
                            "name": item["name"],
                            "value_type": item["type"],
                            "required": True,
                        }
                        for item in io.get("inputs", [])
                    ],
                    "actions": [
                        {
                            "correlation_id": "function",
                            "adapter": "script",
                            "operation": "run",
                            "parameters": {
                                "goal": current_skill.description or change,
                                "script": script,
                                "node_key": self._skill_node_key(current_skill),
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
                contributed = self._nodeplace.contribute(
                    noder_principal=session.principal_id,
                    tenant_id=session.tenant_id,
                    skill=revised_skill,
                    semver=self._bump_semver(version.semver),
                    title=entry.title,
                    summary=current_skill.description or entry.title,
                    node_id=node_id,
                    derived_from=version.version_id,
                    consumes=consumes or None,
                    produces=produces or None,
                )
            except (
                ContributionError,
                OwnershipError,
                SafetyViolation,
                ValueError,
            ) as exc:
                return f"error: the revision was refused before it landed: {exc}"
            version_note = (
                f" The registry followed: version {contributed.version.semver} "
                "now carries the revised function and interface."
            )
        desk_files = DeskFiles(
            self._files,
            tenant=session.tenant_id,
            node_id=node_id,
            seat=SEATS["node.build"],
            # The reviser closure held the consent door; the seat records
            # the attestation, the audit line below records the act.
            consented=True,
        )
        desk_files.write("src/main.py", script)
        self._durable.audit.append(
            "model.seat",
            {
                "purpose": "node.build",
                "tenant": session.tenant_id,
                "by": session.principal_id,
                "node_id": node_id,
                "revision": True,
                "written": desk_files.written,
            },
        )
        # A revision is a new commit on the chain — the user's ask rides
        # as its instruction; the replaced code stays in the parent.
        self._file_node_commit(
            session.tenant_id,
            node_id,
            kind="revise",
            instruction=change,
            by=session.principal_id,
        )
        web_note = (
            (
                " The revised function uses the web hand: make sure the "
                "hosts it reaches are granted on the node's account "
                "(network hosts) — ungranted calls fail closed."
            )
            if "http_request" in script
            else ""
        )
        return (
            f"Revised “{entry.title}” — its execution function "
            "(src/main.py) was rewritten with the change applied, through "
            "the node.build seat, and the act is audited. The node's next "
            "run executes the updated code."
            + version_note
            + web_note
            + cost_note
        )

    @staticmethod
    def _skill_script(skill) -> str | None:
        """The script a skill's function action carries, if any."""
        if skill is None:
            return None
        action = next(
            (a for a in skill.actions if a.adapter == "script"), None
        )
        script = (action.parameters or {}).get("script") if action else None
        return str(script) if script else None

    @staticmethod
    def _skill_node_key(skill) -> str:
        """The stable cache identity a revised function keeps: the key the
        current function ran under, so the revision's verified runs land
        in the same node's history — defaulted from the skill id."""
        action = next(
            (a for a in skill.actions if a.adapter == "script"), None
        )
        key = (action.parameters or {}).get("node_key") if action else None
        return str(key) if key else f"node:{skill.id}"

    @staticmethod
    def _bump_semver(semver: str) -> str:
        """The next patch version — a revision is the same node, moved one
        honest step. An unparsable current version restarts at 1.0.1."""
        parts = str(semver or "").split(".")
        try:
            numbers = [int(p) for p in parts[:3]]
        except ValueError:
            return "1.0.1"
        while len(numbers) < 3:
            numbers.append(0)
        numbers[2] += 1
        return ".".join(str(n) for n in numbers)

    def _node_drawer_read(self, session, node_id: str, path: str) -> str | None:
        """A seat-scoped read of one node's drawer for the author's hand —
        refused paths and missing stores answer None, never an exception."""
        if self._files is None:
            return None
        try:
            return DeskFiles(
                self._files,
                tenant=session.tenant_id,
                node_id=node_id,
                seat=SEATS["node.build"],
                consented=True,
            ).read(path)
        except SeatViolation:
            return None

    @staticmethod
    def _build_cost_note(meter, before_count: int) -> str:
        """What writing the node's function drew, in the user's terms — the
        token count (the resource question) and its small compute cost. Empty
        when nothing was metered (no meter, or a stubbed/free build)."""
        if meter is None:
            return ""
        spent = meter.charges()[before_count:]
        tokens = sum(c.prompt_tokens + c.completion_tokens for c in spent)
        if tokens <= 0:
            return ""
        cost = sum(c.cost for c in spent)
        drew = (
            "free — written by your own local model"
            if cost <= 0
            else f"about ${cost:.4f} of model compute"
        )
        return f" Building it drew ≈{tokens:,} tokens ({drew})."

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
        # A deleted node is absent: its function never resolves — and a
        # REBUILT twin of the same goal resolves past the tombstone.
        node = next(
            (
                n
                for n in nodes
                if n.skill_id == skill_id
                and not self._node_deleted(n.node_id)
            ),
            None,
        )
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
        return self._finalize_function(
            {
                "node_id": node.node_id,
                "skill_id": skill_id,
                "title": skill.name,
                "goal": skill.description,
                "script": str(script),
                "node_key": str(
                    (action.parameters or {}).get("node_key")
                    or f"node:{skill_id}"
                ),
                # The node's egress consent and its drawer's src/ programs
                # ride the function into the run — the same regimes the
                # contract path stamps, applied to the node-function route.
                **self._node_function_extras(session, node.node_id),
                **self._declared_ports(node),
            },
            tenant=session.tenant_id,
        )

    def _finalize_function(self, function: dict, *, tenant: str = "") -> dict:
        """Shape a resolved function for execution: promote the drawer's
        ``src/main.py`` to the script, stamp what the sealed-release
        policy says about that exact tree (sealed / draft / REVOKED),
        then — when a tree of OTHER files remains and a bundle store
        exists — FREEZE that tree into a content-addressed bundle and
        ship its id in place of the inline bytes. A large node then
        travels as a 64-char reference, not its whole codebase, and the
        runner stages it from one packed archive.

        No store (a minimal install, or a test): the tree stays inline —
        the same bytes, the same walls, just not deduplicated or cached."""
        function = _drawer_function(function)
        if tenant:
            # After drawer promotion (the executed tree), before bundle
            # freeze (which replaces the files with a reference).
            function = self._stamp_release_state(tenant, function)
        files = function.get("files")
        if files and self._bundle_store is not None:
            bundle_id = self._freeze_tree(files)
            if bundle_id is not None:
                function = {k: v for k, v in function.items() if k != "files"}
                function["bundle"] = bundle_id
        return function

    def _freeze_tree(self, files: dict) -> str | None:
        """Freeze one node's ``src/`` tree (minus main.py) into the bundle
        store, returning its id — or None when it cannot be a bundle
        (oversized/unsafe: it stays inline, still sandbox-guarded)."""
        if not files or self._bundle_store is None:
            return None
        try:
            return self._bundle_store.freeze(files).bundle_id
        except BundleError:
            return None

    def _node_src_bundle_tree(self, tenant: str, node_id: str) -> dict:
        """A node's ``src/`` tree MINUS ``main.py`` (the entry becomes the
        script, never part of the bundle) — the exact tree the run-time
        path freezes, so recomputing it yields the same bundle id."""
        if self._files is None:
            return {}
        tree: dict[str, str] = {}
        for file in self._files.list(tenant=tenant, node_id=node_id):
            if file.blob_ref:
                continue
            folder = file.folder
            if folder == "src":
                name = file.name
            elif folder.startswith("src/"):
                name = f"{folder[4:]}/{file.name}"
            else:
                continue
            if name == "main.py":
                continue
            tree[name] = file.content
        return tree

    def _bundle_live_ids(self) -> set[str]:
        """Every bundle id a live node would freeze to right now — the
        sweep's reachability roots. Recomputed from each node's CURRENT
        drawer tree (freezing is idempotent and self-heals a missing
        blob), so a bundle absent here is genuinely referenced by nothing."""
        live: set[str] = set()
        if self._nodeplace is None or self._bundle_store is None:
            return live
        for node in self._nodeplace.all_nodes():
            tree = self._node_src_bundle_tree(node.tenant_id, node.node_id)
            bundle_id = self._freeze_tree(tree)
            if bundle_id is not None:
                live.add(bundle_id)
        return live

    def _drawer_blob_refs(self) -> set[str]:
        """Every CAS ref the file drawer still holds — the reference source
        that keeps the sweep from deleting a blob a node happens to share
        with someone's uploaded file (content addressing makes them one)."""
        if self._files is None:
            return set()
        return self._files.all_blob_refs()

    def _node_function_extras(self, session, node_id: str) -> dict:
        """What a node's own function carries beyond its script: the egress
        regime the web broker enforces, and the drawer's ``src/`` files the
        backend stages next to the script.

        Egress mirrors the contract stamp exactly: the open web (minus the
        org's blocks) for a fleet under a verified Supernode, else the
        account's granted hosts — empty fails closed at the broker, and a
        host with no desk at all stamps nothing (no grant, no web hand).
        """
        extras: dict = {}
        # The exact-value binder's wall: value:// references among the
        # run's bindings may only resolve inside THIS tenant.
        extras["_value_tenant"] = session.tenant_id
        if self._desk is not None:
            # On the global service, a signed-in account needs no per-host
            # grants: the web is open by default, blocks still bind.
            verdict = (
                self._kyc.open_egress(
                    node_id, default_open=bool(self._config.global_service)
                )
                if self._kyc is not None
                else None
            )
            if verdict is not None:
                extras["_egress_open"] = True
                extras["_egress_blocked"] = list(verdict)
            else:
                account = self._desk.account_for(node_id)
                extras["_egress_hosts"] = list(
                    account.network_hosts if account is not None else ()
                )
        if self._files is not None:
            staged: dict[str, str] = {}
            for file in self._files.list(
                tenant=session.tenant_id, node_id=node_id
            ):
                if file.blob_ref:
                    continue  # programs are text; blobs stay in the drawer
                folder = file.folder
                if folder == "src":
                    staged[file.name] = file.content
                elif folder.startswith("src/"):
                    staged[f"{folder[4:]}/{file.name}"] = file.content
            if staged:
                extras["files"] = staged
        return extras

    def _function_for_node(self, session, node_id: str) -> dict | None:
        """:meth:`_resolve_node_function`'s sibling, keyed by node id — the
        webhook door knows WHICH node it fires, not what goal minted it.
        Walks the caller's own desk, so a node the minter no longer owns
        resolves to nothing and the hook goes quiet with it."""
        if self._nodeplace is None:
            return None
        try:
            nodes = self._nodeplace.list_own_nodes(
                noder_principal=session.principal_id,
                tenant_id=session.tenant_id,
            )
        except Exception:  # noqa: BLE001 - resolution is best-effort
            return None
        node = next((n for n in nodes if n.node_id == node_id), None)
        if node is None or self._node_deleted(node.node_id):
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
        return self._finalize_function(
            {
                "node_id": node.node_id,
                "skill_id": node.skill_id,
                "title": skill.name,
                "goal": skill.description,
                "script": str(script),
                "node_key": str(
                    (action.parameters or {}).get("node_key")
                    or f"node:{node.skill_id}"
                ),
                **self._node_function_extras(session, node.node_id),
                **self._declared_ports(node),
            },
            tenant=session.tenant_id,
        )

    @staticmethod
    def _declared_ports(node) -> dict:
        """The node's declared output ports, as the run-time stamp the
        script hand validates every successful payload against — the
        contract the node PUBLISHED is the contract its runs answer to,
        so a mocked emit that skips the declared ports fails with the
        gap named instead of passing as a success."""
        ports = [
            {"name": s.name, "type": s.value_type}
            for s in (getattr(node, "produces", None) or ())
        ]
        return {"_output_ports": ports} if ports else {}

    # ------------------------------------------------------------------ #
    # Node provenance: immutable commits, sealed releases, revocation.    #
    # ------------------------------------------------------------------ #
    def _node_src_tree(self, tenant: str, node_id: str) -> dict[str, str]:
        """The node's WHOLE current ``src/`` tree, main.py included — in
        the same path form the runs stage it (``main.py``,
        ``sub/helper.py``), so commit and release identities hash the
        exact tree that executes."""
        if self._files is None:
            return {}
        tree: dict[str, str] = {}
        for file in self._files.list(tenant=tenant, node_id=node_id):
            if file.blob_ref:
                continue
            folder = file.folder
            if folder == "src":
                name = file.name
            elif folder.startswith("src/"):
                name = f"{folder[4:]}/{file.name}"
            else:
                continue
            tree[name] = file.content
        return tree

    def _file_node_commit(
        self,
        tenant: str,
        node_id: str,
        *,
        kind: str,
        instruction: str,
        by: str,
    ) -> None:
        """Every write to a node's function files an immutable commit —
        build, revise, repair, hand edit alike — so the drawer's current
        tree is just the HEAD of a chain that preserves every attempt.
        Best-effort bookkeeping: the write it records already landed."""
        if self._provenance is None:
            return
        try:
            tree = self._node_src_tree(tenant, node_id)
            if tree:
                self._provenance.commit(
                    tenant,
                    node_id,
                    tree,
                    kind=kind,
                    instruction=instruction,
                    by=by,
                )
        except Exception:  # noqa: BLE001 — history is a bonus on a landed write
            logging.getLogger("oolu.gateway").warning(
                "node commit filing failed for %s", node_id, exc_info=True
            )

    def _stamp_release_state(self, tenant: str, function: dict) -> dict:
        """What the sealed-release policy says about the function about
        to run. Three honest answers: SEALED (this exact tree is the
        latest verified release), a DRAFT (edited since the seal — it
        runs, and a verified run seals it anew), or REVOKED (this exact
        tree's release was revoked — new runs refuse with the reason
        until the function is revised into a new draft)."""
        if self._provenance is None or not function.get("node_id"):
            return function
        from ..nodeplace.provenance import tree_hash

        try:
            tree = {"main.py": str(function.get("script", ""))}
            for name, content in (function.get("files") or {}).items():
                tree[str(name)] = str(content)
            digest = tree_hash(tree)
            revoked = self._provenance.revoked_tree(
                tenant, str(function["node_id"])
            )
            if revoked is not None and revoked[0] == digest:
                function["_revoked"] = revoked[1] or (
                    "its verified release was revoked"
                )
                return function
            latest = self._provenance.latest_release(
                tenant, str(function["node_id"])
            )
            if latest is not None:
                function["_release"] = {
                    "release_id": latest.release_id,
                    "sealed": latest.tree_hash == digest,
                }
        except Exception:  # noqa: BLE001 — the stamp is advisory; every
            # other wall (screen, sandbox, confirmation) still binds.
            logging.getLogger("oolu.gateway").warning(
                "release stamping failed", exc_info=True
            )
        return function

    @staticmethod
    def _refuse_revoked(function: dict | None) -> None:
        """The production guard: a revoked release never runs again —
        not silently replanned around, refused in words. A REVISED
        function is a new draft (different tree, no stamp) and passes."""
        if function and function.get("_revoked"):
            raise GatewayError(
                422,
                "release_revoked",
                "this node's verified release was revoked — "
                f"{function['_revoked']} — revise its function to earn a "
                "new release before running it",
            )

    def _find_similar_function_node(self, session, goal: str) -> dict | None:
        """The twin guard's lookup: the user's own function node whose goal
        is the SAME work said differently, if one exists.

        Exact goals are :meth:`_resolve_node_function`'s job — this finds
        what an exact key can never see ('csvs' vs 'csv files'), by
        ``goal_similarity`` against each function node's stored goal
        sentence. A human-sized scan over one person's desk, never the
        marketplace; best match at or above ``NEAR_GOAL_SIMILARITY`` wins."""
        if self._nodeplace is None:
            return None
        exact_id = self._function_skill_id(session.tenant_id, goal)
        try:
            nodes = self._nodeplace.list_own_nodes(
                noder_principal=session.principal_id,
                tenant_id=session.tenant_id,
            )
        except Exception:  # noqa: BLE001 - the guard is best-effort
            return None
        best: tuple[float, dict] | None = None
        for node in nodes:
            if not node.skill_id.startswith("fn-") or node.skill_id == exact_id:
                continue
            if self._node_deleted(node.node_id):
                continue  # a deleted twin is no twin
            version = self._nodeplace.latest_version(node.node_id)
            if version is None:
                continue
            try:
                skill = ReusableSkill.model_validate_json(
                    version.sanitized_skill_json
                )
            except Exception:  # noqa: BLE001
                continue
            score = goal_similarity(goal, skill.description)
            if score >= NEAR_GOAL_SIMILARITY and (
                best is None or score > best[0]
            ):
                best = (
                    score,
                    {
                        "node_id": node.node_id,
                        "title": skill.name,
                        "goal": skill.description,
                    },
                )
        return best[1] if best is not None else None

    def _revivable_run_for(self, session, intent: str):
        """``(state, mode)`` for the caller's own dead-or-stuck run of
        this exact goal, newest first — the thread a re-ask revives
        instead of piling a sibling next to it. Two shapes revive: a
        terminal FAILED run (mode "restart" — re-driven in place) and a
        run paused on an INCIDENT (mode "retry" — the operator door,
        answered by the re-ask itself)."""
        wanted = (intent or "").strip()
        if not wanted:
            return None
        for state in self._durable.runs.list(limit=10_000):
            if state.contract.metadata.get("tenant_id") != session.tenant_id:
                continue
            if state.contract.submitted_by != session.principal_id:
                continue
            if (state.contract.intent or "").strip() != wanted:
                continue
            if state.phase is Phase.FAILED:
                return state, "restart"
            if (
                state.pause is not None
                and state.pause.kind is PauseKind.INCIDENT
            ):
                return state, "retry"
        return None

    def _start_intent_run(self, session, intent: str, *, max_recovery: int = 1) -> dict:
        """Submit a plain intent as a run: the non-marketplace core of
        ``_submit_run``, shared with the chat surface.

        Asking a goal again after it FAILED revives the same run — the
        same run_id, the same Noder thread — instead of minting a fresh
        one: the retry lands where the failure lives, the thread rises
        (its moment moves), and the list stops filling with dead
        siblings of one goal."""
        metadata: dict = {"tenant_id": session.tenant_id}
        # A goal the user already built a node for runs THAT node's own
        # function — the route is the stored code, not a fresh plan.
        function = self._resolve_node_function(session, intent)
        self._refuse_revoked(function)
        if function is not None:
            metadata["node_function"] = function
        revivable = self._revivable_run_for(session, intent)
        if revivable is not None:
            previous, mode = revivable
            # The revived attempt resolves the node FRESH: a node built
            # (or revised) since the failure now carries the route.
            previous.contract = previous.contract.model_copy(
                update={"metadata": metadata}
            )
            self._durable.runs.save(previous)
            try:
                if mode == "restart":
                    state = self._durable.restart(previous.run_id)
                else:
                    state = self._durable.resume(
                        previous.run_id,
                        ResumeInput(
                            kind=PauseKind.INCIDENT,
                            incident_decision="retry",
                            principal=session.principal_id,
                        ),
                    )
            except OrchestratorError as exc:
                raise GatewayError(422, "cannot_execute", str(exc)) from exc
            self._metrics["runs_submitted"] += 1
            self._record_function_verification(state)
            return self._run_dict(state)
        tenant_runs = sum(
            1
            for s in self._durable.runs.list()
            if s.contract.metadata.get("tenant_id") == session.tenant_id
        )
        if tenant_runs >= self._config.max_runs_per_tenant:
            raise GatewayError(429, "quota_exceeded", "tenant run quota exceeded")
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
        self._record_function_verification(state)
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
            if entry is None:
                # The plain path is the chat surface's path exactly —
                # including reviving this goal's FAILED run in place
                # instead of piling a sibling thread beside it.
                return self._start_intent_run(
                    session, intent, max_recovery=max_recovery
                )
            metadata: dict = {"tenant_id": session.tenant_id}
            if node_version_id is None:
                function = self._resolve_node_function(session, intent)
                self._refuse_revoked(function)
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
            self._record_function_verification(state)
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
        # A run belongs to the ACCOUNT that submitted it, not the whole
        # tenant: two people on one host must never see each other's Noder
        # activity. (The run quota below stays tenant-wide — that's a
        # capacity limit on the host, not a visibility rule.)
        runs = [
            s
            for s in self._durable.runs.list(limit=10_000)
            if s.contract.metadata.get("tenant_id") == session.tenant_id
            and s.contract.submitted_by == session.principal_id
        ]
        start = (page - 1) * size
        window = runs[start : start + size]
        # The Noder list's margins ride each summary: pinned, muted, and
        # whether the thread is hidden AS IT STANDS — activity after the
        # hide stamp brings it back by itself.
        run_prefs: dict[str, dict] = {}
        if self._friendships is not None:
            run_prefs = self._friendships.prefs(
                tenant=session.tenant_id,
                owner=session.principal_id,
                kind="run",
            )
        items = []
        for s in window:
            entry = self._run_dict(s)
            pref = run_prefs.get(s.run_id, {})
            entry["pinned"] = bool(pref.get("pinned"))
            entry["muted"] = bool(pref.get("muted"))
            entry["hidden"] = _hidden_now(
                pref.get("hidden_at"), entry.get("updated_at") or ""
            )
            items.append(entry)
        return json_response(
            200,
            {
                "items": items,
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
            export["settings"] = self._settings.effective(tenant, principal)
        if self._assistant_history is not None:
            export["chat"] = self._assistant_history.history(
                tenant=tenant, principal=principal, limit=10_000
            )
        if self._reminders is not None:
            export["reminders"] = [
                r.model_dump(mode="json")
                for r in self._reminders.upcoming(
                    tenant=tenant, principal=principal, limit=10_000
                )
            ]
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
        if self._friendships is not None:
            erased["friendships"] = self._friendships.erase_principal(
                tenant=tenant, principal=principal
            )
        if self._assistant_history is not None:
            erased["chat_turns"] = self._assistant_history.erase(
                tenant=tenant, principal=principal
            )
        if self._reminders is not None:
            erased["reminders"] = self._reminders.erase(
                tenant=tenant, principal=principal
            )
        if self._lessons is not None:
            erased["lessons"] = self._lessons.erase(
                tenant=tenant, owner=principal
            )
        if self._settings is not None:
            # The personal settings layer goes with the account; the
            # tenant layer stays — it belongs to the tenant.
            erased["personal_settings"] = self._settings.erase_personal(
                tenant, principal
            )
        if self._representative is not None:
            # The voice goes with the account: settings, remembered
            # exchanges, and every draft — one per-user artifact chain.
            erased["representative"] = self._representative.erase(
                self._representative_scope(session)
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
            200,
            {"items": node.describe(session.tenant_id, session.principal_id)},
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
            node.set_many(session.tenant_id, changes, session.principal_id)
        except SettingError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        return json_response(
            200,
            {"items": node.describe(session.tenant_id, session.principal_id)},
        )

    # ------------------------------------------------------------------ #
    # Model keys: the BYO-key door and the per-tenant brain behind chat.  #
    # ------------------------------------------------------------------ #
    def _require_model_keys(self) -> ModelKeyring:
        if self._model_keys is None:
            raise GatewayError(404, "not_found", "model keys are not enabled")
        return self._model_keys

    def _tenant_model(
        self, tenant: str, *, purpose: str = CHAT_PURPOSE
    ) -> ChatModelRouter | None:
        """The tenant's chat brain, or None to stay model-less.

        Routers are cached per (tenant, purpose) — adapters keep capability
        caches, and the purpose is what the meter and usage books aggregate
        by — and dropped whenever the tenant's keys change. Settings are
        read through closures at call time, so a settings change needs no
        invalidation.
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
        def _tier_now() -> str:
            # The author's seat thinks harder than the conversation by
            # default: model.build_tier (default "reasoning") governs
            # node.build consultations; "inherit" follows model.tier.
            if purpose == "node.build":
                chosen = str(_effective("model.build_tier", "reasoning"))
                if chosen in ("fast", "reasoning"):
                    return chosen
            return str(_effective("model.tier", "fast"))

        router = self._model_routers.get((tenant, purpose))
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
                tier=_tier_now,
                source=lambda: str(_effective("model.source", "subscription")),
                local_url=lambda: str(_effective("model.local_url", "")),
                local_model=lambda: str(_effective("model.local_model", "")),
                web_search=lambda: bool(_effective("model.web_search", True)),
                purpose=purpose,
            )
            self._model_routers[(tenant, purpose)] = router
        return router

    @staticmethod
    def _seat_actor(router, principal: str):
        """Name WHO is drawing on the shared brain before it is used.
        Routers are cached per (tenant, purpose) while many users share
        one tenant on the global service — this stamp is what keeps
        every user's API draw on their OWN gauge in the usage books.
        A test stub without the channel simply stays tenant-level."""
        if router is not None and hasattr(router, "act_as"):
            router.act_as(principal or "")
        return router

    def _drop_model_routers(self, tenant: str) -> None:
        """Every purpose's router for this tenant — a changed key must
        reach the author's seat as surely as the conversation's."""
        for key in [k for k in self._model_routers if k[0] == tenant]:
            self._model_routers.pop(key, None)

    def _node_function_author(self, tenant: str):
        """The model that writes a new node's execution function — the
        tenant's own brain seated APART: routed under the ``node.build``
        purpose, so the authoring spend and audit stand separate from the
        conversation's. A seam so tests (or a future dedicated authoring
        model) can supply their own."""
        return self._tenant_model(tenant, purpose="node.build")

    def _author_function(self, session, author, goal, demonstrated, *, read_file=None):
        """``(script, io, refusal)`` through the strongest path the seated
        model supports: a tool-calling brain works as the
        :class:`NodeAuthorAgent` — the desk's contracts and upstream
        outputs in hand, plus a drawer read for revisions — while a plain
        ``reply`` model keeps the one-shot ``author_node_function`` gates
        unchanged."""
        if not hasattr(author, "consult"):
            return author_node_function(author, goal, demonstrated=demonstrated)
        agent = NodeAuthorAgent(
            author,
            catalog=lambda: self._author_catalog(session),
            outputs=lambda node_id: self._author_node_outputs(session, node_id),
            read_file=read_file,
            verify=self._author_verifier(),
        )
        authored = agent.author(goal, demonstrated=demonstrated)
        return authored.script, authored.io, authored.refusal

    def _author_verifier(self):
        """The author's finish gate made real: a sandbox dry-run of the
        candidate script through the SAME script hand contract runs use —
        safety screen, dependency healing, contract classification — with
        NO web grant and NO staged files, so nothing leaves the box (a
        refused ``http_request`` answers status 0, exactly what the
        script contract teaches the function to read and report). No
        script runtime on this host → None: the agent authors without
        the verify hand, exactly as before."""
        runner = self._contract_executors.get("script")
        if runner is None:
            return None

        def verify(script: str) -> dict:
            import hashlib

            digest = hashlib.sha256(script.encode()).hexdigest()[:16]
            action = ActionEvent(
                correlation_id="author-verify",
                adapter="script",
                operation="run",
                parameters={
                    "goal": (
                        "verify the authored function executes and speaks "
                        "the contract"
                    ),
                    "script": script,
                    "node_key": f"author-verify:{digest}",
                },
            )
            try:
                outcome = runner.execute(
                    action, idempotency_key=f"author-verify:{digest}"
                )
            except Exception as exc:  # noqa: BLE001 - answered, never fatal
                return {
                    "ok": False,
                    "error": f"the sandbox could not run the script: {exc}",
                }
            if outcome.status is ExecutionStatus.SUCCEEDED:
                report: dict = {"ok": True}
                result = outcome.evidence.get("result")
                if result is not None:
                    report["result"] = result
                return report
            return {
                "ok": False,
                "error": outcome.error or "the script failed in the sandbox",
            }

        return verify

    def _author_catalog(self, session) -> list[dict]:
        """The desk's nodes with their contracts — the slot vocabulary in
        circulation, for the author to REUSE instead of minting synonyms."""
        if self._nodeplace is None:
            return []
        try:
            nodes = self._nodeplace.list_own_nodes(
                noder_principal=session.principal_id,
                tenant_id=session.tenant_id,
            )
        except Exception:  # noqa: BLE001 - the library is advisory context
            return []
        return [
            {
                "node_id": node.node_id,
                "title": node.title,
                "goal": node.summary,
                "consumes": [
                    {"name": s.name, "type": s.value_type} for s in node.consumes
                ],
                "produces": [
                    {"name": s.name, "type": s.value_type} for s in node.produces
                ],
            }
            for node in nodes[:40]
        ]

    def _author_node_outputs(self, session, node_id: str) -> list[dict]:
        """A node's recent run results — the shape its work ACTUALLY
        arrives in downstream, straight from the run store's books."""
        states = [
            s
            for s in self._durable.runs.list(limit=10_000)
            if s.contract.metadata.get("tenant_id") == session.tenant_id
            and (s.contract.metadata.get("node_function") or {}).get("node_id")
            == node_id
            and s.result
        ]
        return [
            {
                "run_id": state.run_id,
                "status": state.result.get("status"),
                "outputs": state.result.get("outputs", []),
            }
            for state in states[-3:]
        ]

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
        view: dict = {
            "items": self._model_usage.view(tenant),
            # The caller's OWN gauge: what THIS account drew, independent
            # of everyone else sharing the tenant's platform key.
            "mine": self._model_usage.user_all_time(
                tenant, session.principal_id
            ),
        }
        if self._subscription is not None and self._subscription.configured():
            brain = self._subscription
            allowance = brain.allowance_for(tenant)
            # A paid plan's allowance renews monthly; the free trial is a
            # lifetime total — the spend basis follows.
            spent = getattr(brain, "spend_for", brain.month_spend)(tenant)
            view["subscription"] = {
                "allowance_usd": allowance,
                "spent_usd": spent,
                "remaining_usd": max(0.0, allowance - spent),
                "trial": bool(
                    getattr(brain, "is_trial", lambda _t: False)(tenant)
                ),
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
        self._drop_model_routers(session.tenant_id)
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
        router = self._seat_actor(
            self._tenant_model(session.tenant_id), session.principal_id
        )
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
        self._drop_model_routers(session.tenant_id)
        return json_response(200, {"removed": provider})

# ------------------------------------------------------------------ #
    # Two-factor authentication: the second lock on spending money.      #
    # ------------------------------------------------------------------ #
    def _require_totp(self):
        if self._totp is None:
            raise GatewayError(
                404, "not_found", "two-factor authentication is not enabled here"
            )
        return self._totp

    def _totp_status(self, request, session, params) -> Response:
        totp = self._require_totp()
        return json_response(
            200, {"enrolled": totp.is_enrolled(session.principal_id)}
        )

    def _totp_enroll(self, request, session, params) -> Response:
        """Begin enrollment: hand back the secret + otpauth URI for a QR.
        Provisional until a code confirms the authenticator works."""
        totp = self._require_totp()
        enrolled = totp.begin_enroll(session.principal_id)
        return json_response(
            200, {"secret": enrolled["secret"], "uri": enrolled["uri"]}
        )

    def _totp_confirm(self, request, session, params) -> Response:
        totp = self._require_totp()
        code = str((request.body or {}).get("code") or "")
        ok = totp.confirm_enroll(
            session.principal_id, code, now=(request.now or self._clock()).timestamp()
        )
        if not ok:
            raise GatewayError(
                400, "invalid_code", "that code didn't match — enter the current one"
            )
        return json_response(200, {"enrolled": True})

    def _totp_disable(self, request, session, params) -> Response:
        totp = self._require_totp()
        totp.disable(session.principal_id)
        return json_response(200, {"enrolled": False})

    # ------------------------------------------------------------------ #
    # Order/booking payment consent: the release valve for spending.     #
    # OoLu may place an order only through this gate — the exact amount, #
    # re-confirmed by the user, plus a fresh authenticator code.         #
    # ------------------------------------------------------------------ #
    def _require_payment_auth(self):
        if self._payment_authorizations is None:
            raise GatewayError(
                404, "not_found", "payment authorization is not enabled here"
            )
        return self._payment_authorizations

    def _payment_auths_list(self, request, session, params) -> Response:
        store = self._require_payment_auth()
        scope = self._representative_scope(session)
        return json_response(
            200, {"items": [a.model_dump() for a in store.pending(scope)]}
        )

    def _payment_auth_request(self, request, session, params) -> Response:
        """Record an intended order awaiting the user's consent — what OoLu
        (or a node it built) files when it wants to place an order or make
        a booking. The order does not execute until the user authorizes."""
        from ..billing import OrderRequest, PaymentAuthorizationError

        store = self._require_payment_auth()
        body = request.body or {}
        try:
            order = OrderRequest(
                merchant=str(body.get("merchant") or ""),
                amount_micros=int(body.get("amount_micros")),
                currency=str(body.get("currency") or "USD"),
                description=str(body.get("description") or ""),
            )
        except (TypeError, ValueError):
            raise GatewayError(
                400, "invalid_request", "an order needs a merchant and an amount"
            ) from None
        try:
            record = store.request(
                self._representative_scope(session),
                order,
                run_id=body.get("run_id"),
            )
        except PaymentAuthorizationError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        return json_response(201, record.model_dump())

    def _payment_auth_decide(self, request, session, params) -> Response:
        """Authorize or cancel a pending order. Authorize demands both the
        exact amount (re-confirmed) and a valid TOTP code; either lock
        failing leaves the order pending, unspent."""
        from ..billing import PaymentAuthorizationError

        store = self._require_payment_auth()
        scope = self._representative_scope(session)
        body = request.body or {}
        action = str(body.get("action") or "authorize")
        if action == "cancel":
            record = store.cancel(scope, params["auth_id"])
            if record is None:
                raise GatewayError(404, "not_found", "no such order")
            return json_response(200, record.model_dump())
        try:
            amount = int(body.get("confirm_amount_micros"))
        except (TypeError, ValueError):
            raise GatewayError(
                400, "invalid_request", "confirm the exact order amount"
            ) from None
        try:
            record = store.authorize(
                scope,
                params["auth_id"],
                confirm_amount_micros=amount,
                code=str(body.get("code") or ""),
            )
        except PaymentAuthorizationError as exc:
            raise GatewayError(400, "authorization_refused", str(exc)) from exc
        return json_response(200, record.model_dump())

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
            # Blob-backed: the bytes live behind /content, not in the row.
            "has_blob": bool(file.blob_ref),
            "created_at": file.created_at.isoformat(),
            "updated_at": file.updated_at.isoformat(),
        }

    # ------------------------------------------------------------------ #
    # The Global Project Graph: models propose, the kernel commits.       #
    # ------------------------------------------------------------------ #
    def _graph_propose(self, request, session, params) -> Response:
        """Submit a structured proposal against the project's truth.

        The first principal to touch a project id becomes its OWNER —
        the same claim pattern as node onboarding. The submitting
        principal is stamped from the SESSION, never taken from the
        body: a proposal cannot speak in someone else's name. A
        rejection is an honest verdict with reasons (409), never a
        server error."""
        project = self._project_graph.ensure_project(
            params["project_id"],
            tenant=session.tenant_id,
            owner=session.principal_id,
        )
        if project is None:
            raise GatewayError(404, "not_found", "no such project")
        body = request.body or {}
        try:
            proposal = GraphProposal.model_validate(
                {
                    "reason": body.get("reason", ""),
                    "patch": body.get("patch") or [],
                    "expected_effects": body.get("expected_effects") or {},
                    "confidence": body.get("confidence"),
                    "node_id": body.get("node_id"),
                    "project_id": params["project_id"],
                    "owner": session.principal_id,
                }
            )
        except ValidationError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        result = self._graph_kernel.process(proposal, tenant=session.tenant_id)
        return json_response(
            200 if result.status == "committed" else 409,
            result.model_dump(mode="json"),
        )

    def _graph_read_filter(self, project: dict, project_id: str, session):
        """The reader's territory: the owner sees all; everyone else
        sees exactly what was granted (read ∪ write, forbidden wins) —
        and a principal with no grant sees NOTHING. None = no access."""
        if session.principal_id == project["owner"]:
            return lambda path: True
        scopes = self._project_graph.scopes_for(
            project_id, session.principal_id
        )
        if scopes is None:
            return None
        readable = scopes.read_paths + scopes.write_paths
        return lambda path: not path_covered(
            path, scopes.forbidden_paths
        ) and path_covered(path, readable)

    def _graph_project_or_404(self, session, params) -> dict:
        project = self._project_graph.project(
            params["project_id"], tenant=session.tenant_id
        )
        if project is None:
            raise GatewayError(404, "not_found", "no such project")
        return project

    def _graph_objects(self, request, session, params) -> Response:
        project = self._graph_project_or_404(session, params)
        visible = self._graph_read_filter(
            project, params["project_id"], session
        )
        if visible is None:
            raise GatewayError(
                403, "forbidden", "no territory granted in this project"
            )
        items = [
            obj.model_dump(mode="json")
            for obj in self._project_graph.list(
                params["project_id"], path=request.query.get("path", "")
            )
            if visible(obj.path)
        ]
        return json_response(200, {"items": items})

    def _graph_object(self, request, session, params) -> Response:
        project = self._graph_project_or_404(session, params)
        visible = self._graph_read_filter(
            project, params["project_id"], session
        )
        current = self._project_graph.get(
            params["project_id"], params["object_id"]
        )
        if current is None or visible is None or not visible(current.path):
            # Invisible and nonexistent answer alike: a 404 that never
            # confirms what the asker may not see.
            raise GatewayError(404, "not_found", "no such object")
        wanted = request.query.get("revision")
        if wanted is not None:
            past = self._project_graph.at_revision(
                params["project_id"], params["object_id"], int(wanted)
            )
            if past is None:
                raise GatewayError(404, "not_found", "no such revision")
            return json_response(200, past.model_dump(mode="json"))
        return json_response(200, current.model_dump(mode="json"))

    def _graph_ledger(self, request, session, params) -> Response:
        """The proposal ledger — every verdict, either way. The owner's
        view for now; scoped readers get their slice when critics land."""
        project = self._graph_project_or_404(session, params)
        if session.principal_id != project["owner"]:
            raise GatewayError(
                403, "forbidden", "only the project's owner reads the ledger"
            )
        entries = self._project_graph.proposals(params["project_id"])
        return json_response(
            200,
            {
                "items": [
                    {
                        "proposal": e["proposal"].model_dump(mode="json"),
                        "result": e["result"].model_dump(mode="json"),
                    }
                    for e in entries
                ]
            },
        )

    def _graph_find(self, request, session, params) -> Response:
        """A critic files a finding — evidence-backed, never a rewrite.

        The finding lands as a graph object under ``issues/{target
        path}`` THROUGH the kernel, so the critic needs write scope on
        the issues subtree only — the design itself stays closed to
        them. Every required field is enforced at the door: a finding
        without evidence is an opinion, and an opinion is a 400."""
        project = self._graph_project_or_404(session, params)
        visible = self._graph_read_filter(
            project, params["project_id"], session
        )
        body = request.body or {}
        target = self._project_graph.get(
            params["project_id"], str(body.get("target") or "")
        )
        if target is None or visible is None or not visible(target.path):
            raise GatewayError(404, "not_found", "no such object")
        severity = str(body.get("severity") or "")
        if severity not in FINDING_SEVERITIES:
            raise GatewayError(
                400,
                "invalid_request",
                f"severity must be one of {', '.join(FINDING_SEVERITIES)}",
            )
        words = str(body.get("finding") or "").strip()
        action = str(body.get("recommended_action") or "").strip()
        evidence = body.get("evidence")
        if not words or not action:
            raise GatewayError(
                400,
                "invalid_request",
                "a finding names what is wrong AND what to do next",
            )
        if not isinstance(evidence, dict) or not evidence:
            raise GatewayError(
                400,
                "invalid_request",
                "a finding without evidence is an opinion — attach the "
                "measurements",
            )
        finding = build_finding(
            target=target,
            critic=session.principal_id,
            severity=severity,
            finding=words,
            evidence=evidence,
            recommended_action=action,
            affected_requirement=body.get("affected_requirement"),
        )
        result = self._graph_kernel.process(
            GraphProposal(
                project_id=params["project_id"],
                owner=session.principal_id,
                reason=f"finding against '{target.object_id}': {words}",
                patch=[PatchOp(op="create", object=finding)],
            ),
            tenant=session.tenant_id,
        )
        payload = result.model_dump(mode="json")
        payload["finding_id"] = finding.object_id
        return json_response(
            200 if result.status == "committed" else 409, payload
        )

    def _graph_findings(self, request, session, params) -> Response:
        """The findings ledger — open issues first, readable territory
        only, optionally narrowed to one target object."""
        project = self._graph_project_or_404(session, params)
        visible = self._graph_read_filter(
            project, params["project_id"], session
        )
        if visible is None:
            raise GatewayError(
                403, "forbidden", "no territory granted in this project"
            )
        wanted = request.query.get("target")
        findings = [
            obj
            for obj in self._project_graph.list(
                params["project_id"], path="issues"
            )
            if obj.type == "finding"
            and visible(obj.path)
            and (wanted is None or obj.parameters.get("target") == wanted)
        ]
        findings.sort(
            key=lambda o: (o.parameters.get("state") != "open", o.path)
        )
        return json_response(
            200, {"items": [o.model_dump(mode="json") for o in findings]}
        )

    def _graph_grant(self, request, session, params) -> Response:
        """Territory is granted by the OWNER, in writing — the same
        consent shape as the egress grants: explicit paths, forbidden
        wins, and nothing at all until the grant exists."""
        project = self._graph_project_or_404(session, params)
        if session.principal_id != project["owner"]:
            raise GatewayError(
                403, "forbidden", "only the project's owner grants territory"
            )
        body = request.body or {}
        try:
            scopes = GraphScopes.model_validate(
                {
                    "principal": body.get("principal", ""),
                    "read_paths": body.get("read_paths") or [],
                    "write_paths": body.get("write_paths") or [],
                    "forbidden_paths": body.get("forbidden_paths") or [],
                }
            )
        except ValidationError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        if not scopes.principal.strip():
            raise GatewayError(
                400, "invalid_request", "whose territory? name a principal"
            )
        self._project_graph.grant_scopes(params["project_id"], scopes)
        return json_response(200, scopes.model_dump(mode="json"))

    def _files_list(self, request, session, params) -> Response:
        store = self._require_files()
        node_id = request.query.get("node_id") or None
        return json_response(
            200,
            {
                "items": [
                    self._file_meta(f)
                    # The Life drawer is PERSONAL: only the caller's own
                    # files (and legacy unowned rows) list on a shared
                    # tenant. Node drawers stay the node's own.
                    for f in store.list(
                        tenant=session.tenant_id,
                        node_id=node_id,
                        owner=session.principal_id,
                    )
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
            # The memories gate: a Life-drawer file belongs to whoever
            # saved it, even on a shared tenant.
            owner=session.principal_id,
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

    def _files_upload(self, request, session, params) -> Response:
        """Raw bytes into the drawer's blob store — the door past the
        inline row cap. Name/folder/node ride the query string; the body
        IS the file, exactly as picked, no base64 inflation."""
        store = self._require_files()
        if not store.blobs_enabled:
            raise GatewayError(
                404, "not_found", "this host keeps no blob store"
            )
        name = str(request.query.get("name", "")).strip()
        if not name:
            raise GatewayError(400, "invalid_request", "name is required")
        try:
            folder = normalize_folder(request.query.get("folder"))
        except ValueError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        data = request.raw
        if not data:
            raise GatewayError(400, "invalid_request", "the body is the file — it is empty")
        node_id = str(request.query.get("node_id") or "") or None
        media_type = str(
            request.header("content-type") or _media_type_for(name)
        ).split(";")[0].strip()
        file = UserFile(
            tenant_id=session.tenant_id,
            node_id=node_id,
            owner=session.principal_id,
            name=name,
            folder=folder,
            media_type=media_type or _media_type_for(name),
        )
        try:
            saved = store.save_bytes(file, data)
        except FileTooLargeError as exc:
            raise GatewayError(413, "too_large", str(exc)) from exc
        return json_response(201, self._file_meta(saved))

    def _files_content(self, request, session, params) -> Response:
        """The file's true bytes, whichever shape it is stored in —
        typed honestly, named for the device's save dialog."""
        file = self._load_file(params, session)
        store = self._require_files()
        try:
            data = store.read_bytes(file)
        except FileTooLargeError as exc:
            raise GatewayError(404, "not_found", str(exc)) from exc
        return Response(
            status=200,
            body=data,
            content_type=file.media_type or "application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{file.name}"'
            },
        )

    def _load_file(self, params, session) -> UserFile:
        store = self._require_files()
        file = store.get(params["file_id"], tenant=session.tenant_id)
        # Another account's Life-drawer file is indistinguishable from a
        # missing one — the memories gate, by id exactly as by listing.
        # Legacy unowned rows ("") stay reachable; node files stay the
        # node's, governed by the node's own doors.
        if file is not None and file.node_id is None:
            if file.owner not in ("", session.principal_id):
                file = None
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
        if file.blob_ref and "content" in body and body["content"] is not None:
            # A binary's bytes are not a text field: editing them through
            # a JSON string could only corrupt the file. Re-upload instead.
            raise GatewayError(
                400,
                "invalid_request",
                "this is a binary file — its bytes are written by upload, "
                "not edited as text (rename and move are fine)",
            )
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
        # A hand edit to a node's src/ program is a commit like any
        # seated write — the chain preserves what the edit replaced.
        touched_src = any(
            f == "src" or f.startswith("src/")
            for f in (file.folder, updated.folder)
        )
        if updated.node_id and touched_src:
            self._file_node_commit(
                session.tenant_id,
                updated.node_id,
                kind="edit",
                instruction=(
                    f"edited {updated.folder}/{updated.name}".strip("/")
                ),
                by=session.principal_id,
            )
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
        # When each node last MOVED: the newest run that executed its
        # function — the sidebar orders by it, newest upper, like Life.
        last_activity: dict[str, str] = {}
        for s in self._durable.runs.list(limit=10_000):
            if s.contract.metadata.get("tenant_id") != session.tenant_id:
                continue
            nid = (s.contract.metadata.get("node_function") or {}).get("node_id")
            if not nid:
                continue
            moved = s.updated_at.isoformat()
            if moved > last_activity.get(nid, ""):
                last_activity[nid] = moved
        node_prefs: dict[str, dict] = {}
        if self._friendships is not None:
            node_prefs = self._friendships.prefs(
                tenant=session.tenant_id,
                owner=session.principal_id,
                kind="node",
            )
        items = []
        for e in entries:
            item = e.model_dump(mode="json")
            item["last_activity"] = last_activity.get(e.node_id, "")
            # The org a member serves under, IN WORDS: the onboarder's
            # card names the Supernode like the owner's does — never a
            # bare id, even when the parent is not on this desk.
            item["supernode_title"] = (
                desk.node_title(e.account.supernode_id)
                if e.account.supernode_id
                else ""
            )
            pref = node_prefs.get(e.node_id, {})
            item["pinned"] = bool(pref.get("pinned"))
            item["muted"] = bool(pref.get("muted"))
            item["hidden"] = _hidden_now(
                pref.get("hidden_at"), item["last_activity"]
            )
            # The node's own description — what it was built to do — for
            # the Code tab's README-like head. Best-effort: a node whose
            # registry record is unreadable simply shows no description.
            if self._nodeplace is not None:
                try:
                    version = self._nodeplace.latest_version(e.node_id)
                    skill = (
                        ReusableSkill.model_validate_json(
                            version.sanitized_skill_json
                        )
                        if version is not None
                        else None
                    )
                    item["summary"] = skill.description if skill else ""
                except Exception:  # noqa: BLE001
                    item["summary"] = ""
            items.append(item)
        return json_response(200, {"items": items})

    def _node_code_bytes(self, session, node_id: str) -> int:
        """The size of a node's program: its drawer's src/ bytes."""
        if self._files is None:
            return 0
        total = 0
        for file in self._files.list(tenant=session.tenant_id, node_id=node_id):
            if file.folder == "src" or file.folder.startswith("src/"):
                total += len(file.content or "")
        return total

    def _fleet_supernode(self, node_id: str):
        """The nearest Supernode a node serves under, or None — the org
        every fleet act (building, interact metering, assignment
        authority) answers to."""
        if self._desk is None:
            return None
        seen: set[str] = set()
        current = self._desk.account_for(node_id)
        while current is not None and current.node_id not in seen:
            seen.add(current.node_id)
            if current.is_supernode and current.node_id != node_id:
                return current
            if not current.supernode_id:
                return None
            current = self._desk.account_for(current.supernode_id)
        return None

    def _work_node_prefs_put(self, request, session, params) -> Response:
        """How a node sits in MY Work list — pin, mute, hide (delete-from-
        list). The node must be on the caller's own desk; the margins are
        the owner's alone, same store as friend and run threads."""
        from ..social import FriendshipError

        friends = self._require_friendships()
        desk = self._require_desk()
        node_id = params["node_id"]
        mine = {
            e.node_id
            for e in desk.overview(
                principal=session.principal_id, tenant=session.tenant_id
            )
        }
        if node_id not in mine:
            raise GatewayError(404, "not_found", "no such node on your desk")
        body = request.body or {}

        def _flag(name: str) -> bool | None:
            return bool(body[name]) if name in body else None

        try:
            pref = friends.set_pref(
                tenant=session.tenant_id,
                owner=session.principal_id,
                kind="node",
                key=node_id,
                pinned=_flag("pinned"),
                muted=_flag("muted"),
                hidden=_flag("hidden"),
            )
        except FriendshipError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        return json_response(200, {"node_id": node_id, **pref})

    def _work_assign(self, request, session, params) -> Response:
        """The Supernode's staffing hand: assign a user to an UNCLAIMED
        member node — the blue on-demand seat becomes an onboarded one.
        Only the org's own responsible may assign, and an already-claimed
        seat is refused in words, never reassigned silently."""
        desk = self._require_desk()
        node_id = params["node_id"]
        username = str((request.body or {}).get("username", "")).strip()
        if not username:
            raise GatewayError(400, "invalid_request", "name the user to assign")
        supernode = self._fleet_supernode(node_id)
        if supernode is None or supernode.responsible != session.principal_id:
            raise GatewayError(
                403,
                "forbidden",
                "only the Supernode's responsible may assign this seat",
            )
        try:
            account = desk.onboard_account(
                node_id, principal=username, tenant=session.tenant_id
            )
        except (ContributionError, OwnershipError, ValueError) as exc:
            raise GatewayError(409, "conflict", str(exc)) from exc
        self._durable.audit.append(
            "node.assigned",
            {
                "run_id": f"assign:{node_id}",
                "node_id": node_id,
                "assigned": username,
                "by": session.principal_id,
            },
        )
        return json_response(200, account.model_dump(mode="json"))

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
                    # The egress CONSENT: the exact hosts this node's http
                    # actions may reach — given and withdrawable by the
                    # humans who answer for the node, validated hard.
                    network_hosts=body.get("network_hosts"),
                    # The same consent inverted, for a Supernode whose web
                    # stands open (verified under the global account): the
                    # hosts the org refuses, and the principals it will
                    # not hear from — just like a user blocking a user.
                    blocked_hosts=body.get("blocked_hosts"),
                    blocked_users=body.get("blocked_users"),
                )
        except OwnershipError as exc:
            raise GatewayError(403, "forbidden", str(exc)) from exc
        except ContributionError as exc:
            raise GatewayError(404, "not_found", str(exc)) from exc
        except (ValueError, ValidationError) as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        return json_response(200, account.model_dump(mode="json"))

    def _work_order(self, request, session, params) -> Response:
        """The Supernode owner's SOP: where a member stands in the org's
        execution order. Work flows in ascending numbers — an explicit
        hand-off to the next node, like an SOP; members sharing a number
        run in PARALLEL; ``null`` clears it (the node is called whenever
        needed). Mutable — an SOP is retuned as the org learns — and
        only the parent Supernode's own humans may set it."""
        desk = self._require_desk()
        body = request.body or {}
        if "order" not in body:
            raise GatewayError(
                400,
                "invalid_request",
                "send order: a step number, or null for called-when-needed",
            )
        order = body.get("order")
        if isinstance(order, bool) or (
            order is not None and not isinstance(order, int)
        ):
            raise GatewayError(
                400, "invalid_request", "order must be a whole number or null"
            )
        try:
            account = desk.set_exec_order(
                params["node_id"],
                principal=session.principal_id,
                tenant=session.tenant_id,
                order=order,
            )
        except OwnershipError as exc:
            raise GatewayError(403, "forbidden", str(exc)) from exc
        except ContributionError as exc:
            raise GatewayError(404, "not_found", str(exc)) from exc
        except ValueError as exc:
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
    # Imitate: a guided lesson in the node's own window builds a node.    #
    # ------------------------------------------------------------------ #
    # The platform owns no global mouse/keyboard capture and no screen
    # recording (the shell is capability-minimal by design, and mobile
    # will never allow it) — what it owns COMPLETELY is everything that
    # runs through a node: the hash-chained audit of every execution and
    # each node's daily log file. So the lesson is taught here: the user
    # names the goal, describes each step, runs the real work through
    # the node while recording — and stop pairs those words with the
    # window's execution logs and builds through the one gated path.
    def _require_lessons(self):
        if self._lessons is None:
            raise GatewayError(
                404, "not_found", "imitation lessons are not enabled here"
            )
        return self._lessons

    # ------------------------------------------------------------------ #
    # Node webhooks: an outside system's door to one node's own function. #
    # ------------------------------------------------------------------ #
    def _node_hook_status(self, request, session, params) -> Response:
        self._imitate_entry(session, params["node_id"])
        hook = self._node_hooks.get(params["node_id"])
        return json_response(
            200,
            {
                "enabled": hook is not None,
                "created_at": hook.created_at if hook else None,
            },
        )

    def _node_hook_mint(self, request, session, params) -> Response:
        """Mint (or rotate) the node's webhook. The plaintext token is in
        THIS response and nowhere else ever again — only its digest is
        stored. The hook fires as the minter: their identity, their
        quotas, their node's egress grants, and every confirmation wall."""
        node_id = params["node_id"]
        self._imitate_entry(session, node_id)
        if self._function_for_node(session, node_id) is None:
            raise GatewayError(
                422,
                "cannot_execute",
                "this node has no execution function inside — a webhook "
                "would fire nothing; build the function first",
            )
        token = self._node_hooks.mint(
            node_id, tenant=session.tenant_id, principal=session.principal_id
        )
        self._durable.audit.append(
            "node.hook_minted",
            {
                "node_id": node_id,
                "tenant": session.tenant_id,
                "by": session.principal_id,
            },
        )
        return json_response(
            201,
            {
                "token": token,
                "path": f"/v1/hooks/nodes/{node_id}/{token}",
                "note": (
                    "shown once — store it now; minting again rotates it, "
                    "which is also how a leaked URL is revoked"
                ),
            },
        )

    def _node_hook_revoke(self, request, session, params) -> Response:
        self._imitate_entry(session, params["node_id"])
        revoked = self._node_hooks.revoke(params["node_id"])
        if revoked:
            self._durable.audit.append(
                "node.hook_revoked",
                {
                    "node_id": params["node_id"],
                    "tenant": session.tenant_id,
                    "by": session.principal_id,
                },
            )
        return json_response(200, {"enabled": False, "revoked": revoked})

    def _node_hook_fire(self, request, session, params) -> Response:
        """The public door: the token IS the credential. A wrong token and
        a node that never had a hook answer the SAME 404, so the door
        confirms nothing. The run wears the minter's identity and walls:
        run quota, egress grants, and the confirmation regime for
        model-written code all bind exactly as if they pressed run."""
        from types import SimpleNamespace

        record = self._node_hooks.verify(params["node_id"], params["token"])
        if record is None:
            raise GatewayError(404, "not_found", "no such hook")
        owner = SimpleNamespace(
            tenant_id=record.tenant, principal_id=record.principal
        )
        function = self._function_for_node(owner, record.node_id)
        if function is None:
            raise GatewayError(
                422,
                "cannot_execute",
                "the node this hook fires no longer has a function here",
            )
        # The production guard binds the public door too: a revoked
        # release never runs again, whoever rings.
        self._refuse_revoked(function)
        payload = request.body
        if payload is not None:
            text = json.dumps(payload, ensure_ascii=False)
            if len(text.encode("utf-8")) > _MAX_HOOK_PAYLOAD:
                raise GatewayError(
                    400,
                    "invalid_request",
                    f"webhook payload exceeds {_MAX_HOOK_PAYLOAD} bytes",
                )
            files = dict(function.get("files") or {})
            # The caller's payload, staged where the function was told to
            # look for it (NODE_FUNCTION_PROMPT names this exact file).
            files["webhook_payload.json"] = text
            function["files"] = files
        tenant_runs = sum(
            1
            for s in self._durable.runs.list()
            if s.contract.metadata.get("tenant_id") == record.tenant
        )
        if tenant_runs >= self._config.max_runs_per_tenant:
            raise GatewayError(429, "quota_exceeded", "tenant run quota exceeded")
        contract = TaskContract(
            intent=str(function["goal"]),
            submitted_by=record.principal,
            metadata={
                "tenant_id": record.tenant,
                "node_function": function,
                "trigger": "webhook",
            },
        )
        try:
            state = self._durable.submit(contract, max_recovery_attempts=1)
        except OrchestratorError as exc:
            raise GatewayError(422, "cannot_execute", str(exc)) from exc
        self._metrics["runs_submitted"] += 1
        self._record_function_verification(state)
        run = self._run_dict(state)
        self._durable.audit.append(
            "node.hook_fired",
            {
                "node_id": record.node_id,
                "tenant": record.tenant,
                "run_id": run.get("run_id"),
            },
        )
        return json_response(
            202,
            {
                "run_id": run.get("run_id"),
                "phase": run.get("phase"),
                "awaiting": run.get("awaiting"),
            },
        )

    # ------------------------------------------------------------------ #
    # Node deletion: tombstone now, revive within the window, purge after.#
    # ------------------------------------------------------------------ #
    def _work_node_delete(self, request, session, params) -> Response:
        """Delete the node for REAL — everywhere at once: off the Work
        desk, off its Supernode's member roster, out of run resolution,
        its marketplace listing revoked. The tombstone stands for
        ``NODE_REVIVAL_DAYS`` so an administrator can undo an accident;
        then the retention pass purges the account and the node's
        drawer for good."""
        desk, _entry = self._imitate_entry(session, params["node_id"])
        node_id = params["node_id"]
        now = request.now or self._clock()
        if not desk.delete_node(node_id, at=now):
            raise GatewayError(404, "not_found", "no such node to delete")
        # The marketplace listing goes with it — best-effort: an account
        # responsible who is not the registry creator cannot revoke the
        # listing, but the desk/roster/resolution walls bind regardless.
        if self._nodeplace is not None:
            try:
                self._nodeplace.revoke(
                    node_id,
                    noder_principal=session.principal_id,
                    tenant_id=session.tenant_id,
                )
            except Exception:  # noqa: BLE001 — the tombstone already stands
                pass
        revivable_until = now + timedelta(days=NODE_REVIVAL_DAYS)
        self._durable.audit.append(
            "node.deleted",
            {
                "node_id": node_id,
                "by": session.principal_id,
                "tenant": session.tenant_id,
                "deleted_at": now.isoformat(),
                "revivable_until": revivable_until.isoformat(),
            },
        )
        return json_response(
            200,
            {"deleted": True, "revivable_until": revivable_until.isoformat()},
        )

    def _work_node_revive(self, request, session, params) -> Response:
        """The administrator's undo: within the window, the node's own
        responsible/admin — or its Supernode's — brings an accidentally
        deleted node back whole. After the window: gone for good (410),
        which is exactly what the delete promised."""
        desk = self._require_desk()
        node_id = params["node_id"]
        account = desk.account_for(node_id)
        if (
            account is None
            or account.deleted_at is None
            or desk.node_tenant(node_id) != session.tenant_id
        ):
            raise GatewayError(404, "not_found", "no deleted node by that id")
        allowed = {account.responsible, account.admin} - {None, ""}
        if account.supernode_id:
            parent = desk.account_for(account.supernode_id)
            if parent is not None:
                allowed |= {parent.responsible, parent.admin} - {None, ""}
        if session.principal_id not in allowed:
            raise GatewayError(
                403, "forbidden", "only its administrators may revive a node"
            )
        now = request.now or self._clock()
        deadline = account.deleted_at + timedelta(days=NODE_REVIVAL_DAYS)
        if now > deadline:
            raise GatewayError(
                410,
                "gone",
                "the revival window has closed — the delete stands",
            )
        desk.revive_node(node_id)
        self._durable.audit.append(
            "node.revived",
            {
                "node_id": node_id,
                "by": session.principal_id,
                "tenant": session.tenant_id,
            },
        )
        return json_response(200, {"revived": True})

    def _work_deleted_members(self, request, session, params) -> Response:
        """A Supernode's recently deleted members — the revival list its
        administrators read. Walled to the caller's own desk."""
        desk, _entry = self._imitate_entry(session, params["node_id"])
        items = desk.deleted_members_of(
            params["node_id"], tenant=session.tenant_id
        )
        for item in items:
            deleted_at = datetime.fromisoformat(item["deleted_at"])
            item["revivable_until"] = (
                deleted_at + timedelta(days=NODE_REVIVAL_DAYS)
            ).isoformat()
        return json_response(200, {"items": items})

    def _node_deleted(self, node_id: str) -> bool:
        """Whether the node is tombstoned — resolution, reuse offers,
        and the build dedupe all treat a deleted node as absent."""
        if self._desk is None:
            return False
        account = self._desk.account_for(node_id)
        return account is not None and account.deleted_at is not None

    def _purge_deleted_nodes(self, now) -> None:
        """The delete becomes real: accounts whose revival window has
        passed leave the books, and each node's drawer and webhook go
        with them. Rides the retention tick; never raises into serving."""
        if self._desk is None:
            return
        cutoff = now - timedelta(days=NODE_REVIVAL_DAYS)
        for account in self._desk.purge_deleted(before=cutoff):
            node_id = account.node_id
            tenant = self._desk.node_tenant(node_id) or ""
            if self._files is not None and tenant:
                for file in self._files.list(tenant=tenant, node_id=node_id):
                    self._files.delete(file.file_id, tenant=tenant)
            self._node_hooks.revoke(node_id)
            self._durable.audit.append(
                "node.purged",
                {"node_id": node_id, "tenant": tenant},
            )

    # ------------------------------------------------------------------ #
    # Node provenance doors: history, releases, revocation.               #
    # ------------------------------------------------------------------ #
    def _require_provenance(self):
        if self._provenance is None:
            raise GatewayError(
                404, "not_found", "node provenance is not enabled here"
            )
        return self._provenance

    def _node_commits(self, request, session, params) -> Response:
        """The node's function history, newest first — every build,
        revision, repair, and hand edit as an immutable commit, read
        like a repo's log. Walled to the caller's own desk."""
        ledger = self._require_provenance()
        self._imitate_entry(session, params["node_id"])
        items = [
            {
                "commit_id": commit.commit_id,
                "parent_id": commit.parent_id,
                "tree_hash": commit.tree_hash,
                "kind": commit.kind,
                "instruction": commit.instruction,
                "by": commit.by,
                "created_at": commit.created_at.isoformat(),
                "files": sorted(commit.file_hashes),
            }
            for commit in ledger.history(session.tenant_id, params["node_id"])
        ]
        return json_response(200, {"items": items})

    def _node_releases(self, request, session, params) -> Response:
        """What verification sealed, newest first — each release with
        its live operational status (active | revoked) riding along.
        The artifact rows never change; only the status does."""
        ledger = self._require_provenance()
        self._imitate_entry(session, params["node_id"])
        return json_response(
            200,
            {"items": ledger.releases(session.tenant_id, params["node_id"])},
        )

    def _node_release_revoke(self, request, session, params) -> Response:
        """The revocation door: a vulnerable release is revoked in words
        — reason required — never silently modified. New runs of that
        exact tree refuse from this moment; a REVISED function is a new
        draft and runs to earn a new seal. Idempotent; the first reason
        stands."""
        ledger = self._require_provenance()
        self._imitate_entry(session, params["node_id"])
        release = ledger.get_release(
            session.tenant_id, params["node_id"], params["release_id"]
        )
        if release is None:
            raise GatewayError(404, "not_found", "no such release of this node")
        reason = str((request.body or {}).get("reason") or "").strip()
        if not reason:
            raise GatewayError(
                400, "invalid_request", 'give the reason: {"reason": "..."}'
            )
        flipped = ledger.revoke(
            session.tenant_id,
            release.release_id,
            reason=reason,
            by=session.principal_id,
        )
        if flipped:
            self._durable.audit.append(
                "node.release_revoked",
                {
                    "node_id": params["node_id"],
                    "release_id": release.release_id,
                    "tree_hash": release.tree_hash,
                    "reason": reason,
                    "by": session.principal_id,
                },
            )
        control = ledger.control(session.tenant_id, release.release_id)
        return json_response(
            200,
            {
                "release_id": release.release_id,
                "status": control.get("status", "revoked"),
                "reason": control.get("reason", reason),
            },
        )

    def _imitate_entry(self, session, node_id: str):
        """Imitate happens on the caller's OWN desk — teaching demands
        the teacher answer for the node whose window records it."""
        desk = self._require_desk()
        entry = next(
            (
                e
                for e in desk.overview(
                    principal=session.principal_id, tenant=session.tenant_id
                )
                if e.node_id == node_id
            ),
            None,
        )
        if entry is None:
            raise GatewayError(404, "not_found", "no such node on your desk")
        return desk, entry

    @staticmethod
    def _lesson_json(lesson) -> dict:
        return {
            "lesson_id": lesson.lesson_id,
            "node_id": lesson.node_id,
            "goal": lesson.goal,
            "status": lesson.status,
            "created_at": lesson.created_at.isoformat(),
            "ended_at": (
                lesson.ended_at.isoformat() if lesson.ended_at else None
            ),
            "built_node_id": lesson.built_node_id,
            "steps": [
                {
                    "seq": s.seq,
                    "kind": s.kind,
                    "text": s.text,
                    "at": s.at.isoformat(),
                }
                for s in lesson.steps
            ],
        }

    def _imitate_status(self, request, session, params) -> Response:
        lessons = self._require_lessons()
        self._imitate_entry(session, params["node_id"])
        lesson = lessons.active(
            tenant=session.tenant_id,
            node_id=params["node_id"],
            owner=session.principal_id,
        )
        return json_response(
            200, {"lesson": self._lesson_json(lesson) if lesson else None}
        )

    def _imitate_start(self, request, session, params) -> Response:
        lessons = self._require_lessons()
        self._imitate_entry(session, params["node_id"])
        try:
            lesson = lessons.start(
                tenant=session.tenant_id,
                node_id=params["node_id"],
                owner=session.principal_id,
                goal=str((request.body or {}).get("goal", "")),
            )
        except ValueError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        return json_response(201, {"lesson": self._lesson_json(lesson)})

    def _imitate_step(self, request, session, params) -> Response:
        lessons = self._require_lessons()
        self._imitate_entry(session, params["node_id"])
        lesson = lessons.active(
            tenant=session.tenant_id,
            node_id=params["node_id"],
            owner=session.principal_id,
        )
        if lesson is None:
            raise GatewayError(404, "not_found", "no lesson is recording here")
        try:
            lessons.add_step(
                lesson.lesson_id,
                tenant=session.tenant_id,
                owner=session.principal_id,
                kind="say",
                text=str((request.body or {}).get("text", "")),
            )
        except ValueError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        fresh = lessons.get(
            lesson.lesson_id,
            tenant=session.tenant_id,
            owner=session.principal_id,
        )
        return json_response(200, {"lesson": self._lesson_json(fresh)})

    def _pair_lesson_runs(self, desk, session, lesson) -> list[str]:
        """The automatic half of the demonstration: every run this node
        executed while the lesson recorded, read from the same audit
        chain the activity feed serves — the user's words paired with
        what the machine verifiably DID."""
        started = lesson.created_at.isoformat()
        paired: list[str] = []
        try:
            feed = desk.activity(lesson.node_id, tenant=session.tenant_id)
        except ContributionError:
            return paired
        for run in feed:
            if not run.steps:
                continue
            if run.steps[-1]["at"] < started:
                continue  # ran before the lesson opened — not part of it
            outcome = run.steps[-1]["event_type"]
            paired.append(
                f"run {run.run_id[:8]}: {len(run.steps)} logged events, "
                f"ended {outcome}"
            )
        return paired

    def _imitate_stop(self, request, session, params) -> Response:
        """Close the lesson. ``build: true`` compiles the demonstration —
        the user's ordered steps plus the runs the window logged — into
        ONE node through the same gated build path as every other door,
        and files the lesson verbatim into the new node's drawer as a
        training data log. ``build: false`` discards, keeping the record."""
        lessons = self._require_lessons()
        desk, _entry = self._imitate_entry(session, params["node_id"])
        lesson = lessons.active(
            tenant=session.tenant_id,
            node_id=params["node_id"],
            owner=session.principal_id,
        )
        if lesson is None:
            raise GatewayError(404, "not_found", "no lesson is recording here")
        build = bool((request.body or {}).get("build", False))
        if not build:
            closed = lessons.finish(
                lesson.lesson_id,
                tenant=session.tenant_id,
                owner=session.principal_id,
                status="discarded",
            )
            return json_response(
                200, {"lesson": self._lesson_json(closed), "say": ""}
            )
        said = [s.text for s in lesson.steps if s.kind == "say"]
        if not said:
            raise GatewayError(
                400,
                "invalid_request",
                "a lesson needs at least one demonstrated step — describe "
                "what to do, in order, before building",
            )
        # Pair the words with the logs, then record the pairing ON the
        # lesson — the stored demonstration is the full training record.
        for line in self._pair_lesson_runs(desk, session, lesson):
            try:
                lessons.add_step(
                    lesson.lesson_id,
                    tenant=session.tenant_id,
                    owner=session.principal_id,
                    kind="run",
                    text=line,
                )
            except ValueError:
                break  # the lesson is full — the said steps still build
        lesson = lessons.get(
            lesson.lesson_id,
            tenant=session.tenant_id,
            owner=session.principal_id,
        )
        demonstrated = [
            f"{s.text}" if s.kind == "say" else f"(observed: {s.text})"
            for s in lesson.steps
        ]
        say = self._build_function_node(
            session, lesson.goal, demonstrated=demonstrated
        )
        if say.startswith("error:"):
            # The lesson stays recording: fix the goal or add a step and
            # press stop again — nothing recorded is lost to a refusal.
            return json_response(
                200, {"lesson": self._lesson_json(lesson), "say": say}
            )
        built_id = self._lesson_built_node_id(session, lesson.goal)
        closed = lessons.finish(
            lesson.lesson_id,
            tenant=session.tenant_id,
            owner=session.principal_id,
            status="built",
            built_node_id=built_id,
        )
        self._file_lesson_log(session, closed, built_id)
        return json_response(
            200, {"lesson": self._lesson_json(closed), "say": say}
        )

    def _lesson_built_node_id(self, session, goal: str) -> str:
        """The node the build just minted — found deterministically by
        the same goal-derived skill id the builder used."""
        if self._nodeplace is None:
            return ""
        skill_id = self._function_skill_id(session.tenant_id, goal)
        node = next(
            (
                n
                for n in self._nodeplace.list_own_nodes(
                    noder_principal=session.principal_id,
                    tenant_id=session.tenant_id,
                )
                if n.skill_id == skill_id
            ),
            None,
        )
        return node.node_id if node is not None else ""

    def _file_lesson_log(self, session, lesson, built_id: str) -> None:
        """The lesson, verbatim, as a JSON data log in the BUILT node's
        drawer — node creation requirements as a solid training record:
        goal, ordered demonstrated steps, paired executions, timestamps."""
        if self._files is None or not built_id or lesson is None:
            return
        try:
            self._files.save(
                UserFile(
                    tenant_id=session.tenant_id,
                    node_id=built_id,
                    name=f"lesson-{lesson.lesson_id[:8]}.json",
                    folder="lessons",
                    media_type="application/json",
                    content=json.dumps(
                        {
                            **self._lesson_json(lesson),
                            "taught_by": session.principal_id,
                            "taught_in_node": lesson.node_id,
                        },
                        indent=2,
                    ),
                )
            )
        except FileTooLargeError:
            pass  # a lesson that big still built; only the copy is skipped

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
    # The Supernode's template button: a working structure, imported.    #
    # ------------------------------------------------------------------ #
    # A member whose function has grown past this many bytes of src/ is
    # a seat doing several jobs: the structure should re-reason and
    # BRANCH the work into more seats. Code size is the trigger — it is
    # measurable, monotone with complexity, and read off the drawer.
    REBRANCH_CODE_BYTES = 24_000

    def _resolve_org_template(self, session, node_id: str, *, re_reason=False):
        """Gate, resolve, record — the shared half of preview and apply.

        Deterministic plan first, exactly like node execution: a RECORDED
        choice returns instantly (never re-reasoned); a keyword match on
        the Supernode's description is pure arithmetic; only when the
        evidence is thin is the model consulted — and then only to PICK a
        key from the catalog, never to invent a structure. The verdict is
        recorded on the account, so every later press — preview or apply,
        for every role and node id — is free and identical."""
        from ..nodeplace.org_templates import (
            model_chooser,
            resolve_org_template,
        )

        desk = self._require_desk()
        try:
            account = desk.supernode_owned(
                node_id, principal=session.principal_id, tenant=session.tenant_id
            )
        except OwnershipError as exc:
            raise GatewayError(403, "forbidden", str(exc)) from exc
        except ContributionError as exc:
            raise GatewayError(404, "not_found", str(exc)) from exc
        description = desk.describe(node_id, tenant=session.tenant_id)
        # Re-reasoning drops the recorded verdict: the model is consulted
        # afresh because the trigger (code size) says the shape moved.
        recorded = "" if re_reason else account.org_template
        author = (
            None
            if recorded
            else self._seat_actor(
                self._node_function_author(session.tenant_id),
                session.principal_id,
            )
        )
        resolved = resolve_org_template(
            description,
            recorded=recorded,
            chooser=model_chooser(author) if author is not None else None,
        )
        desk.record_org_template(
            node_id,
            principal=session.principal_id,
            tenant=session.tenant_id,
            key=resolved.template.key,
        )
        return desk, resolved

    def _org_template(self, request, session, params) -> Response:
        """Preview: the resolved structure, role by role, with which
        seats already exist under this Supernode — nothing minted."""
        node_id = params["node_id"]
        desk, resolved = self._resolve_org_template(session, node_id)
        members = desk.members_of(node_id, tenant=session.tenant_id)
        existing = {m["title"].strip().lower() for m in members}
        # Growth pressure, read off each member's drawer: a seat whose
        # function outgrew the branch threshold marks the structure as
        # due for a re-reason — the operator's button, never a silent
        # re-plan.
        pressure = []
        for m in members:
            code = self._node_code_bytes(session, m["node_id"])
            pressure.append(
                {
                    "node_id": m["node_id"],
                    "title": m["title"],
                    "code_bytes": code,
                    "over": code > self.REBRANCH_CODE_BYTES,
                }
            )
        return json_response(
            200,
            {
                "members": pressure,
                "needs_branch": any(m["over"] for m in pressure),
                "branch_threshold_bytes": self.REBRANCH_CODE_BYTES,
                "key": resolved.template.key,
                "name": resolved.template.name,
                "purpose": resolved.template.purpose,
                "source": resolved.source,
                "evidence": list(resolved.evidence),
                "roles": [
                    {
                        "name": role.name,
                        "responsibility": role.responsibility,
                        "goal": role.goal,
                        "authority": role.authority,
                        "exists": role.name.strip().lower() in existing,
                    }
                    for role in resolved.template.roles
                ],
            },
        )

    def _org_template_apply(self, request, session, params) -> Response:
        """Apply: import the missing seats as member nodes under this
        Supernode — each with its NAME, its one responsibility, and its
        essential function as a DETERMINISTIC script (the template is the
        plan; no model writes these). Idempotent by role name: a seat
        that already sits is skipped, never duplicated. Members start
        unclaimed, exactly like any node minted under a Supernode."""
        from ..nodeplace.org_templates import role_script

        node_id = params["node_id"]
        desk, resolved = self._resolve_org_template(
            session, node_id,
            re_reason=bool((request.body or {}).get("re_reason")),
        )
        if self._nodeplace is None:
            raise GatewayError(404, "not_found", "nodes are not enabled here")
        existing = {
            m["title"].strip().lower()
            for m in desk.members_of(node_id, tenant=session.tenant_id)
        }
        created: list[dict] = []
        skipped: list[dict] = []
        for role in resolved.template.roles:
            if role.name.strip().lower() in existing:
                skipped.append({"name": role.name, "reason": "already seated"})
                continue
            skill_id = self._function_skill_id(session.tenant_id, role.goal)
            skill = ReusableSkill.model_validate(
                {
                    "id": skill_id,
                    "name": role.name,
                    "description": role.goal,
                    "signature": {"application": "script", "adapter": "script"},
                    "parameters": [],
                    # The seat's essential function: deterministic, from
                    # the template — emits the role's structured work
                    # product. Grown later by rebuilding with a model.
                    "actions": [
                        {
                            "correlation_id": "function",
                            "adapter": "script",
                            "operation": "run",
                            "parameters": {
                                "goal": role.goal,
                                "script": role_script(role),
                                "node_key": f"node:{skill_id}",
                            },
                        }
                    ],
                }
            )
            try:
                result = self._nodeplace.contribute(
                    noder_principal=session.principal_id,
                    tenant_id=session.tenant_id,
                    skill=skill,
                    semver="1.0.0",
                    title=role.name,
                    summary=role.responsibility,
                    produces=[
                        Slot(
                            name="work_product",
                            value_type="str",
                            role="result",
                        )
                    ],
                )
                desk.create_account(
                    result.node.node_id,
                    principal=session.principal_id,
                    tenant=session.tenant_id,
                    supernode_id=node_id,
                    authority_level=role.authority,
                    policy_version=NODE_POLICY_VERSION,
                )
            except (ContributionError, OwnershipError, ValueError) as exc:
                skipped.append({"name": role.name, "reason": str(exc)})
                continue
            created.append(
                {
                    "node_id": result.node.node_id,
                    "name": role.name,
                    "authority": role.authority,
                }
            )
        self._durable.audit.append(
            "org_template.applied",
            {
                "run_id": f"template:{node_id}",
                "node_id": node_id,
                "template": resolved.template.key,
                "source": resolved.source,
                "created": len(created),
                "skipped": len(skipped),
                "by": session.principal_id,
            },
        )
        return json_response(
            200,
            {
                "key": resolved.template.key,
                "name": resolved.template.name,
                "source": resolved.source,
                "created": created,
                "skipped": skipped,
            },
        )

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

    def _bundle_sweep(self):
        """The configured CAS sweep, or None when the bundle store isn't
        wired (a minimal install has no frozen trees to reclaim)."""
        if self._bundle_store is None or self._files is None:
            return None
        from ..runtime.sweep import CallableSource, CasSweep

        return CasSweep(
            self._bundle_store,
            # The CAS the bundle blobs live in — the same object store the
            # drawer's blobs use in a real install, so the drawer reference
            # source is valid against it.
            self._bundle_store.artifacts,
            sources=[CallableSource("drawer", self._drawer_blob_refs)],
            live_bundle_ids=self._bundle_live_ids,
            tiers=self._bundle_tiers,
        )

    def _bundle_inventory(self, request, session, params) -> Response:
        """The frozen trees themselves: every stored manifest with its
        size, age, and which nodes freeze to it right now. ``live`` here
        is EXACTLY the sweep's reachability — the same recomputation from
        each node's current drawer — so a bundle shown unreferenced is one
        the next sweep would reap (once its blobs age past the grace)."""
        if self._bundle_store is None:
            raise GatewayError(404, "not_found", "the bundle store is not enabled")
        holders: dict[str, list[str]] = {}
        if self._nodeplace is not None:
            for node in self._nodeplace.all_nodes():
                tree = self._node_src_bundle_tree(node.tenant_id, node.node_id)
                bundle_id = self._freeze_tree(tree)
                if bundle_id is not None:
                    holders.setdefault(bundle_id, []).append(node.skill_id)
        stored = self._bundle_store.manifests()
        items = [
            {
                "bundle_id": manifest.bundle_id,
                "file_count": manifest.file_count,
                "total_bytes": manifest.total_bytes,
                "created_at": created,
                "live": manifest.bundle_id in holders,
                "held_by": sorted(holders.get(manifest.bundle_id, ())),
            }
            for manifest, created in stored
        ]
        items.reverse()  # newest first, like the history card
        return json_response(
            200,
            {
                "items": items,
                "count": len(items),
                "total_bytes": sum(m.total_bytes for m, _ in stored),
            },
        )

    def _bundle_sweep_inspect(self, request, session, params) -> Response:
        """Dry run: exactly what the sweep WOULD reclaim, touching nothing."""
        sweep = self._bundle_sweep()
        if sweep is None:
            raise GatewayError(404, "not_found", "the bundle store is not enabled")
        return json_response(200, sweep.inspect().as_dict())

    def _bundle_sweep_apply(self, request, session, params) -> Response:
        """Reclaim the store's dead frozen trees. A platform move — approve
        authority required, like the hygiene sweep — and the outcome lands
        in the audit trail."""
        sweep = self._bundle_sweep()
        if sweep is None:
            raise GatewayError(404, "not_found", "the bundle store is not enabled")
        if self._approval is None:
            raise GatewayError(404, "not_found", "approval authority is not configured")
        try:
            self._approval.approve(
                session,
                run_id="bundles:sweep",
                policy="bundles.sweep",
                requester_id="",
                now=request.now or self._clock(),
            )
        except AuthorizationError as exc:
            raise GatewayError(403, "forbidden", str(exc)) from exc
        plan = sweep.collect()
        self._durable.audit.append(
            "bundles.swept",
            {
                "run_id": "bundles:sweep",
                "by": session.principal_id,
                "dead_manifests": len(plan.dead_manifests),
                "orphan_blobs": len(plan.orphan_blobs),
                "reclaimed_bytes": plan.reclaimed_bytes,
            },
        )
        return json_response(200, plan.as_dict())

    # ------------------------------------------------------------------ #
    # The sweep's recurring Routine.                                       #
    # ------------------------------------------------------------------ #
    def _sweep_schedule_view(self, request, session, params) -> Response:
        view = self._sweep_schedule.view()
        return json_response(200, view or {"enabled": False})

    def _sweep_schedule_set(self, request, session, params) -> Response:
        """Stand up (or retune) the Routine. This is where the consent for
        every future unattended firing is given, so it passes the same
        approve gate as a manual sweep — once, audited, revocable."""
        if self._bundle_store is None or self._files is None:
            raise GatewayError(404, "not_found", "the bundle store is not enabled")
        if self._approval is None:
            raise GatewayError(404, "not_found", "approval authority is not configured")
        now = request.now or self._clock()
        try:
            self._approval.approve(
                session,
                run_id="bundles:schedule",
                policy="bundles.sweep",
                requester_id="",
                now=now,
            )
        except AuthorizationError as exc:
            raise GatewayError(403, "forbidden", str(exc)) from exc
        raw = (request.body or {}).get("interval_hours", 24)
        try:
            interval = float(raw)
        except (TypeError, ValueError) as exc:
            raise GatewayError(
                400, "invalid_request", "interval_hours must be a number"
            ) from exc
        view = self._sweep_schedule.enable(
            interval_hours=interval,
            granted_by=session.principal_id,
            tenant=session.tenant_id,
            now=now,
        )
        self._durable.audit.append(
            "bundles.sweep_scheduled",
            {
                "run_id": "bundles:schedule",
                "by": session.principal_id,
                "tenant": session.tenant_id,
                "interval_hours": view["interval_hours"],
            },
        )
        return json_response(200, view)

    def _sweep_schedule_clear(self, request, session, params) -> Response:
        """Revoke the standing consent — same authority that granted it."""
        if self._approval is None:
            raise GatewayError(404, "not_found", "approval authority is not configured")
        try:
            self._approval.approve(
                session,
                run_id="bundles:schedule",
                policy="bundles.sweep",
                requester_id="",
                now=request.now or self._clock(),
            )
        except AuthorizationError as exc:
            raise GatewayError(403, "forbidden", str(exc)) from exc
        disabled = self._sweep_schedule.disable()
        if disabled:
            self._durable.audit.append(
                "bundles.sweep_unscheduled",
                {"run_id": "bundles:schedule", "by": session.principal_id},
            )
        return json_response(200, {"enabled": False, "disabled": disabled})

    def _bundle_sweep_audit(self, request, session, params) -> Response:
        """The sweep's whole history, straight off the hash-chained audit
        log: consents granted and revoked, and every firing — manual or
        scheduled — newest first, capped small. No new bookkeeping: the
        records were already being written; this reads them back."""
        records = [
            record
            for run_id in ("bundles:sweep", "bundles:schedule")
            for record in self._durable.audit.records(run_id=run_id)
        ]
        records.sort(key=lambda record: record.seq, reverse=True)
        return json_response(
            200,
            {
                "items": [
                    {
                        "at": record.at.isoformat(),
                        "event_type": record.event_type,
                        **{
                            key: value
                            for key, value in record.payload.items()
                            if key != "run_id"
                        },
                    }
                    for record in records[:50]
                ]
            },
        )

    def _maybe_scheduled_sweep(self, request) -> None:
        """The lazy tick: cheap by construction. A monotonic gate bounds
        due-checks to one per minute per host; the durable claim decides
        the actual firing; and nothing in here may raise into the request."""
        import time as time_module

        try:
            now_mono = time_module.monotonic()
            # Retention rides the same lazy traffic but keeps its OWN
            # hourly gate — it must not depend on a bundle schedule
            # existing or on the sweep's minute window.
            self._maybe_retention(now_mono, request.now or self._clock())
            if now_mono < self._sweep_gate:
                return
            self._sweep_gate = now_mono + 60.0
            self._scheduled_sweep_tick(request.now or self._clock())
        except Exception:  # noqa: BLE001 - maintenance never breaks serving
            logging.getLogger("oolu.gateway").exception("scheduled sweep tick failed")

    def _maybe_retention(self, now_mono: float, now) -> None:
        """The activity log's retention, applied for real: once an hour,
        terminal runs, finished tasks, delivered outbox rows, and the
        audit chain's oldest prefix older than ``retention_days`` leave
        the books — trimmed, audited, and never touching live work. Off
        when the window is 0."""
        days = float(getattr(self._config, "retention_days", 0.0) or 0.0)
        if days <= 0 or now_mono < self._retention_gate:
            return
        self._retention_gate = now_mono + 3600.0
        from ..durable.maintenance import prune_retention

        pruned = prune_retention(
            self._durable.conn, older_than_days=days, now=now
        )
        if any(pruned.values()):
            self._durable.audit.append(
                "retention.pruned",
                {"run_id": "retention:tick", "days": days, **pruned},
            )
        # Deleted nodes whose revival window has passed go for good.
        self._purge_deleted_nodes(now)

    def _scheduled_sweep_tick(self, now) -> None:
        """One due-check and, when this host wins the claim, one sweep —
        under the schedule's standing consent, audited like a manual run."""
        if not self._sweep_schedule.claim_due(now):
            return
        schedule = self._sweep_schedule.view() or {}
        sweep = self._bundle_sweep()
        if sweep is None:
            self._sweep_schedule.record_result(
                now, error="the bundle store is not enabled"
            )
            return
        try:
            plan = sweep.collect()
        except Exception as exc:  # noqa: BLE001 - the Routine records its
            # own failure and waits for the next interval; it never raises.
            self._sweep_schedule.record_result(now, error=str(exc))
            logging.getLogger("oolu.gateway").exception("scheduled sweep failed")
            return
        summary = {
            "dead_manifests": len(plan.dead_manifests),
            "orphan_blobs": len(plan.orphan_blobs),
            "reclaimed_bytes": plan.reclaimed_bytes,
            "tier_discards": plan.tier_discards,
        }
        self._sweep_schedule.record_result(now, summary=summary)
        self._durable.audit.append(
            "bundles.swept",
            {
                "run_id": "bundles:schedule",
                "scheduled": True,
                # The firing runs under the STANDING consent — name whose.
                "granted_by": schedule.get("granted_by", ""),
                **summary,
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
        advise through the LEARNED STACK — Beta counts first (direct
        evidence), the small transformer for what counts cannot see
        (cold starts, cross-goal shapes) — constructed per request
        because the evidence pool is the TENANT's history, never a
        neighbor's. Containment is the port's: advice stays clamped to
        DEFAULT_PROPOSAL_STRENGTH pseudo-observations."""
        if self._proposal_model is not None:
            return self._proposal_model
        if self._trace_store is None:
            return None
        from ..orchestrator.proposals import TraceProposalModel
        from ..orchestrator.ranker import (
            LearnedProposalStack,
            TinyTransformerProposalModel,
        )

        return LearnedProposalStack(
            TraceProposalModel(self._trace_store, context=session.tenant_id),
            TinyTransformerProposalModel(
                self._trace_store, context=session.tenant_id
            ),
        )

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

    def _stamp_fleet_order(self, contract: NodeContract) -> NodeContract:
        """The Supernode owners' execution order, stamped as ``sop`` edges.

        Within one Supernode's members present on this contract, every
        child in an earlier order group must finish before any child in
        the next present group — the explicit hand-off of an SOP. Equal
        numbers share a group and run in parallel; members with no order
        impose nothing (called whenever needed). Existing edges — explicit
        or implied by typed data flow — outrank the SOP in either
        direction: a slot dependency is physics, and a contradiction must
        surface as parallelism, never become a cycle."""
        body = contract.body
        if not isinstance(body, SubgraphBody):
            return contract
        edges_for = getattr(self._desk, "sop_edges_for", None)
        if edges_for is None:
            return contract
        pairs = edges_for([child.id for child in body.nodes])
        if not pairs:
            return contract
        fixed = {(e.source, e.target) for e in body.edges} | {
            (e.source, e.target) for e in derive_data_edges(body.nodes)
        }
        added = [
            ContractEdge(source=source, target=target, provenance="sop")
            for source, target in pairs
            if (source, target) not in fixed and (target, source) not in fixed
        ]
        if not added:
            return contract
        return contract.model_copy(
            update={
                "body": SubgraphBody(
                    nodes=list(body.nodes), edges=list(body.edges) + added
                )
            }
        )

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
        if self._desk is not None:
            # The Supernode owners' SOP binds HERE, where every contract
            # passes on its way to execution: their execution order lands
            # as explicit sop edges the scheduler honors — work passed to
            # the next number, ties in parallel, unordered members free.
            contract = self._stamp_fleet_order(contract)
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
            # Each registered child's http actions carry that node's egress
            # CONSENT into the executor — stamped at execution time, so the
            # run honors the grants of this moment, not of compile time.
            compiled = self._stamp_egress(contract, compiled, children)

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

    def _stamp_egress(self, contract, compiled, children):
        """Stamp each registered child's egress regime onto its http
        actions: the allow-grant by default, or the OPEN web (minus the
        org's blocked hosts) for nodes under a Supernode verified as a
        legal entity under the global account — trust widens consent,
        the same way it lifts ranking."""
        ids = [c.id for c in children]
        open_grants: dict[str, tuple[str, ...]] = {}
        if self._kyc is not None:
            for version_id, node_id in self._desk.owning_nodes(ids).items():
                verdict = self._kyc.open_egress(
                    node_id, default_open=bool(self._config.global_service)
                )
                if verdict is not None:
                    open_grants[version_id] = verdict
        return stamp_egress_grants(
            contract,
            compiled,
            self._desk.network_grants(ids),
            open_grants=open_grants,
        )

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
        if self._desk is not None:
            # Consent is withdrawable while a contract sits held, so the
            # egress grants are stamped from the accounts of THIS moment —
            # the approver authorizes a run under current consent.
            members = (
                parsed.body.nodes
                if isinstance(parsed.body, SubgraphBody)
                else [parsed]
            )
            compiled = self._stamp_egress(parsed, compiled, members)
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

    def _require_values(self):
        if self._values is None:
            raise GatewayError(
                404, "not_found", "the value store is not enabled here"
            )
        return self._values

    def _run_values(self, request, session, params) -> Response:
        """A run's result outputs, filed as immutable exact-value refs —
        idempotent (content-addressed), walled to the run's own
        submitter, audited on first filing."""
        store = self._require_values()
        state = self._durable.runs.get(params["run_id"])
        if (
            state is None
            or state.contract.metadata.get("tenant_id") != session.tenant_id
            or state.contract.submitted_by != session.principal_id
        ):
            raise GatewayError(404, "not_found", "no such run of yours")
        outputs = (state.result or {}).get("outputs") or []
        refs: dict[str, str] = {}
        for index, output in enumerate(outputs):
            filed = store.snapshot_outputs(
                session.tenant_id, output, label=f"{state.run_id}:{index}"
            )
            for name, ref in filed.items():
                refs[f"{index}.{name}" if len(outputs) > 1 else name] = ref
        self._durable.audit.append(
            "values.snapshot",
            {
                "run_id": state.run_id,
                "by": session.principal_id,
                "refs": sorted(refs.values()),
            },
        )
        return json_response(200, {"run_id": state.run_id, "fields": refs})

    def _run_lineage(self, request, session, params) -> Response:
        """A run's values in their chain, both directions: each output
        field's reference, the inputs it was computed FROM, and the work
        later computed from it. Snapshots are content-addressed, so
        re-deriving the refs here lands on the exact rows completion
        filed — no second copy, no drift."""
        store = self._require_values()
        state = self._durable.runs.get(params["run_id"])
        if (
            state is None
            or state.contract.metadata.get("tenant_id") != session.tenant_id
            or state.contract.submitted_by != session.principal_id
        ):
            raise GatewayError(404, "not_found", "no such run of yours")
        items: list[dict] = []
        for output in (state.result or {}).get("outputs") or []:
            # The payload the function emitted is what completion filed;
            # the evidence wrapper around it is bookkeeping, not a value.
            payload = (
                output.get("result") if isinstance(output, dict) else None
            )
            if payload is None:
                continue
            refs = store.snapshot_outputs(session.tenant_id, payload)
            for name, ref in refs.items():
                items.append(
                    {
                        "field": name,
                        "value_ref": ref,
                        **store.lineage(session.tenant_id, ref),
                    }
                )
        return json_response(200, {"run_id": state.run_id, "items": items})

    def _values_render(self, request, session, params) -> Response:
        """The deterministic renderer: the model shapes the sentence,
        the store supplies every value — a missing reference refuses,
        never fabricates."""
        from ..values import ValueError_, render_segments

        store = self._require_values()
        segments = (request.body or {}).get("segments")
        if not isinstance(segments, list) or not segments:
            raise GatewayError(
                400, "invalid_request", 'give the segments: {"segments": [...]}'
            )
        try:
            text = render_segments(
                segments, store=store, tenant=session.tenant_id
            )
        except ValueError_ as exc:
            raise GatewayError(422, "cannot_render", str(exc)) from exc
        return json_response(200, {"text": text})

    def _investor_metrics(self):
        """The metrics service over this host's REAL stores — readers are
        closures on what the gateway already holds; a store this host
        lacks simply leaves its metrics to the manual door."""
        if self._metrics_store is None:
            return None
        from datetime import UTC, datetime, timedelta

        from ..telemetry.investor import InvestorMetricsService

        def _runs():
            return [
                s
                for s in self._durable.runs.list(limit=10_000)
                if s.contract.metadata.get("tenant_id")
            ]

        def _active_since(days: int) -> set[str]:
            floor = datetime.now(UTC) - timedelta(days=days)
            return {
                s.contract.submitted_by
                for s in _runs()
                if s.updated_at >= floor and s.contract.submitted_by
            }

        def _avg_daily_minutes() -> float:
            floor = datetime.now(UTC) - timedelta(days=1)
            spans: dict[str, list] = {}
            for s in _runs():
                if s.updated_at < floor or not s.contract.submitted_by:
                    continue
                spans.setdefault(s.contract.submitted_by, []).extend(
                    (s.created_at, s.updated_at)
                )
            if not spans:
                return 0.0
            minutes = [
                (max(stamps) - min(stamps)).total_seconds() / 60
                for stamps in spans.values()
            ]
            return sum(minutes) / len(minutes)

        def _model_totals(field: str) -> float:
            usage = self._model_usage
            if usage is None:
                raise LookupError("no model usage books on this host")
            total = 0.0
            for tenant in usage.tenants():
                line = usage.all_time(tenant)
                total += (
                    line["prompt_tokens"] + line["completion_tokens"]
                    if field == "tokens"
                    else line[field]
                )
            return total

        def _capital() -> float:
            billing = self._billing
            if billing is None:
                raise LookupError("no earnings books on this host")
            micros = 0
            for principal in billing.principals():
                balance = billing.balance(principal)
                micros += (
                    balance.available_micros
                    + balance.pending_micros
                    + balance.reserved_micros
                )
            return micros / 1_000_000

        def _stickiness() -> float:
            monthly = len(_active_since(30))
            if not monthly:
                return 0.0
            return len(_active_since(1)) / monthly * 100

        def _terminal_today():
            done = [
                s
                for s in _runs()
                if s.updated_at.date() == today
                and s.phase.value in ("completed", "failed", "cancelled")
            ]
            return done

        def _success_rate() -> float:
            done = _terminal_today()
            if not done:
                raise LookupError("no terminal runs today — nothing to rate")
            wins = [s for s in done if s.phase.value == "completed"]
            return len(wins) / len(done) * 100

        def _first_attempt_rate() -> float:
            done = _terminal_today()
            if not done:
                raise LookupError("no terminal runs today — nothing to rate")
            first = [
                s
                for s in done
                if s.phase.value == "completed" and s.user_retries == 0
            ]
            return len(first) / len(done) * 100

        def _earnings_today() -> float:
            billing = self._billing
            if billing is None:
                raise LookupError("no earnings books on this host")
            micros = 0
            for principal in billing.principals():
                for entry in billing.entries(principal):
                    if entry.created_at.date() == today:
                        micros += entry.amount_micros
            return micros / 1_000_000

        def _model_month_cost() -> float:
            usage = self._model_usage
            if usage is None:
                raise LookupError("no model usage books on this host")
            return sum(usage.month_cost(t) for t in usage.tenants())

        def _day7_retention() -> float:
            floor = datetime.now(UTC) - timedelta(days=8)
            ceiling = datetime.now(UTC) - timedelta(days=7)
            cohort = {
                s.contract.submitted_by
                for s in _runs()
                if floor <= s.updated_at < ceiling and s.contract.submitted_by
            }
            if not cohort:
                raise LookupError("no activity 7 days ago — no cohort yet")
            kept = cohort & _active_since(1)
            return len(kept) / len(cohort) * 100

        def _request_success() -> float:
            requests = self._metrics.get("requests", 0)
            if not requests:
                raise LookupError("no requests since start")
            errors = self._metrics.get("errors", 0)
            return (requests - errors) / requests * 100

        # ---- phase 2 readers -------------------------------------------- #
        month = datetime.now(UTC).strftime("%Y-%m")

        def _earnings_month() -> float:
            billing = self._billing
            if billing is None:
                raise LookupError("no earnings books on this host")
            micros = 0
            for principal in billing.principals():
                for entry in billing.entries(principal):
                    if entry.created_at.strftime("%Y-%m") == month:
                        micros += entry.amount_micros
            return micros / 1_000_000

        def _completed_month() -> int:
            return len(
                [
                    s
                    for s in _runs()
                    if s.phase.value == "completed"
                    and s.updated_at.strftime("%Y-%m") == month
                ]
            )

        def _arpu() -> float:
            monthly = len(_active_since(30))
            if not monthly:
                raise LookupError("no monthly actives — no ARPU basis")
            return _earnings_month() / monthly

        def _cost_per_success() -> float:
            completed = _completed_month()
            if not completed:
                raise LookupError("no completed runs this month")
            return _model_month_cost() / completed

        def _contribution_margin() -> float:
            earnings = _earnings_month()
            if not earnings:
                raise LookupError("no earnings this month — no margin basis")
            return (earnings - _model_month_cost()) / earnings * 100

        def _ai_terminal_30d():
            floor = datetime.now(UTC) - timedelta(days=30)
            return [
                s
                for s in _runs()
                if s.updated_at >= floor
                and s.phase.value in ("completed", "failed", "cancelled")
            ]

        def _ai_task_success() -> float:
            done = [
                s
                for s in _ai_terminal_30d()
                if isinstance(
                    s.contract.metadata.get("node_function"), dict
                )
            ]
            if not done:
                raise LookupError("no node-function runs in 30 days")
            wins = [s for s in done if s.phase.value == "completed"]
            return len(wins) / len(done) * 100

        def _intervention_rate() -> float:
            done = _ai_terminal_30d()
            if not done:
                raise LookupError("no terminal runs in 30 days")
            touched = [s for s in done if s.user_retries > 0]
            return len(touched) / len(done) * 100

        def _repairs_total() -> float:
            return float(
                len(
                    [
                        record
                        for record in self._durable.audit.records()
                        if record.event_type == "model.seat"
                        and record.payload.get("purpose") == "node.repair"
                    ]
                )
            )

        def _todays_entries():
            billing = self._billing
            if billing is None:
                raise LookupError("no earnings books on this host")
            return [
                entry
                for principal in billing.principals()
                for entry in billing.entries(principal)
                if entry.created_at.date() == today
            ]

        def _avg_transaction() -> float:
            entries = _todays_entries()
            if not entries:
                raise LookupError("no transactions today")
            return sum(e.amount_micros for e in entries) / len(entries) / 1e6

        def _activation_rate() -> float:
            started: set[str] = set()
            completed: set[str] = set()
            for s in _runs():
                if not s.contract.submitted_by:
                    continue
                started.add(s.contract.submitted_by)
                if s.phase.value == "completed":
                    completed.add(s.contract.submitted_by)
            if not started:
                raise LookupError("no accounts have started a run yet")
            return len(completed & started) / len(started) * 100

        def _at_risk() -> float:
            earlier = _active_since(30) - _active_since(7)
            return float(len(earlier))

        # ---- phase 3: the moat, measured -------------------------------- #
        def _node_reuse_rate() -> float:
            done = _ai_terminal_30d()
            if not done:
                raise LookupError("no terminal runs in 30 days")
            reused = [
                s
                for s in done
                if isinstance(s.contract.metadata.get("node_function"), dict)
            ]
            return len(reused) / len(done) * 100

        def _sealed_releases() -> float:
            if self._provenance is None:
                raise LookupError("no provenance ledger on this host")
            return float(self._provenance.count_releases())

        today = datetime.now(UTC).date()
        readers = {
            "users.daily_active": lambda: len(_active_since(1)),
            "users.weekly_active": lambda: len(_active_since(7)),
            "users.monthly_active": lambda: len(_active_since(30)),
            "users.stickiness_dau_mau": _stickiness,
            "engagement.avg_daily_minutes": _avg_daily_minutes,
            "executions.total": lambda: len(_runs()),
            "executions.daily": lambda: len(
                [s for s in _runs() if s.created_at.date() == today]
            ),
            "workflows.completed_daily": lambda: len(
                [
                    s
                    for s in _terminal_today()
                    if s.phase.value == "completed"
                ]
            ),
            "workflows.success_rate": _success_rate,
            "workflows.first_attempt_success_rate": _first_attempt_rate,
            "revenue.earnings_daily_usd": _earnings_today,
            "cost.model_month_usd": _model_month_cost,
            "retention.day7_pct": _day7_retention,
            "reliability.request_success_pct": _request_success,
            "model.tokens_total": lambda: _model_totals("tokens"),
            "model.calls_total": lambda: _model_totals("calls"),
            "model.spend_usd": lambda: _model_totals("cost_usd"),
            "capital.in_app_usd": _capital,
            # ---- phase 2 -------------------------------------------- #
            "unit.arpu_usd": _arpu,
            "unit.cost_per_successful_workflow_usd": _cost_per_success,
            "unit.contribution_margin_pct": _contribution_margin,
            "ai.task_success_rate": _ai_task_success,
            "ai.intervention_rate": _intervention_rate,
            "ai.repairs_total": _repairs_total,
            "market.transactions_daily": lambda: float(
                len(_todays_entries())
            ),
            "market.avg_transaction_usd": _avg_transaction,
            "health.activation_rate_pct": _activation_rate,
            "health.at_risk_users": _at_risk,
            "moat.node_reuse_rate_pct": _node_reuse_rate,
            "moat.reusable_verified_nodes": _sealed_releases,
            "moat.proprietary_events_total": lambda: float(
                self._durable.audit.count()
            ),
        }
        if self._nodeplace is not None:
            readers["nodes.total"] = lambda: len(self._nodeplace.all_nodes())
            readers["market.listings_active"] = lambda: float(
                len(self._nodeplace.discover(""))
            )
        return InvestorMetricsService(self._metrics_store, readers=readers)

    def _require_metrics(self):
        service = self._investor_metrics()
        if service is None:
            raise GatewayError(
                404, "not_found", "the metrics tracker is not enabled here"
            )
        return service

    def _metrics_view(self, request, session, params) -> Response:
        return json_response(200, self._require_metrics().view())

    def _metrics_summary(self, request, session, params) -> Response:
        """The executive strip: each headline metric with the matrix's
        status components — actual, previous period, growth, target,
        threshold status, owner."""
        return json_response(200, self._require_metrics().summary())

    def _metrics_scorecard(self, request, session, params) -> Response:
        """The weighted composite, pillars renormalized over what this
        platform can actually measure — excluded pillars are named."""
        return json_response(200, self._require_metrics().scorecard())

    def _competitor_ledger(self):
        from ..telemetry.investor import CompetitorLedger

        if self._competitors is None:
            self._competitors = CompetitorLedger(self._durable.conn)
        return self._competitors

    def _competitors_view(self, request, session, params) -> Response:
        """The strategic comparison: per competitor, per matrix
        dimension — the newest relative score with evidence, confidence,
        and last-updated. Unobserved dimensions are absent, never
        guessed."""
        self._require_metrics()
        return json_response(200, self._competitor_ledger().comparison())

    def _competitors_record(self, request, session, params) -> Response:
        """The observation door: approved and audited like the manual
        metric door — competitor intelligence is external eyes, so every
        entry names its evidence, source, and confidence."""
        self._require_metrics()
        if self._approval is None:
            raise GatewayError(
                404, "not_found", "approval authority is not configured"
            )
        try:
            self._approval.approve(
                session,
                run_id="competitors:observe",
                policy="metrics.record",
                requester_id="",
                now=request.now or self._clock(),
            )
        except AuthorizationError as exc:
            raise GatewayError(403, "forbidden", str(exc)) from exc
        body = request.body or {}
        try:
            observed = self._competitor_ledger().observe(
                str(body.get("competitor", "")),
                str(body.get("dimension", "")),
                float(body.get("score")),
                evidence=str(body.get("evidence", "") or ""),
                source=str(body.get("source", "") or ""),
                confidence=str(body.get("confidence", "medium") or "medium"),
            )
        except (TypeError, ValueError) as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        self._durable.audit.append(
            "competitors.observed",
            {
                "run_id": "competitors:observe",
                "by": session.principal_id,
                **observed,
            },
        )
        return json_response(200, observed)

    def _metrics_scenario(self, request, session, params) -> Response:
        """Decision support, deterministically: the matrix's what-if
        outputs computed from CURRENT actuals (the ledgers' own numbers)
        and the operator's stated assumptions — no model ever touches a
        number. The baseline is named in the answer, approximations
        included."""
        from ..telemetry.investor import project_scenario

        service = self._require_metrics()
        service.collect()
        latest = self._metrics_store.latest()

        def _value(key: str) -> float | None:
            point = latest.get(key)
            return float(point["value"]) if point else None

        arpu = _value("unit.arpu_usd")
        mau = _value("users.monthly_active")
        daily = _value("revenue.earnings_daily_usd")
        monthly_revenue = (
            arpu * mau
            if arpu is not None and mau is not None
            else (daily or 0.0) * 30
        )
        baseline = {
            "monthly_revenue_usd": monthly_revenue,
            "monthly_cost_usd": _value("cost.model_month_usd") or 0.0,
            "cash_usd": _value("capital.in_app_usd") or 0.0,
        }
        body = request.body or {}
        try:
            projected = project_scenario(
                scenario=str(body.get("scenario", "")),
                baseline=baseline,
                assumptions=dict(body.get("assumptions") or {}),
            )
        except (TypeError, ValueError) as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        return json_response(200, projected)

    def _metrics_report(self, request, session, params) -> Response:
        """The automated investor report: one Markdown document off the
        ledgers — executive summary, scorecard, cohorts, competitors —
        every number the runtime's own, none written by a model."""
        service = self._require_metrics()
        summary = service.summary()
        scorecard = service.scorecard()
        cohorts = self._metrics_cohorts(request, session, params).body
        competitors = self._competitor_ledger().comparison()
        now = (request.now or self._clock()).strftime("%Y-%m-%d")
        lines = [
            f"# OoLu — Investor Report ({now})",
            "",
            "## Executive summary",
            "",
            "| Metric | Actual | Prev | Δ% | Target | Status |",
            "| --- | ---: | ---: | ---: | ---: | --- |",
        ]

        def _num(value) -> str:
            if value is None:
                return "—"
            return f"{value:,.2f}" if value % 1 else f"{int(value):,}"

        for item in summary["items"]:
            growth = item["growth_rate_pct"]
            lines.append(
                f"| {item['label']} | {_num(item['actual'])} | "
                f"{_num(item['previous_period'])} | "
                f"{_num(growth) if growth is not None else '—'} | "
                f"{_num(item['target'])} | {item['status']} |"
            )
        lines += ["", "## Scorecard", ""]
        if scorecard["score"] is not None:
            lines.append(f"**{scorecard['score']} / 100**")
            lines.append("")
            for pillar in scorecard["pillars"]:
                lines.append(
                    f"- {pillar['name'].replace('_', ' ')}: "
                    f"{pillar['score']:.0f} "
                    f"(weight {pillar['effective_weight'] * 100:.0f}%)"
                )
            if scorecard["excluded"]:
                lines.append(
                    "- not yet measurable: "
                    + ", ".join(scorecard["excluded"]).replace("_", " ")
                )
        else:
            lines.append("No scoreable data yet.")
        lines += ["", "## Cohort retention", ""]
        for cohort in cohorts["items"][-6:]:
            points = ", ".join(
                f"M{p['offset']} {p['pct']}%" for p in cohort["retention"][:6]
            )
            lines.append(
                f"- {cohort['cohort']} (n={cohort['size']}): {points}"
            )
        if not cohorts["items"]:
            lines.append("No cohorts yet.")
        lines += ["", "## Competitive position", ""]
        for entry in competitors["items"]:
            lines.append(f"### vs {entry['competitor']}")
            for dim, obs in entry["dimensions"].items():
                lead = "we lead" if obs["relative_score"] > 0 else (
                    "they lead" if obs["relative_score"] < 0 else "even"
                )
                lines.append(
                    f"- {dim.replace('_', ' ')}: {lead} "
                    f"({obs['relative_score']:+.1f}, {obs['confidence']} "
                    f"confidence) — {obs['evidence'] or 'no evidence noted'}"
                )
        if not competitors["items"]:
            lines.append("No competitor observations recorded.")
        return json_response(
            200, {"generated_at": now, "markdown": "\n".join(lines)}
        )

    def _metrics_cohorts(self, request, session, params) -> Response:
        """Signup-month cohorts off the run books: each account joins
        the cohort of its FIRST activity month, and every cohort shows
        how many members were active in each month since — the matrix's
        cohort analysis, computed from real stamps, never sampled."""
        self._require_metrics()  # the same wall and enablement check
        from datetime import UTC as _UTC
        from datetime import datetime as _dt

        from ..telemetry.investor import month_span

        first_seen: dict[str, str] = {}
        active_months: dict[str, set[str]] = {}
        for state in self._durable.runs.list(limit=10_000):
            who = state.contract.submitted_by
            if not who or not state.contract.metadata.get("tenant_id"):
                continue
            born = state.created_at.strftime("%Y-%m")
            moved = state.updated_at.strftime("%Y-%m")
            active_months.setdefault(who, set()).update({born, moved})
            if who not in first_seen or born < first_seen[who]:
                first_seen[who] = born
        cohorts: dict[str, list[str]] = {}
        for who, born in first_seen.items():
            cohorts.setdefault(born, []).append(who)
        now_month = _dt.now(_UTC).strftime("%Y-%m")
        items = []
        for born in sorted(cohorts)[-12:]:
            members = cohorts[born]
            retention = []
            for offset, month in enumerate(month_span(born, now_month)):
                active = sum(
                    1 for who in members if month in active_months[who]
                )
                retention.append(
                    {
                        "month": month,
                        "offset": offset,
                        "active": active,
                        "pct": round(active / len(members) * 100, 1),
                    }
                )
            items.append(
                {"cohort": born, "size": len(members), "retention": retention}
            )
        return json_response(200, {"items": items})

    def _metrics_history(self, request, session, params) -> Response:
        self._require_metrics()
        days = max(1, min(3650, int(request.query.get("days", "90"))))
        return json_response(
            200, {"series": self._metrics_store.history(days=days)}
        )

    def _metrics_snapshot(self, request, session, params) -> Response:
        """The daily tick: collect and file every auto metric — the call
        a Routine (or the panel itself) makes to keep the series alive."""
        collected = self._require_metrics().collect()
        return json_response(200, {"collected": collected})

    def _metrics_record(self, request, session, params) -> Response:
        """The manual door: an approved, audited recording for sources
        the app cannot see — commits, SEO, capital raises."""
        service = self._require_metrics()
        if self._approval is None:
            raise GatewayError(404, "not_found", "approval authority is not configured")
        try:
            self._approval.approve(
                session,
                run_id=f"metrics:{params['key']}",
                policy="metrics.record",
                requester_id="",
                now=request.now or self._clock(),
            )
        except AuthorizationError as exc:
            raise GatewayError(403, "forbidden", str(exc)) from exc
        body = request.body or {}
        try:
            value = float(body["value"])
        except (KeyError, TypeError, ValueError) as exc:
            raise GatewayError(
                400, "invalid_request", 'give the number: {"value": …}'
            ) from exc
        try:
            spec = service.record_manual(params["key"], value)
        except KeyError as exc:
            raise GatewayError(404, "not_found", str(exc)) from exc
        self._durable.audit.append(
            "metrics.recorded",
            {
                "run_id": f"metrics:{spec.key}",
                "metric": spec.key,
                "value": value,
                "by": session.principal_id,
            },
        )
        return json_response(200, {"key": spec.key, "value": value})

    def _platform_finance(self, request, session, params) -> Response:
        """The operator's two-sided ledger, straight off the books: what
        every account has DRAWN from the platform's model keys (per
        tenant — that is where usage is booked) and what every noder has
        EARNED from node execution (per principal). No projections, no
        estimates — the same stores the meters write."""
        if self._model_usage is None and self._billing is None:
            raise GatewayError(404, "not_found", "finance books are not enabled here")
        accounts: list[dict] = []
        if self._model_usage is not None:
            for tenant in self._model_usage.tenants():
                entry: dict = {
                    "tenant_id": tenant,
                    # The whole ledger line (all months, all sources) and
                    # this month's per-source rows.
                    "all_time": self._model_usage.all_time(tenant),
                    "month": self._model_usage.view(tenant),
                    # WHO drew it: every user's independent gauge under
                    # the shared tenant line — the same consultations,
                    # keyed by the acting principal at booking time.
                    "users": self._model_usage.users(tenant),
                }
                if self._subscription is not None:
                    allowance = self._subscription.allowance_for(tenant)
                    spent = self._subscription.spend_for(tenant)
                    entry["subscription"] = {
                        "allowance_usd": allowance,
                        "spent_usd": spent,
                        "remaining_usd": max(0.0, allowance - spent),
                        "trial": bool(self._subscription.is_trial(tenant)),
                    }
                accounts.append(entry)
        noders: list[dict] = []
        if self._billing is not None:
            for principal in self._billing.principals():
                balance = self._billing.balance(principal).model_dump(mode="json")
                noders.append({"principal": principal, **balance})
        return json_response(200, {"accounts": accounts, "noders": noders})

    def _usage_giveback(self, request, session, params) -> Response:
        """The give-back: erase the booked model spend of all or selected
        accounts, restoring their allowance — the experiment-cohort
        refill. An approved, audited platform move: the amounts forgiven
        are named on the audit log, never silently zeroed."""
        if self._model_usage is None:
            raise GatewayError(404, "not_found", "model usage is not tracked here")
        if self._approval is None:
            raise GatewayError(404, "not_found", "approval authority is not configured")
        try:
            self._approval.approve(
                session,
                run_id="usage:giveback",
                policy="usage.giveback",
                requester_id="",
                now=request.now or self._clock(),
            )
        except AuthorizationError as exc:
            raise GatewayError(403, "forbidden", str(exc)) from exc
        body = request.body or {}
        # Selected USERS on a shared tenant: erase exactly what each one
        # drew (their own line), refilling the shared quota by that
        # amount — everyone else's gauges stand untouched.
        users = [
            {
                "tenant": str(u.get("tenant") or u.get("tenant_id") or "").strip(),
                "account": str(u.get("account") or "").strip(),
            }
            for u in (body.get("users") or [])
            if isinstance(u, dict)
        ]
        users = [u for u in users if u["tenant"] and u["account"]]
        if body.get("all"):
            tenants = self._model_usage.tenants()
        else:
            tenants = [
                str(t).strip() for t in (body.get("tenants") or []) if str(t).strip()
            ]
        if not tenants and not users:
            raise GatewayError(
                400,
                "invalid_request",
                'name the accounts to refill ("tenants": [...] and/or'
                ' "users": [{"tenant": ..., "account": ...}]) or pass'
                ' "all": true',
            )
        given_back: dict[str, float] = {
            tenant: self._model_usage.reset(tenant) for tenant in tenants
        }
        for user in users:
            if user["tenant"] in tenants:
                continue  # the whole tenant already reset — nothing left
            given_back[f"{user['tenant']}:{user['account']}"] = (
                self._model_usage.reset_user(user["tenant"], user["account"])
            )
        self._durable.audit.append(
            "usage.giveback",
            {
                "run_id": "usage:giveback",
                "by": session.principal_id,
                "given_back_usd": given_back,
            },
        )
        return json_response(200, {"given_back_usd": given_back})

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
        now = request.now or self._clock()
        # Forgot-password promotion: the e-mailed password was STAGED, not
        # set — using it is what makes it real (and what proves control of
        # the inbox, so the address counts as verified from here on).
        if self._pending_passwords.take(username, password, now=now):
            accounts.change_password(username, password)
            if self._mail_codes is not None and self._identity_links is not None:
                email = self._identity_links.email_of(username)
                if email:
                    self._mail_codes.mark_verified(email, "verify")
        try:
            result = accounts.login(username, password, now=now)
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
        # The owner is in with their CURRENT password: any staged key —
        # theirs or a stranger's — is dead weight now, and clearing it
        # closes the window a mailed password would otherwise hold open.
        self._pending_passwords.clear(username)
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
                # Continue with phone: offered only when this host can
                # actually text (an SMS sender + the code store).
                "phone": bool(
                    self._sms is not None
                    and self._mail_codes is not None
                    and self._accounts is not None
                ),
            },
        )

    # ------------------------------------------------------------------ #
    # Continue with phone: an SMS code is the key.                        #
    # ------------------------------------------------------------------ #
    # A phone-created account lives in its own username namespace so a
    # manual registration can never squat the name a number would get.
    _PHONE_USERNAME_PREFIX = "phone-"

    def _require_phone_door(self):
        accounts = self._require_accounts()
        if self._sms is None or self._mail_codes is None:
            raise GatewayError(
                404, "not_found", "phone sign-in is not offered on this host"
            )
        return accounts

    def _phone_start(self, request, session, params) -> Response:
        """Text a one-time code to the number — the same hashed, expiring,
        attempt-limited store the mail door uses. The answer never says
        whether the number has an account (no enumeration) — and it stays
        identical when the throttle skips a send: every text costs the
        host real provider money, so one number cannot be a billing lever
        (the code from moments ago still works anyway)."""
        self._require_phone_door()
        from ..sms import normalize_phone

        try:
            phone = normalize_phone((request.body or {}).get("phone"))
        except ValueError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        if self._send_throttle.allow(
            phone,
            "phone-code",
            cooldown_s=60,
            per_day=10,
            now=request.now or self._clock(),
        ):
            code = self._mail_codes.issue(phone, "phone")
            self._sms.send(
                to=phone,
                body=f"Your OoLu sign-in code is {code}. It expires in 30 "
                "minutes. If you didn't request it, ignore this text.",
            )
        return json_response(200, {"sent": True})

    def _phone_verify(self, request, session, params) -> Response:
        """The code comes back: sign in — or create the account when the
        number is new. A fresh account is born WITH a usable password,
        auto-generated and texted to the number (changeable in Settings),
        so username+password works from day one."""
        accounts = self._require_phone_door()
        from ..sms import normalize_phone

        body = request.body or {}
        try:
            phone = normalize_phone(body.get("phone"))
        except ValueError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        code = str(body.get("code", "")).strip()
        if not code:
            raise GatewayError(400, "invalid_request", "the texted code is required")
        if not self._mail_codes.redeem(phone, "phone", code):
            raise GatewayError(
                401, "unauthorized", "that code is wrong or expired — start again"
            )
        now = request.now or self._clock()
        existing = (
            self._identity_links.lookup("phone", phone)
            if self._identity_links is not None
            else None
        )
        created = False
        if existing is not None:
            username = existing["username"]
        else:
            if self._identity_links is None:
                raise GatewayError(
                    404, "not_found", "phone accounts need the identity-link store"
                )
            username = self._fresh_phone_username(phone, accounts)
            password = secrets.token_urlsafe(9)
            try:
                accounts.create_user(
                    username,
                    password,
                    tenant=self._config.registration_tenant,
                    granted_by="phone-signin",
                )
            except ValueError as exc:
                raise GatewayError(400, "invalid_request", str(exc)) from exc
            self._identity_links.link(
                provider="phone", subject=phone, tenant=self._config.registration_tenant,
                username=username, email="", at=now,
            )
            # The account is born with a REAL password, told to its owner
            # — never an unknowable secret that forces a settings dance.
            self._sms.send(
                to=phone,
                body=f"Welcome to OoLu! Your account is {username} and "
                f"your password is {password} — change it in Settings "
                "whenever you like.",
            )
            self._metrics["registrations"] += 1
            created = True
        try:
            result = accounts.external_login(username, method="phone", now=now)
        except AuthenticationError as exc:
            raise GatewayError(401, "unauthorized", str(exc)) from exc
        return json_response(
            200,
            {
                "token": result.token,
                "expires_at": result.expires_at.isoformat(),
                "tenant": result.tenant_id,
                "principal": result.principal,
                "created": created,
            },
        )

    def _fresh_phone_username(self, phone: str, accounts) -> str:
        """A username from the RESERVED phone namespace: phone-<last4>,
        suffixed until free. Manual registration can never mint names
        here (see _fresh_username), so the number's name is never taken."""
        base = f"{self._PHONE_USERNAME_PREFIX}{phone[-4:]}"
        candidate = base
        for suffix in range(2, 10_000):
            if accounts.user(candidate) is None:
                return candidate
            candidate = f"{base}-{suffix}"
        raise GatewayError(500, "internal", "could not derive a free username")

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

    @classmethod
    def _fresh_username(cls, email: str, accounts) -> str:
        base = username_from_email(email)
        # The account-creation rule: names auto-created sign-ins mint
        # (phone-…) are a RESERVED namespace — a manual registration can
        # never take the name a phone number would get, so "continue
        # with phone" never finds its account squatted.
        if base.startswith(cls._PHONE_USERNAME_PREFIX):
            base = f"u-{base}"
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
        """Start a password reset. Always 202 — an unknown address, a
        throttled one, and a fresh send all look identical, so nothing
        enumerates accounts (and nobody's inbox becomes a target)."""
        if self._mail is None or self._mail_codes is None:
            raise GatewayError(404, "not_found", "password reset is not enabled")
        body = request.body or {}
        email = str(body.get("email", "")).strip().lower()
        link = (
            self._identity_links.lookup("email", email)
            if self._identity_links is not None and _EMAIL_RE.match(email)
            else None
        )
        if link is not None and self._send_throttle.allow(
            email,
            "reset-code",
            cooldown_s=60,
            per_day=10,
            now=request.now or self._clock(),
        ):
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

    def _reset_email_password(self, request, session, params) -> Response:
        """Forgot password, the one-step way: the server GENERATES a new
        password and e-mails it — the user signs in with it and changes it
        in Settings. No code to type back.

        Hardened on two axes. The mailed password is STAGED, never set:
        the current password keeps working untouched until the new one is
        actually used (its first sign-in promotes it and proves inbox
        control), so a stranger who knows the address can lock nobody out
        — and the mail can honestly say "if you didn't ask, nothing has
        changed". And the door is paced per address (cooldown + daily cap)
        so it cannot be turned into a mail cannon.

        Always 202: an unknown address, a throttled one, and a fresh send
        all answer identically, so nothing enumerates accounts."""
        self._require_accounts()
        if self._mail is None or self._mail_codes is None:
            raise GatewayError(404, "not_found", "password reset is not enabled")
        body = request.body or {}
        email = str(body.get("email", "")).strip().lower()
        now = request.now or self._clock()
        link = (
            self._identity_links.lookup("email", email)
            if self._identity_links is not None and _EMAIL_RE.match(email)
            else None
        )
        if link is not None and self._send_throttle.allow(
            email, "reset-password", cooldown_s=600, per_day=5, now=now
        ):
            password = secrets.token_urlsafe(9)
            self._pending_passwords.stage(link["username"], password, now=now)
            self._mail.send(
                to=email,
                subject="Your new OoLu password",
                body=(
                    f"A new password for your OoLu account "
                    f"{link['username']}: {password}\n\n"
                    "It works for the next 30 minutes. Your current "
                    "password keeps working until you sign in with this "
                    "new one — so if you didn't ask for this, just ignore "
                    "it: nothing has changed. After signing in, change it "
                    "in Settings whenever you like."
                ),
            )
            self._metrics["password_resets"] += 1
        return json_response(202, {"status": "sent"})

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
            # This window was opened by the app (window.open), so it may
            # close itself; the app is already polling finish() and will
            # complete sign-in on its own channel. A brief message shows
            # first in case the browser blocks the auto-close.
            page = (
                "<!doctype html><meta charset='utf-8'><title>OoLu</title>"
                "<body style='font-family:system-ui;margin:3rem'>"
                f"<h2>Signed in as {_escape(principal)}.</h2>"
                "<p>Returning you to OoLu — you can close this window.</p>"
                "<script>setTimeout(function(){window.close();},600);</script>"
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

    def _auth_set_password(self, request, session, params) -> Response:
        """The signed-in account sets its own sign-in password.

        This is what makes a Google-created account a real username +
        password login: Google minted the account with an unknowable
        random password, so the user could never type their way in. Here
        they choose one, and next time either door works."""
        accounts = self._require_accounts()
        password = str((request.body or {}).get("password", ""))
        if len(password) < 8:
            raise GatewayError(
                400, "invalid_request", "passwords need at least 8 characters"
            )
        if not accounts.change_password(session.principal_id, password):
            raise GatewayError(404, "not_found", "no such account")
        return json_response(200, {"username": session.principal_id, "ok": True})

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
            state = self._durable.resume(run_id, resume)
        except OrchestratorError as exc:
            raise GatewayError(409, "conflict", str(exc)) from exc
        # A resumed run (e.g. the human confirmed model-written code) may
        # complete HERE — the verification evidence must not depend on
        # which door the run finished behind.
        self._record_function_verification(state)
        return state

    def _persist_rebuilt_route(self, state: RunState) -> None:
        """Self-built code the user's credit paid for becomes a REAL node.

        A COMPLETED run whose route the model rebuilt
        (``origin="llm_rebuild"``) has proven its script end to end —
        burying that code in one run's log would waste the build the user
        paid for. It is contributed as a function node and given a desk
        account, so it lands in Work → My nodes (not only the run list),
        and the next run of this goal routes straight through it instead
        of rebuilding. One node per goal — the same skill-id dedupe as
        every build. Refusals are silent: the run already succeeded, and
        persistence is a bonus, never a step of it."""
        if self._nodeplace is None or self._desk is None:
            return
        if state.phase is not Phase.COMPLETED:
            return
        route = state.route
        if route is None or route.chosen.origin != "llm_rebuild":
            return
        action = next(
            (
                item.action
                for item in route.chosen.actions
                if item.action.adapter == "script"
            ),
            None,
        )
        script = (action.parameters or {}).get("script") if action else None
        if not script:
            return
        tenant = str((state.contract.metadata or {}).get("tenant_id", ""))
        principal = state.contract.submitted_by
        intent = (state.contract.intent or "").strip()
        if not tenant or not principal or not intent:
            return
        skill_id = self._function_skill_id(tenant, intent)
        try:
            nodes = self._nodeplace.list_own_nodes(
                noder_principal=principal, tenant_id=tenant
            )
            if any(n.skill_id == skill_id for n in nodes):
                return  # a node already answers for this goal
            name = concise_name(intent)
            skill = ReusableSkill.model_validate(
                {
                    "id": skill_id,
                    "name": name,
                    "description": intent,
                    "signature": {
                        "application": "script",
                        "adapter": "script",
                    },
                    "parameters": [],
                    "actions": [
                        {
                            "correlation_id": "function",
                            "adapter": "script",
                            "operation": "run",
                            "parameters": {
                                "goal": intent,
                                "script": str(script),
                                "node_key": f"node:{skill_id}",
                            },
                        }
                    ],
                }
            )
            result = self._nodeplace.contribute(
                noder_principal=principal,
                tenant_id=tenant,
                skill=skill,
                semver="1.0.0",
                title=name,
                summary=intent,
                produces=[
                    Slot(name="result", value_type="str", role="result")
                ],
            )
            self._desk.create_account(
                result.node.node_id,
                principal=principal,
                tenant=tenant,
                policy_version=NODE_POLICY_VERSION,
            )
            self._durable.audit.append(
                "node.rebuild_persisted",
                {
                    "run_id": state.run_id,
                    "node_id": result.node.node_id,
                    "skill_id": skill_id,
                },
            )
        except Exception:  # noqa: BLE001 — a bonus on a succeeded run,
            # never a new way for it to fail.
            return

    def _record_function_verification(self, state: RunState) -> None:
        """A TERMINAL run through a node's own function IS evidence.

        The engine executed the node's stored code end to end — sandboxed,
        audited, through the same pipeline as any run — so the node earns
        real evidence from local use, both ways: a COMPLETED run records a
        verified success (and the account's one honest promotion,
        needs_verification -> live — the door out of 'stuck at
        needs-verification forever'); a FAILED run records a verified
        FAILURE, so a node's health can dip from local use, not only
        climb. One event per run (idempotent on the run id), terminal
        phases only — a paused run is not evidence yet, and a retry that
        lands here again cannot double-record.

        The event carries NO consumer principal (the deriver's no-binding
        shape): a self-run proves the function works — or doesn't — but
        it must never unlock rating your own node."""
        # Same hook, sibling concern: a completed REBUILT route persists
        # as a real node on the desk before any evidence bookkeeping.
        self._persist_rebuilt_route(state)
        # And a run that healed its own function promotes the healed code
        # into the drawer — the node.repair seat's write, after the run.
        self._promote_repaired_function(state)
        # And the run's real outputs are FILED: immutable values, the
        # node's port index, and the input→output lineage.
        self._file_run_values(state)
        if self._metering is None or self._nodeplace is None:
            return
        if state.phase is Phase.COMPLETED:
            outcome = "succeeded"
        elif state.phase is Phase.FAILED:
            outcome = "failed"
        else:
            return
        function = (state.contract.metadata or {}).get("node_function")
        if not isinstance(function, dict) or not function.get("node_id"):
            return
        node_id = str(function["node_id"])
        version = self._nodeplace.latest_version(node_id)
        if version is None:
            return
        records = self._durable.audit.records(run_id=state.run_id)
        last = records[-1] if records else None
        recorded = self._metering.record(
            MeteringEvent(
                idempotency_key=f"node-verify:{state.run_id}",
                run_id=state.run_id,
                version_id=version.version_id,
                outcome=outcome,
                audit_seq=last.seq if last else 0,
                occurred_at=last.at if last else datetime.now(UTC),
            )
        )
        # Only a SUCCESS promotes: a failed run never verifies a node,
        # and error/restricted states are never healed here either way.
        if recorded and outcome == "succeeded" and self._desk is not None:
            self._desk.mark_verified(node_id)
        # A verified run SEALS the exact tree it executed as a release —
        # content-addressed and idempotent (the same tree is the same
        # release; a revoked release stays revoked through a re-seal).
        # Editing the drawer afterwards never edits the release: it
        # starts a new draft the next verified run can seal.
        if outcome == "succeeded" and self._provenance is not None:
            try:
                tenant = str(state.contract.metadata.get("tenant_id", ""))
                tree = self._node_src_tree(tenant, node_id)
                head = None
                if tree:
                    head = self._provenance.commit(
                        tenant,
                        node_id,
                        tree,
                        kind="snapshot",
                        instruction=f"tree verified by run {state.run_id}",
                        by=state.contract.submitted_by or "",
                    )
                release = self._provenance.seal(
                    tenant,
                    node_id,
                    tree=tree or None,
                    commit_id=head.commit_id if head is not None else "",
                    semver=version.semver,
                    verified_by_run=state.run_id,
                )
                self._durable.audit.append(
                    "node.release_sealed",
                    {
                        "node_id": node_id,
                        "release_id": release.release_id,
                        "tree_hash": release.tree_hash,
                        "run_id": state.run_id,
                        "semver": version.semver,
                    },
                )
            except Exception:  # noqa: BLE001 — sealing is bookkeeping on
                # a verified run; the verification itself already stands.
                logging.getLogger("oolu.gateway").warning(
                    "release sealing failed for %s", node_id, exc_info=True
                )

    def _file_run_values(self, state: RunState) -> None:
        """A COMPLETED node-function run's outputs, filed where the typed
        workflow model wants them: each payload field an immutable exact
        value, the node's PORT INDEX pointed at the fresh refs — so an
        ``output://{node_id}/{port}`` edge in any later binding resolves
        to THIS answer — and the lineage from the run's resolved input
        references recorded next to them. Content-addressed puts and
        insert-or-ignore lineage make a retry file the same rows once.
        Best-effort: a bonus on a succeeded run, never a new way for it
        to fail."""
        if self._values is None or state.phase is not Phase.COMPLETED:
            return
        function = (state.contract.metadata or {}).get("node_function")
        if not isinstance(function, dict) or not function.get("node_id"):
            return
        execution = state.execution
        if execution is None:
            return
        tenant = str(state.contract.metadata.get("tenant_id", ""))
        node_id = str(function["node_id"])
        try:
            for outcome in execution.action_outcomes:
                if outcome.status is not ExecutionStatus.SUCCEEDED:
                    continue
                evidence = outcome.evidence or {}
                payload = evidence.get("result")
                if payload is None:
                    continue
                refs = self._values.snapshot_outputs(
                    tenant, payload, label=node_id, producer=node_id
                )
                inputs = [
                    str(line.get("value_ref"))
                    for line in evidence.get("value_provenance") or []
                    if line.get("value_ref")
                ]
                if inputs and refs:
                    self._values.record_lineage(
                        tenant, node_id, inputs, list(refs.values())
                    )
        except Exception:  # noqa: BLE001 — filing is bookkeeping on a
            # finished run; the answer stands either way.
            logging.getLogger("oolu.gateway").warning(
                "value filing failed for run %s", state.run_id, exc_info=True
            )

    def _promote_repaired_function(self, state: RunState) -> None:
        """A COMPLETED run that healed its own function writes the healed
        code home: ``src/main.py`` in the node's drawer, through the
        ``node.repair`` seat — scope-checked and audited like every seated
        model act.

        This is the promotion `docs/model-seats.md` reserved: the RUN
        never mutates files mid-flight (the repair loop verifies and
        caches only); the gateway performs the explicit act afterwards,
        exactly once per run (idempotent on the audit log), and only for
        the node-function action itself — never for some other script a
        route happened to carry. From the next run on, the drawer copy —
        now the healed code — is what resolves, and its cache entry is
        already warm."""
        if self._files is None or state.phase is not Phase.COMPLETED:
            return
        function = (state.contract.metadata or {}).get("node_function")
        if not isinstance(function, dict) or not function.get("node_id"):
            return
        execution = state.execution
        if execution is None:
            return
        repaired: str | None = None
        for outcome in execution.action_outcomes:
            if outcome.status is not ExecutionStatus.SUCCEEDED:
                continue
            if outcome.skill_id != str(function.get("skill_id") or ""):
                continue  # only the node's OWN function promotes its drawer
            script = (outcome.evidence or {}).get("repaired_script")
            if script:
                repaired = str(script)
        if not repaired:
            return
        # Exactly once per run: a resume or retry that lands here again
        # finds the act already on the log and leaves it there.
        for record in self._durable.audit.records(run_id=state.run_id):
            if (
                record.event_type == "model.seat"
                and record.payload.get("purpose") == "node.repair"
            ):
                return
        tenant = str(state.contract.metadata.get("tenant_id", ""))
        node_id = str(function["node_id"])
        try:
            desk_files = DeskFiles(
                self._files,
                tenant=tenant,
                node_id=node_id,
                seat=SEATS["node.repair"],
            )
            desk_files.write("src/main.py", repaired)
        except SeatViolation:  # a seat refusal never breaks the run's answer
            logging.getLogger("oolu.gateway").warning(
                "repair promotion refused by the seat", exc_info=True
            )
            return
        self._durable.audit.append(
            "model.seat",
            {
                "purpose": "node.repair",
                "tenant": tenant,
                "by": state.contract.submitted_by,
                "node_id": node_id,
                "run_id": state.run_id,
                "written": desk_files.written,
            },
        )
        # The healed code is a commit like any other write — the failing
        # parent stays on the chain as the evidence it healed FROM.
        self._file_node_commit(
            tenant,
            node_id,
            kind="repair",
            instruction=f"run {state.run_id} repaired the function",
            by=state.contract.submitted_by or "",
        )

    def _run_dict(self, state: RunState) -> dict:
        return {
            "run_id": state.run_id,
            "intent": state.intent,
            "updated_at": state.updated_at.isoformat(),
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
            self._settings.effective(
                tenant, state.contract.submitted_by or None
            ).get(AUTOBUILD_CONSENT_KEY, False)
        )
        return {
            "consent": consent,
            "hint": None if consent else AUTOBUILD_HINT,
        }


def _hidden_now(hidden_at: str | None, last_at: str) -> bool:
    """Whether a thread is hidden AS IT STANDS: a hide stamps a moment,
    and only words spoken AFTER that moment bring the thread back. ISO
    timestamps in one format compare lexicographically."""
    if not hidden_at:
        return False
    return not last_at or last_at <= str(hidden_at)
