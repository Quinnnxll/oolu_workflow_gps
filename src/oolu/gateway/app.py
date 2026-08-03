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
from ..billing.doubleentry import DoubleEntryLedger
from ..billing.escrow import EscrowPolicy
from ..billing.launch import LaunchGuard
from ..billing.psp import FakePsp
from ..billing.subscription import SubscriptionError, SubscriptionService
from ..billing.tax import InvoiceBook, JurisdictionModule, TaxRegistry
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
    repair_node_function,
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
from ..marketplace import (
    AgentDelegation,
    CatalogService,
    FederationDesk,
    InventoryService,
    JobDesk,
    MarketNotFound,
    MarketplaceError,
    MarketplaceSpine,
    OrderHistory,
    OrderService,
    PayoutChangeDesk,
    ProtocolViolation,
    PurchasePolicy,
    ReconciliationDesk,
    RecurringBook,
    RfqService,
    RfqSpecification,
    SalesPolicy,
    SalesPolicyStore,
    SelfApproval,
    SellerKyc,
    SellerKycError,
    SellerUnverified,
    StrongAuthenticationRequired,
)
from ..marketplace import (
    Offer as CommerceOffer,
)
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
    stamp_output_obligations,
    stamp_value_tenant,
    utility,
)
from ..orchestrator import (
    DagRouteRunner,
    ValuePipeError,
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
from ..roster import agent_card, agent_turn, roster_items
from ..runtime.bundle import BundleError, BundleStore
from ..seats import SEATS, DeskFiles, SeatViolation
from ..settings_node import SettingError, SettingsNode
from ..skills.contract import (
    ContractEdge,
    NodeContract,
    NodeStats,
    Slot,
    SubgraphBody,
    derive_data_edges,
)
from ..skills.inputs import bind_inputs, inputs_manifest, validate_user_inputs
from ..skills.models import ActionEvent, ExecutionStatus, ReusableSkill
from ..skills.ports import ActionExecutor
from ..social import MAX_MESSAGE_CHARS, MAX_PHOTO_BYTES
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
# which cannot create a node and must not narrate that it did). Adjectives
# ride along ("a NEW node", "a weather-fetching node"), and the ask-shaped
# forms count too ("i need a node that ..."), because every phrasing this
# misses becomes a workflow run on the meta-sentence instead of a build.
_NODE_BUILD_RE = re.compile(
    r"^\s*"
    # optional polite / addressing lead-in
    r"(?:(?:hey\s+)?oolu[,:]?\s+)?"
    r"(?:(?:please|can\s+you|could\s+you|would\s+you|will\s+you|"
    r"i(?:'d| would)?\s+(?:like|want)\s+you\s+to|i\s+want\s+to|"
    r"i\s+need\s+you\s+to)\s+)?"
    r"(?:please\s+)?"
    # the build verb — or the ask-shaped "i need/want ..." with an
    # indefinite article (definite forms like "i need the node logs"
    # stay conversation)
    r"(?:(?:build|create|make|add|set\s+up)\s+(?:me\s+)?"
    r"(?:a|an|the|another|my)\s+"
    r"|i\s+(?:need|want)\s+(?:a|an|another)\s+)"
    r"(?:[\w()/-]+\s+){0,3}?"
    r"node\b"
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


# The explicit PROGRAM request (F1) — checked BEFORE the node regex, so
# "build me a program (node) that …" routes to the program pipeline and
# never pays for a discarded single-file authoring. This is the v1
# trigger by decision: no plan-sniffing fork, no tax on the common path.
# After "program" (or "program node") the head noun must END or be
# followed by a CONNECTOR (F1.1) — otherwise "build me a program manager
# node" would be hijacked as a program build of "manager node"; that is a
# single node about program management, and falls through to the node
# regex.
_PROGRAM_BUILD_RE = re.compile(
    r"(?:(?:build|create|make|write|set\s+up)\s+(?:me\s+)?"
    r"(?:a|an|the|another|my)\s+"
    r"|i\s+(?:need|want)\s+(?:a|an|another)\s+)"
    r"program(?:\s+node)?\b"
    r"(?:\s*(?:for|that|to|which|:)\s*(?P<goal>.*)|\s*)$",
    re.IGNORECASE | re.DOTALL,
)


def explicit_program_build_goal(message: str | None) -> str | None:
    """The goal in an explicit program-build request, or ``None``. A bare
    "build me a program" matches with an empty goal (so the builder can
    ask what to build); "build me a program manager node" does NOT match
    (the head noun is followed by a word, not a connector)."""
    match = _PROGRAM_BUILD_RE.match(message or "")
    if match is None:
        return None
    return (match.group("goal") or "").strip(" .!?")


def _program_tree_path_unsafe(path: str) -> bool:
    """A program tree key must be POSIX-relative and inside the tree —
    the same wall the spec applies to declared module paths, applied to
    EVERY key the author supplies (F0.1)."""
    if not path or path.startswith("/") or "\\" in path or ":" in path:
        return True
    return any(part in ("", "..", ".") for part in path.split("/"))


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
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
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
        commerce_psp=None,  # billing.psp.PaymentProviderPort: the order
        # machine's payment provider — live StripePaymentIntents when a
        # secret key exists, the pre-launch FakePsp otherwise
        commerce_providers=(),  # identity ProviderConfigs for the money
        # gate: a live commerce PSP demands require_production_money
        commerce_jurisdiction=None,  # billing.tax.JurisdictionModule: the
        # host's operating jurisdiction; None = a zero-rate LOCAL module
        commerce_evidence=None,  # artifact store for delivery evidence:
        # content lands content-addressed (sha256:<digest>), so the ref
        # on the audit chain is tamper-evident. None = refs only
        commerce_job_dispatcher=None,  # marketplace.WorkerLeaseDispatcher
        # (or any callable taking an ExecutionJob): hands jobs to the
        # worker control plane's signed leases. None = record-only desk
        commerce_peer_secrets=None,  # Mapping[peer_id, shared secret] for
        # the A2A protocol — injected at composition (a vault concern);
        # a peer without a secret can announce nothing this host trusts
        commerce_peer_identity="",  # this host's agreed identity on the
        # peer wire — the signer id its announcements carry. "" = this
        # host does not announce
        commerce_peer_transport=None,  # marketplace.PeerTransport: how
        # fetches reach a peer's announcements door. None = no fetching
        commerce_extra_jurisdictions=(),  # extra billing.tax
        # JurisdictionModules beyond the host's own — the federation's
        # cross-border deployment gate reads the same registry
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
        assistant_history=None,  # social.AssistantHistoryStore: one thread
        # per account per agent (OoLu + the roster), shared by every
        # signed-in device
        profile_photos=None,  # social.ProfilePhotoStore: the byline's
        # face — published to the account's own tenant
        press=None,  # press.PressDesk: the contribution spine (A1) —
        # member-published, licensed, attributed, revocable-forward
        ad_dividend=None,  # billing.AdDividendService: verified ad
        # impressions → conserved contributor accruals (A5). None keeps
        # the previews-only posture; the service itself refuses local
        # infra either way (require_production_money).
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
        # The Paver's heartbeat (W1): the survey's own standing Routine on
        # its own table, and the durable map it refreshes. Same consent-
        # first, fleet-safe discipline as the sweep — a separate schedule
        # so the two cadences never entangle.
        from ..paver import PaveStore, PaverScheduleStore, WebSurveyor

        self._paver_schedule = PaverScheduleStore(durable.conn)
        self._pave_store = PaveStore(durable.conn)
        self._surveyor = WebSurveyor()
        self._paver_gate = 0.0
        # Retention's own gate: at most one pruning pass per hour per host.
        self._retention_gate = 0.0
        # The pulse (personal-nodes plan P0): durable schedules that fire
        # runs as their owners, elected by (schedule, occurrence) claims.
        from ..pulse import PulseStore

        self._pulse = PulseStore(durable.conn)
        self._pulse_gate = 0.0
        # The ad house (agents-expansion A4): campaigns, computed
        # placements, gated delivery events — and the versioned-consent
        # record the whole surface stands behind (invariant 13).
        from ..adhouse import AdEventStore, CampaignStore, PlacementStore
        from ..legal import LegalAcceptanceStore

        self._legal_acceptances = LegalAcceptanceStore(durable.conn)
        self._ad_campaigns = CampaignStore(durable.conn)
        self._ad_placements = PlacementStore(durable.conn)
        self._ad_events = AdEventStore(durable.conn)
        # The explorer desk (A6): verified-buyer reviews and member lab
        # reports — the evidence layer the comparison matrix reads.
        from ..explorer import LabStore, ReviewDesk, ReviewStore

        self._explorer_reviews = ReviewDesk(ReviewStore(durable.conn))
        self._explorer_lab = LabStore(durable.conn)
        # The calendar records (A7's prerequisite): one calendar for
        # OoLu, the starter shelf, and the travel desk — with the
        # consented, privacy-shaped free-busy grants beside it.
        from ..records import CalendarStore, FreeBusyGrants

        self._calendar = CalendarStore(durable.conn)
        self._freebusy = FreeBusyGrants(durable.conn)
        # The commercial spine (marketplace-build-plan M0): typed intents,
        # the deterministic policy ladder, digest-bound approvals. M2
        # wires the history port, so risk facts derive from the order
        # book instead of arriving on faith.
        self._commerce = MarketplaceSpine(
            durable.conn,
            audit=durable.audit,
            history=OrderHistory(durable.conn),
        )
        # The fixed-price market (M1) + M2's trust machinery: catalog,
        # atomic inventory, escrow-held orders, the double-entry ledger,
        # tax and invoices. The payment provider is the pre-launch fake —
        # live Stripe swaps in behind the same port, gated by
        # require_production_money. The jurisdiction module is the
        # compliance deployment gate: no module, no transacting.
        self._commerce_seller_kyc = SellerKyc(durable.conn, audit=durable.audit)
        self._commerce_inventory = InventoryService(durable.conn)
        self._commerce_jurisdiction = (
            commerce_jurisdiction
            if commerce_jurisdiction is not None
            else JurisdictionModule(code="LOCAL", tax_rate_bps=0)
        )
        self._commerce_tax = TaxRegistry(
            (self._commerce_jurisdiction, *tuple(commerce_extra_jurisdictions))
        )
        self._commerce_catalog = CatalogService(
            durable.conn,
            audit=durable.audit,
            seller_verified=self._commerce_seller_verified,
            inventory=self._commerce_inventory,
            tax=self._commerce_tax,
            jurisdiction=self._commerce_jurisdiction.code,
        )
        self._commerce_ledger = DoubleEntryLedger(durable.conn)
        self._commerce_invoices = InvoiceBook(durable.conn)
        self._commerce_orders = OrderService(
            durable.conn,
            audit=durable.audit,
            spine=self._commerce,
            psp=commerce_psp if commerce_psp is not None else FakePsp(),
            ledger=self._commerce_ledger,
            providers=tuple(commerce_providers),
            escrow=EscrowPolicy(),
            inventory=self._commerce_inventory,
            invoices=self._commerce_invoices,
            jurisdiction=self._commerce_jurisdiction,
        )
        self._commerce_rfq = RfqService(durable.conn, audit=durable.audit)
        self._commerce_sales_policies = SalesPolicyStore(durable.conn)
        self._commerce_evidence = commerce_evidence
        # M3: recurring obligations, four-eyes payout changes, typed
        # execution jobs, and the reconciliation desk.
        self._commerce_recurring = RecurringBook(
            durable.conn, audit=durable.audit
        )
        self._commerce_payout_changes = PayoutChangeDesk(
            durable.conn, audit=durable.audit
        )
        self._commerce_jobs = JobDesk(
            durable.conn, audit=durable.audit, dispatcher=commerce_job_dispatcher
        )
        self._commerce_reconciliation = ReconciliationDesk(
            durable.conn,
            audit=durable.audit,
            orders=self._commerce_orders.orders,
            ledger=self._commerce_ledger,
            invoices=self._commerce_invoices,
        )
        # M4: the open market — peers, imports, and the sourcing sweep,
        # all behind the same policy engine and ledger.
        self._commerce_federation = FederationDesk(
            durable.conn,
            audit=durable.audit,
            tax=self._commerce_tax,
            secrets=commerce_peer_secrets,
        )
        self._commerce_peer_secrets = dict(commerce_peer_secrets or {})
        self._commerce_peer_identity = str(commerce_peer_identity or "")
        self._commerce_peer_transport = commerce_peer_transport
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
        self._profile_photos = profile_photos
        self._press = press
        # The poll floor's social scientist: reported findings, so a
        # verdict speaks once (lazy — built on the first decided vote).
        self._poll_findings = None
        self._ad_dividend = ad_dividend
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
        # The agent roster (A0): who is listed below OoLu. The same /v1/chat
        # door carries a roster agent's turn (body: {"agent": ...}); this
        # door is what the sidebar renders.
        r.add("GET", "/v1/roster", self._roster)
        # The press (A1): the contribution spine. Members publish under a
        # stated license; every read excludes superseded records by law.
        r.add("GET", "/v1/press/genres", self._press_genres)
        r.add("GET", "/v1/press/contributions", self._press_list)
        r.add("POST", "/v1/press/contributions", self._press_publish)
        r.add(
            "GET",
            "/v1/press/contributions/{contribution_id}",
            self._press_detail,
        )
        r.add(
            "POST",
            "/v1/press/contributions/{contribution_id}/unpublish",
            self._press_unpublish,
        )
        r.add(
            "GET",
            "/v1/press/contributions/{contribution_id}/media/{index}",
            self._press_media,
        )
        # The newsroom (A2): stories composed from contributions only,
        # each member's edition, and the feedback that (with consent)
        # shapes it. The reasons render on demand from the detail door.
        r.add("GET", "/v1/press/stories", self._press_stories)
        r.add("GET", "/v1/press/stories/{story_id}", self._press_story_detail)
        r.add(
            "POST",
            "/v1/press/stories/{story_id}/feedback",
            self._press_story_feedback,
        )
        r.add("POST", "/v1/press/newsroom/run", self._press_newsroom_run)
        r.add("POST", "/v1/press/edition/schedule", self._press_edition_schedule)
        # The poll floor (A3): vote first, see second; the floor holds.
        r.add("GET", "/v1/press/polls/next", self._press_poll_next)
        r.add("POST", "/v1/press/polls/{pair_id}/vote", self._press_poll_vote)
        r.add("GET", "/v1/press/polls/{pair_id}/stats", self._press_poll_stats)
        # The member's own pairwise preferences, DPO-shaped (consented).
        r.add(
            "GET",
            "/v1/press/preferences/export",
            self._press_preferences_export,
        )
        # The byline: an account's published face and name, readable by
        # the account's own tenant — and the owner's doors to set it.
        r.add("GET", "/v1/profiles/{username}", self._profile_get)
        r.add("GET", "/v1/profiles/{username}/photo", self._profile_photo_get)
        r.add("POST", "/v1/profile/photo", self._profile_photo_put)
        r.add("DELETE", "/v1/profile/photo", self._profile_photo_delete)
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
        # The pulse (personal-nodes plan P0): standing schedules that
        # fire runs as their owners — daily/weekly/monthly/yearly.
        r.add("GET", "/v1/pulse", self._pulse_view)
        r.add("POST", "/v1/pulse", self._pulse_create)
        r.add("POST", "/v1/pulse/{schedule_id}", self._pulse_toggle)
        r.add("DELETE", "/v1/pulse/{schedule_id}", self._pulse_delete)
        # The commercial spine (marketplace-build-plan M0): intents and
        # digest-bound approvals only — no order execution door exists.
        r.add("GET", "/v1/commerce/policy", self._commerce_policy_get)
        r.add("PUT", "/v1/commerce/policy", self._commerce_policy_put)
        r.add("GET", "/v1/commerce/delegations", self._commerce_delegations_list)
        r.add("POST", "/v1/commerce/delegations", self._commerce_delegation_grant)
        r.add(
            "DELETE",
            "/v1/commerce/delegations/{delegation_id}",
            self._commerce_delegation_revoke,
        )
        r.add("GET", "/v1/commerce/intents", self._commerce_intents_list)
        r.add("POST", "/v1/commerce/intents", self._commerce_intent_create)
        r.add("GET", "/v1/commerce/intents/{intent_id}", self._commerce_intent_get)
        r.add("GET", "/v1/commerce/approvals", self._commerce_approvals_inbox)
        r.add(
            "POST",
            "/v1/commerce/intents/{intent_id}/approval",
            self._commerce_intent_approve,
        )
        # The fixed-price market (M1): the catalog and the order machine.
        # Seller KYC: apply as a legal entity; a reviewer with approve
        # authority decides; verification is what publication reads.
        r.add("GET", "/v1/commerce/seller/kyc", self._commerce_seller_kyc_status)
        r.add("POST", "/v1/commerce/seller/kyc", self._commerce_seller_kyc_apply)
        r.add(
            "GET",
            "/v1/commerce/seller/kyc/queue",
            self._commerce_seller_kyc_queue,
            requires_permission="kyc:review",
        )
        r.add(
            "POST",
            "/v1/commerce/seller/kyc/decide",
            self._commerce_seller_kyc_decide,
        )
        r.add("GET", "/v1/commerce/catalog", self._commerce_catalog_browse)
        r.add("GET", "/v1/commerce/listings", self._commerce_listings_list)
        r.add("POST", "/v1/commerce/listings", self._commerce_listing_create)
        # A published product's media, by reference — publication is the
        # consent that crosses the drawer wall (the press media law).
        r.add(
            "GET",
            "/v1/commerce/listings/{listing_id}/media/{index}",
            self._commerce_listing_media,
        )
        # The desk: where the member's position meets the market's
        # demand (the briefing), the standing brief schedule, and the
        # list-out of everything they created on the platform.
        r.add("GET", "/v1/commerce/desk", self._commerce_desk)
        r.add(
            "POST", "/v1/commerce/desk/schedule", self._commerce_desk_schedule
        )
        r.add("GET", "/v1/commerce/mine", self._commerce_mine)
        r.add(
            "POST",
            "/v1/commerce/listings/{listing_id}/publish",
            self._commerce_listing_publish,
        )
        r.add(
            "POST",
            "/v1/commerce/listings/{listing_id}/offer",
            self._commerce_listing_offer,
        )
        # M2: RFQ and quotes, seller automation policy, evidence and
        # invoices.
        r.add("GET", "/v1/commerce/rfqs", self._commerce_rfqs_list)
        r.add("POST", "/v1/commerce/rfqs", self._commerce_rfq_open)
        r.add(
            "GET", "/v1/commerce/rfqs/{rfq_id}/quotes", self._commerce_quotes_list
        )
        r.add(
            "POST", "/v1/commerce/rfqs/{rfq_id}/quotes", self._commerce_quote_submit
        )
        r.add("POST", "/v1/commerce/rfqs/{rfq_id}/award", self._commerce_rfq_award)
        r.add("GET", "/v1/commerce/sales-policy", self._commerce_sales_policy_get)
        r.add("PUT", "/v1/commerce/sales-policy", self._commerce_sales_policy_put)
        r.add(
            "POST",
            "/v1/commerce/orders/{order_id}/evidence",
            self._commerce_order_evidence,
        )
        r.add(
            "GET",
            "/v1/commerce/orders/{order_id}/invoice",
            self._commerce_order_invoice,
        )
        # M3: milestones, recurring obligations, payout changes, jobs,
        # and the reconciliation desk.
        r.add(
            "GET",
            "/v1/commerce/orders/{order_id}/milestones",
            self._commerce_milestones_list,
        )
        r.add(
            "POST",
            "/v1/commerce/orders/{order_id}/milestones/{index}/deliver",
            self._commerce_milestone_deliver,
        )
        r.add(
            "POST",
            "/v1/commerce/orders/{order_id}/milestones/{index}/accept",
            self._commerce_milestone_accept,
        )
        r.add(
            "POST",
            "/v1/commerce/orders/{order_id}/milestones/{index}/fail",
            self._commerce_milestone_fail,
        )
        r.add(
            "POST",
            "/v1/commerce/orders/{order_id}/refund-unreleased",
            self._commerce_refund_unreleased,
        )
        r.add(
            "POST",
            "/v1/commerce/orders/{order_id}/adjudicate",
            self._commerce_order_adjudicate,
        )
        r.add("GET", "/v1/commerce/recurring", self._commerce_recurring_list)
        r.add("POST", "/v1/commerce/recurring", self._commerce_recurring_create)
        r.add(
            "POST",
            "/v1/commerce/recurring/{obligation_id}/renew",
            self._commerce_recurring_renew,
        )
        r.add(
            "DELETE",
            "/v1/commerce/recurring/{obligation_id}",
            self._commerce_recurring_cancel,
        )
        r.add(
            "GET", "/v1/commerce/payout-changes", self._commerce_payout_list
        )
        r.add(
            "POST", "/v1/commerce/payout-changes", self._commerce_payout_request
        )
        r.add(
            "POST",
            "/v1/commerce/payout-changes/{request_id}/approve",
            self._commerce_payout_approve,
        )
        r.add(
            "POST",
            "/v1/commerce/payout-changes/{request_id}/apply",
            self._commerce_payout_apply,
        )
        r.add(
            "POST",
            "/v1/commerce/orders/{order_id}/jobs",
            self._commerce_job_dispatch,
        )
        r.add(
            "POST", "/v1/commerce/jobs/{job_id}/ack", self._commerce_job_ack
        )
        r.add(
            "POST",
            "/v1/commerce/jobs/{job_id}/complete",
            self._commerce_job_complete,
        )
        # M4: the open market.
        r.add("GET", "/v1/commerce/peers", self._commerce_peers_list)
        r.add(
            "POST",
            "/v1/commerce/peers",
            self._commerce_peer_register,
            requires_permission="providers:manage",
        )
        r.add(
            "POST",
            "/v1/commerce/peers/{peer_id}/state",
            self._commerce_peer_state,
            requires_permission="providers:manage",
        )
        r.add(
            "POST",
            "/v1/commerce/peers/{peer_id}/offers",
            self._commerce_peer_import,
        )
        r.add(
            "POST",
            "/v1/commerce/peers/{peer_id}/fetch",
            self._commerce_peer_fetch,
        )
        r.add(
            "GET", "/v1/commerce/announcements", self._commerce_announcements
        )
        r.add("GET", "/v1/commerce/source", self._commerce_source)
        r.add(
            "GET",
            "/v1/commerce/reconciliation",
            self._commerce_reconciliation_list,
        )
        r.add(
            "POST",
            "/v1/commerce/reconciliation/sweep",
            self._commerce_reconciliation_sweep,
        )
        r.add("GET", "/v1/commerce/orders", self._commerce_orders_list)
        r.add("POST", "/v1/commerce/orders", self._commerce_order_place)
        r.add("GET", "/v1/commerce/orders/{order_id}", self._commerce_order_get)
        r.add(
            "POST", "/v1/commerce/orders/{order_id}/ship", self._commerce_order_ship
        )
        r.add(
            "POST",
            "/v1/commerce/orders/{order_id}/deliver",
            self._commerce_order_deliver,
        )
        r.add(
            "POST",
            "/v1/commerce/orders/{order_id}/accept",
            self._commerce_order_accept,
        )
        r.add(
            "POST",
            "/v1/commerce/orders/{order_id}/cancel",
            self._commerce_order_cancel,
        )
        r.add(
            "POST",
            "/v1/commerce/orders/{order_id}/refund",
            self._commerce_order_refund,
        )
        r.add(
            "GET",
            "/v1/commerce/orders/{order_id}/ledger",
            self._commerce_order_ledger,
        )
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
        # Versioned consent (A4, invariant 13): what the caller has
        # accepted, and the door to accept the current version.
        r.add("GET", "/v1/legal/consent", self._legal_consent_get)
        r.add("POST", "/v1/legal/consent", self._legal_consent_post)
        # The ad house (A4): campaigns for verified sellers; placements
        # merged at render on the News/Poll surfaces only; delivery
        # events behind gates; earnings previews, never balances.
        r.add("GET", "/v1/adhouse/campaigns", self._adhouse_campaigns_list)
        r.add("POST", "/v1/adhouse/campaigns", self._adhouse_campaign_create)
        r.add(
            "POST",
            "/v1/adhouse/campaigns/{campaign_id}/status",
            self._adhouse_campaign_status,
        )
        r.add("GET", "/v1/press/ads", self._press_ads)
        r.add(
            "POST",
            "/v1/adhouse/placements/{placement_id}/impression",
            self._adhouse_impression,
        )
        r.add(
            "POST",
            "/v1/adhouse/placements/{placement_id}/click",
            self._adhouse_click,
        )
        r.add("GET", "/v1/adhouse/preview", self._adhouse_preview)
        # A5: verified impressions become conserved contributor money —
        # production-gated; a local host answers with the honest refusal.
        r.add("POST", "/v1/adhouse/settle", self._adhouse_settle)
        # The explorer desk (A6): verified evidence, one comparison
        # matrix, deterministic best-buy briefs, followed interests.
        # One search for every surface: OoLu, shop, request, Explorer —
        # a unique listing id hits exactly; anything else finds the
        # CLOSEST existing products (the one retrieval scorer).
        r.add("GET", "/v1/commerce/search", self._commerce_search)
        # Life books: the prebuilt nodes' one architecture — the shared
        # function is the house's; each member's data is ONE private
        # file per book in their own Life drawer, at a stable pointer.
        r.add("GET", "/v1/life/books", self._life_books)
        r.add("POST", "/v1/life/books/import", self._life_books_import)
        r.add("GET", "/v1/life/books/{kind}", self._life_book_rows)
        r.add("GET", "/v1/life/books/{kind}/chart", self._life_book_chart)
        # What EXISTS to follow: the categories real listings and open
        # requests actually carry — never an invented taxonomy.
        r.add("GET", "/v1/explorer/categories", self._explorer_categories)
        r.add("GET", "/v1/explorer/compare", self._explorer_compare)
        r.add("GET", "/v1/explorer/reviews", self._explorer_reviews_list)
        r.add("POST", "/v1/explorer/reviews", self._explorer_review_create)
        r.add("GET", "/v1/explorer/lab", self._explorer_lab_list)
        r.add("POST", "/v1/explorer/lab", self._explorer_lab_create)
        r.add("POST", "/v1/explorer/interests", self._explorer_interest)
        # The travel desk (A7) and its prerequisite, the calendar records:
        # one calendar, privacy-shaped free-busy, constraint-checked
        # briefs, and the confirm door that lands a booked trip as events.
        r.add("GET", "/v1/records/calendar", self._calendar_list)
        r.add("POST", "/v1/records/calendar", self._calendar_add)
        r.add(
            "DELETE",
            "/v1/records/calendar/{event_id}",
            self._calendar_delete,
        )
        r.add("GET", "/v1/records/freebusy/grants", self._freebusy_grants)
        r.add("POST", "/v1/records/freebusy/grants", self._freebusy_grant_set)
        r.add("GET", "/v1/travel/plan", self._travel_plan)
        r.add("POST", "/v1/travel/confirm", self._travel_confirm)
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
        # The Paver (W1): the survey's standing schedule and the map it
        # draws. Enabling is the approved, audited act (like the sweep);
        # the map is the caller's own tenant, read straight back.
        r.add("GET", "/v1/paver/schedule", self._paver_schedule_view)
        r.add("POST", "/v1/paver/schedule", self._paver_schedule_set)
        r.add("DELETE", "/v1/paver/schedule", self._paver_schedule_clear)
        r.add("GET", "/v1/paver/webs", self._paver_webs)
        r.add("GET", "/v1/paver/webs/{anchor}", self._paver_webs_for_anchor)
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
        r.add("GET", "/v1/market/library", self._market_library)
        r.add(
            "GET",
            "/v1/nodes/overview",
            self._nodes_overview,
            requires_permission="users:manage",
        )
        r.add("POST", "/v1/market/quotes", self._market_quote)
        r.add("POST", "/v1/market/assemble", self._market_assemble)
        r.add("POST", "/v1/runs/contract", self._submit_contract_run)
        r.add("GET", "/v1/runs/contract/holds", self._list_contract_holds)
        r.add("GET", "/v1/inbox", self._inbox_view)
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
        # A roster agent's turn (A0): same door, its own lean path — its
        # own seat, its own thread, no tools, no task lane. OoLu's full
        # surface continues below.
        roster_agent = str(body.get("agent") or "").strip()
        if roster_agent and roster_agent != "oolu":
            return self._roster_turn(
                request, session, body, roster_agent, message, history
            )
        # A book asked for by name ("show cashflow") answers as a CHART
        # BLOCK from the member's own Life/Files — deterministic, before
        # any model spend.
        booked = self._book_command(session, message)
        if booked is not None:
            say, block = booked
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
            return json_response(
                200,
                {
                    "reply": say,
                    "source": "desk",
                    "actions": [],
                    "reasoning": None,
                    "device": None,
                    "copy": None,
                    "run_id": None,
                    "run": None,
                    "block": block,
                },
            )
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
                if answer is not None and kind == "handoff":
                    # The offered standing output (B4): yes binds it onto
                    # the declared input, no runs the node plain — either
                    # way the run the question paused now fires.
                    turn, run = self._run_with_handoff(
                        session, offered_goal, bind=answer == "yes"
                    )
                elif answer is not None and kind == "task_reminder":
                    # The offered reminder (P2): yes files the row into
                    # the standing store; no leaves the task alone.
                    turn = self._answer_task_reminder(
                        session, offered_goal, accept=answer == "yes"
                    )
                elif answer == "yes" and kind == "reuse":
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
        # build that no code performed. A "build me a program …" routes to
        # the program pipeline FIRST (F1) — before any single-file authoring
        # could be paid for and discarded.
        if turn is None and not in_node and self._nodeplace is not None:
            program_goal = explicit_program_build_goal(message)
            if program_goal is not None:
                built = self._build_program_node(session, program_goal)
                if built.startswith("error:"):
                    turn = ChatTurn(
                        say="I couldn't build that program: "
                        + built[6:].strip(),
                        source="tool",
                    )
                else:
                    turn = ChatTurn(
                        say=built,
                        source="tool",
                        actions=[{"tool": "build_program"}],
                    )
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
        # A spoken rhythm ("every day at 9 run …") is DETERMINISTIC,
        # model or not — the row is created and read back from the
        # store, never narrated (the reminder doctrine, on a schedule).
        if turn is None and not in_node:
            spoken = self._pulse_command(
                session, message, body, request.now or self._clock()
            )
            if spoken is not None:
                turn = ChatTurn(
                    say=spoken, source="tool", actions=[{"tool": "pulse"}]
                )
        if turn is None:
            cleaned = [h for h in history if isinstance(h, dict)]
            recent = cleaned[-20:]
            # Server-side conversation truth, first step (plan M2): when
            # the window drops older turns, the drop is NAMED and the
            # earliest standing user asks survive verbatim — commitments
            # outlive compaction instead of vanishing silently.
            dropped = [
                str(h.get("content", ""))
                for h in cleaned[:-20]
                if h.get("role") == "user" and str(h.get("content", "")).strip()
            ]
            if dropped:
                context_note = (
                    context_note
                    + "\nEarlier in this conversation, beyond the visible "
                    "window, the user said (oldest first): "
                    + " | ".join(d[:160] for d in dropped[:3])
                )
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
        actions = turn.actions
        build_task = (
            explicit_node_build_goal(turn.task)
            if turn.task and not in_node and self._nodeplace is not None
            else None
        )
        if build_task is not None:
            # The model routed an explicit node-build ask into the task
            # lane (its prompt tells it to). Building is the WRITING
            # door — author the function, pass the birth gate, land on
            # the desk — never a workflow run on the meta-sentence: a
            # run for "build a node to X" executes nothing but noise
            # and leaves nothing in any node.
            built = self._build_function_node(session, build_task)
            if built.startswith("error:"):
                say = f"I couldn't build that node: {built[7:].strip()}"
            else:
                say = built
                actions = [*actions, {"tool": "build_node"}]
        elif turn.task:
            try:
                # A declared input another node's standing output answers
                # (B4): the run PAUSES on one question — use that newest
                # output as the default? — and fires on the answer, the
                # value bound only on a yes, never silently.
                handoff = (
                    self._offer_handoff(session, turn.task)
                    if not in_node
                    else None
                )
                if handoff is not None:
                    say = f"{say} {handoff}".strip()
                else:
                    # With standing consent, the missing node is built
                    # BEFORE the run — function written, node on the desk,
                    # the route through it — instead of firing a doomed
                    # run first and offering afterwards. No consent, no
                    # silent build: the growth offer below still asks.
                    built = None
                    if not in_node:
                        built = self._autobuild_before_run(session, turn.task)
                    if built:
                        say = f"{say} {built}".strip()
                    run = self._start_intent_run(session, turn.task)
                    self._metrics["chat_runs"] += 1
                    # The run may have already failed DURING execution
                    # (submit runs synchronously to the first pause or
                    # terminal phase). The growth check must fire here too
                    # — not only on the planning-time refusal below — so a
                    # failed execution names the failing node and offers
                    # to grow what's missing.
                    say = self._describe_run_failure(
                        say, run, autobuild_hint=in_node
                    )
                    if not in_node:
                        # P2: a task the run dated offers its reminder.
                        say = self._offer_task_reminder(say, session, run)
                        say = self._offer_growth(
                            say, session, turn.task, run=run
                        )
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
                "actions": actions,
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
        """One agent's thread, oldest first — OoLu's by default, a roster
        agent's with ``?agent=`` — what a fresh device loads so every
        client shows the same conversation."""
        if self._assistant_history is None:
            raise GatewayError(404, "not_found", "chat history is not kept here")
        agent = str(request.query.get("agent") or "oolu").strip()
        if agent != "oolu" and agent_card(agent) is None:
            raise GatewayError(400, "invalid_request", f"unknown agent: {agent}")
        return json_response(
            200,
            {
                "items": self._assistant_history.history(
                    tenant=session.tenant_id,
                    principal=session.principal_id,
                    agent=agent,
                )
            },
        )

    # ------------------------------------------------------------------ #
    # The agent roster (A0): the list below OoLu, and a roster turn.      #
    # ------------------------------------------------------------------ #
    def _roster(self, request, session, params) -> Response:
        """Who is listed below OoLu — the sidebar renders exactly this."""
        return json_response(200, {"items": roster_items()})

    def _roster_turn(
        self, request, session, body, agent_id, message, history
    ) -> Response:
        """One turn with a roster agent: the card's honest scope, spoken
        through the agent's OWN seat — metered and booked apart from the
        OoLu conversation — and landed in the agent's OWN thread. The
        News thread carries the desk's intake: raw material is detected
        and reviewed BEFORE the model speaks — the desk's own hand, the
        seat's ordinary voice for everything else."""
        card = agent_card(agent_id)
        if card is None:
            raise GatewayError(
                400, "invalid_request", f"unknown agent: {agent_id}"
            )
        served = None
        block = None
        if card.agent_id == "news":
            served = self._news_intake_turn(session, body, message)
        elif card.agent_id == "market":
            served = self._market_desk_turn(
                session, message, request.now or self._clock()
            )
        elif card.agent_id == "poll":
            served = self._poll_desk_turn(session, message)
        elif card.agent_id == "explorer":
            served = self._explorer_desk_turn(
                session, message, request.now or self._clock()
            )
        if served is not None:
            # A desk may answer with words alone, or words plus a BLOCK
            # — a structured piece the client renders in the bubble (a
            # poll pair to vote on, the genre chips to pick from).
            say, reasoning, source, *rest = served
            block = rest[0] if rest else None
        else:
            # The seat is the office: the router is cached and metered per
            # (tenant, purpose), and the actor stamp keeps a shared tenant's
            # gauges per person — exactly the chat.turn discipline.
            router = self._seat_actor(
                self._tenant_model(session.tenant_id, purpose=card.seat),
                session.principal_id,
            )
            turn_now = request.now or self._clock()
            local_now = turn_now + timedelta(
                minutes=_tz_minutes(body.get("tz_offset_minutes"))
            )
            time_note = (
                f"Current time: {turn_now:%Y-%m-%d %H:%M} UTC; the user's "
                f"local time is {local_now:%Y-%m-%d %H:%M}."
            )
            recent = [h for h in history if isinstance(h, dict)][-20:]
            say, reasoning, source = agent_turn(
                card, message, history=recent, model=router, context=time_note
            )
        # The conversation survives the device, exactly like OoLu's —
        # tagged with the agent, so each thread stays its own.
        if self._assistant_history is not None:
            self._assistant_history.append(
                tenant=session.tenant_id,
                principal=session.principal_id,
                kind="user",
                body=message,
                agent=card.agent_id,
            )
            self._assistant_history.append(
                tenant=session.tenant_id,
                principal=session.principal_id,
                kind="assistant",
                body=say,
                agent=card.agent_id,
            )
        return json_response(
            200,
            {
                "reply": say,
                "source": source,
                "actions": [],
                "reasoning": reasoning,
                "device": None,
                "copy": None,
                "run_id": None,
                "run": None,
                "agent": card.agent_id,
                "block": block,
            },
        )

    # The Poll thread's deterministic asks — the social scientist's desk.
    _POLL_PAIR_ASKS = frozenset(
        {"poll", "next", "next pair", "another", "vote", "play"}
    )
    _POLL_GENRE_ASKS = frozenset(
        {"genres", "genre", "streams", "switch genre", "pick a genre"}
    )
    _POLL_FINDING_ASKS = frozenset(
        {
            "findings",
            "report",
            "patterns",
            "science",
            "what did you learn",
            "what have you learned",
        }
    )

    def _poll_desk_turn(self, session, message):
        """The Poll desk's hand on one thread message — or None for
        ordinary conversation. The poll and the genre picking are
        MESSAGE BLOCKS: a pair to vote on in the bubble, the genre
        chips to tap; naming a genre in words deals from that stream.
        "findings" reads the scientist's standing field notes."""
        from ..press import GENRES, PressError, taxonomy_items

        press = self._press
        if press is None or press.polls is None:
            return None
        normal = re.sub(
            r"\s+", " ", str(message or "").strip().casefold()
        ).rstrip(".!?")
        genre = None
        if normal in GENRES:
            genre = normal
        else:
            for item in taxonomy_items():
                if normal == item["label"].casefold():
                    genre = item["key"]
                    break
        if genre is None and normal not in self._POLL_PAIR_ASKS:
            if normal in self._POLL_GENRE_ASKS:
                return (
                    "Pick a stream — or say “poll” and I choose by what "
                    "the floor enjoys.",
                    None,
                    "desk",
                    {"kind": "genres", "items": taxonomy_items()},
                )
            if normal in self._POLL_FINDING_ASKS:
                findings = self._poll_science_judge(session)
                if not findings:
                    return (
                        "Still researching — the floor needs more decided "
                        "comparisons before any pattern is worth words. "
                        "Every vote helps.",
                        None,
                        "desk",
                    )
                return (
                    "\n\n".join(f.report() for f in findings),
                    None,
                    "desk",
                )
            return None
        try:
            pair = press.polls.next_pair(
                tenant=session.tenant_id,
                principal=session.principal_id,
                corpus=press.store.list(tenant=session.tenant_id, limit=500),
                genre=genre,
            )
        except PressError as exc:
            return (str(exc), None, "desk")
        return (
            "Which one? Tap to vote — the results follow your own choice, "
            "and every decided comparison teaches the floor's science.",
            None,
            "desk",
            {"kind": "poll", "pair": self._pair_dict(pair)},
        )

    def _news_intake_turn(self, session, body, message):
        """The News desk's hand on one thread message — or None when the
        message is ordinary conversation (the seat's model answers).

        The growth-offer discipline, applied to publishing:
        - An OFFERED draft is answered by the very next message: a plain
          yes publishes (the offer rendered the license terms — the yes
          is the consent), a plain no drops it, anything else withdraws
          the offer silently (fresh material starts a fresh review).
        - A GATHERING draft takes the next message as the answer to the
          desk's one question — folded in verbatim — unless it is a
          question itself (the agent answers; the desk's question keeps
          standing) or a plain no (dropped).
        - With nothing standing, material is DETECTED: attached files at
          any length, or article-shaped text past the floor.
        """
        press = self._press
        if press is None or press.intake is None:
            return None
        from ..chat import consent_answer
        from ..press import (
            DROPPED,
            draft_from_material,
            fold_answer,
            looks_like_material,
            review,
        )

        intake = press.intake
        file_ids = (body or {}).get("file_ids") or []
        if not isinstance(file_ids, list):
            raise GatewayError(400, "invalid_request", "file_ids must be a list")
        # Attachments are validated at the door — a file that is not the
        # caller's own refuses loudly NOW, not at publish time.
        refs = self._drawer_refs(session, file_ids) if file_ids else []
        answer = consent_answer(message)
        standing = intake.get(
            tenant=session.tenant_id, principal=session.principal_id
        )

        def _staged(staged, say):
            # A dropped review (a leak) leaves NOTHING standing: fixed
            # words arrive as fresh material, never folded onto leaky
            # ones.
            if staged.stage == "dropped":
                intake.pop(
                    tenant=session.tenant_id, principal=session.principal_id
                )
            else:
                intake.put(staged)
            return (say, None, "desk")

        def _fresh_review():
            draft = draft_from_material(
                tenant=session.tenant_id,
                principal=session.principal_id,
                message=message,
                file_ids=tuple(r.file_id for r in refs),
                media_types=tuple(r.media_type for r in refs),
            )
            staged, say = review(
                draft,
                live_texts=press.store.live_texts(tenant=session.tenant_id),
                title_of=self._contribution_title(session),
            )
            return _staged(staged, say)

        if standing is not None and standing.stage == "offered":
            draft = intake.pop(
                tenant=session.tenant_id, principal=session.principal_id
            )
            if draft is None:
                return None  # spent concurrently; the conversation stands
            if answer == "yes":
                return self._intake_publish(session, draft)
            if answer == "no":
                return (DROPPED, None, "desk")
            # Withdrawn — the newest material is the one on the table.
            if looks_like_material(message, has_media=bool(refs)):
                return _fresh_review()
            return None
        if standing is not None:  # gathering: the desk's question stands
            if answer == "no":
                intake.pop(
                    tenant=session.tenant_id, principal=session.principal_id
                )
                return (DROPPED, None, "desk")
            if message.strip().endswith("?"):
                return None  # a question is a question; the draft stands
            grown = fold_answer(standing, message)
            if refs:
                grown = grown.model_copy(
                    update={
                        "file_ids": (
                            *grown.file_ids,
                            *(r.file_id for r in refs),
                        )
                    }
                )
            staged, say = review(
                grown,
                live_texts=press.store.live_texts(tenant=session.tenant_id),
                title_of=self._contribution_title(session),
            )
            return _staged(staged, say)
        if looks_like_material(message, has_media=bool(refs)):
            return _fresh_review()
        return None

    def _contribution_title(self, session):
        """A title lookup for the intake's retelling credit — the
        neighbor's own headline, or None when it is gone."""

        def title_of(contribution_id: str) -> str | None:
            record = self._press.store.get(
                contribution_id, tenant=session.tenant_id
            )
            return record.title if record is not None else None

        return title_of

    def _intake_publish(self, session, draft):
        """The consented publication: the same gate the manual door
        walks, the same audit voice — then the newsroom judges the
        shelf, because whether a piece is WORTH composing is the
        rubric's decision, not the desk's mood."""
        from ..press import INTAKE_LICENSE, Newsroom, PressError

        press = self._press
        media = self._drawer_refs(
            session, list(draft.file_ids), missing="drop"
        )
        gone = len(draft.file_ids) - len(media)
        try:
            record = press.publish(
                tenant=session.tenant_id,
                author=session.principal_id,
                title=draft.title,
                body=draft.body,
                genres=draft.genres,
                license=INTAKE_LICENSE,
                consent=True,  # the yes answered the offer's rendered terms
                media=media,
            )
        except PressError as exc:
            # The gate's refusal is the desk's words; the draft returns
            # to gathering so fixed material can answer it.
            press.intake.put(draft.model_copy(update={"stage": "gathering"}))
            return (
                f"I couldn't publish it: {exc} Fix that and send it "
                "again — I never rewrite your words.",
                None,
                "desk",
            )
        self._durable.audit.append(
            "press.contribution_published",
            {
                "contribution_id": record.contribution_id,
                "author": record.author,
                "genres": list(record.genres),
                "license": record.license,
                "similar_to": record.similar_to,
            },
        )
        say = f"Published — “{record.title}” is on the shelf, credited to you."
        if gone:
            say += (
                f" ({gone} attachment{'s' if gone != 1 else ''} "
                f"{'were' if gone != 1 else 'was'} gone from your drawer "
                "and left out.)"
            )
        if press.stories is None:
            return (say, None, "desk")
        model = self._seat_actor(
            self._tenant_model(session.tenant_id, purpose="news.compose"),
            session.principal_id,
        )
        composed = Newsroom(press.store, press.stories).run(
            tenant=session.tenant_id, model=model
        )
        for story in composed:
            self._durable.audit.append(
                "press.story_composed",
                {
                    "story_id": story.story_id,
                    "source": story.source,
                    "rubric_version": story.rubric_version,
                    "lineage": [
                        {"contribution_id": s.contribution_id, "weight": s.weight}
                        for s in story.lineage
                    ],
                },
            )
        mine = next(
            (
                s
                for s in composed
                if any(
                    share.contribution_id == record.contribution_id
                    for share in s.lineage
                )
            ),
            None,
        )
        if mine is not None:
            say += (
                f" The newsroom judged it worth telling: “{mine.headline}” "
                "is in the stories now."
            )
        else:
            say += (
                " It's on the shelf — the newsroom composes it into a "
                "story as the rubric allows (freshness, corroboration, "
                "depth)."
            )
        return (say, None, "desk")

    # The Market thread's own deterministic asks — narrow on purpose, so
    # ordinary conversation always reaches the seat's model.
    _MARKET_LIST_ASKS = frozenset(
        {
            "list",
            "list out",
            "list it out",
            "mine",
            "my things",
            "what have i created",
            "what did i create",
            "what have i made",
        }
    )
    _MARKET_BRIEF_ASKS = frozenset(
        {"brief", "briefing", "desk", "what needs me", "what's waiting"}
    )

    def _market_desk_turn(self, session, message, now):
        """The Market desk's hand on one thread message — or None when
        the message is ordinary conversation. Two deterministic asks:
        the LIST-OUT (everything the member created on the platform,
        grouped and named) and the BRIEF (where their position meets
        the market's demand, right now)."""
        normal = re.sub(
            r"\s+", " ", str(message or "").strip().casefold()
        ).rstrip(".!?")
        if normal in self._MARKET_LIST_ASKS:
            return (self._market_mine_words(session), None, "desk")
        if normal in self._MARKET_BRIEF_ASKS:
            from ..marketplace import briefing_message

            items = self._market_desk_items(session, now)
            if not items:
                return (
                    "The desk sees nothing waiting on you — no approvals, "
                    "no orders needing action, and no open request "
                    "matching what you sell.",
                    None,
                    "desk",
                )
            return (briefing_message(items), None, "desk")
        return None

    def _market_mine_words(self, session) -> str:
        """The list-out, in the desk's own words: counts and names per
        group, bounded — the form blocks in this thread carry the rest."""
        listings = self._commerce_catalog.store.for_seller(
            tenant=session.tenant_id, seller=session.principal_id
        )
        rfqs = self._commerce_rfq.mine(
            tenant=session.tenant_id, buyer=session.principal_id
        )
        orders = self._commerce_orders.orders.list_for(
            tenant=session.tenant_id, principal=session.principal_id
        )
        recurring = self._commerce_recurring.list_for(
            tenant=session.tenant_id, principal=session.principal_id
        )
        delegations = self._commerce.delegations.list_for(
            tenant=session.tenant_id, principal=session.principal_id
        )
        cap = 8
        lines = ["Created on this platform, by you:"]
        lines.append(f"Listings ({len(listings)}):")
        for x in listings[:cap]:
            media = f", {len(x.media)} media" if x.media else ""
            lines.append(f"  • “{x.title}” — {x.status}{media}")
        lines.append(f"Requests for quotes ({len(rfqs)}):")
        for r in rfqs[:cap]:
            lines.append(
                f"  • {r.specification.category} ×"
                f"{r.specification.quantity} — {r.state}"
            )
        lines.append(f"Orders ({len(orders)}):")
        for o in orders[:cap]:
            role = (
                "buying"
                if o.record.buyer_principal == session.principal_id
                else "selling"
            )
            lines.append(
                f"  • {o.record.order_id[:8]} — {o.state} ({role})"
            )
        lines.append(f"Recurring obligations ({len(recurring)}):")
        lines.append(f"Delegations ({len(delegations)}):")
        lines.append(
            "The form blocks in this thread carry every detail and door."
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # The explorer desk: closest products, followable categories, and     #
    # comparisons that end.                                               #
    # ------------------------------------------------------------------ #
    def _comparisons(self):
        if getattr(self, "_explorer_comparisons", None) is None:
            from ..explorer import ComparisonStore

            self._explorer_comparisons = ComparisonStore(self._durable.conn)
        return self._explorer_comparisons

    def _closest_listings(self, query: str, *, limit: int = 8) -> list:
        """The CLOSEST existing products for any words — a unique
        listing id hits exactly; text ranks the active shelf by the one
        retrieval scorer over title, category, and description."""
        from ..retrieval import score as similarity_score

        query = str(query or "").strip()
        exact = self._commerce_catalog.store.get(query)
        if exact is not None and exact.status == "active":
            return [exact]
        scored = [
            (
                similarity_score(
                    query,
                    f"{x.title}\n{x.category}\n{x.description}",
                ),
                x,
            )
            for x in self._commerce_catalog.store.active()
        ]
        scored = [(s, x) for s, x in scored if s > 0.05]
        scored.sort(key=lambda pair: (-pair[0], pair[1].listing_id))
        return [x for _, x in scored[: int(limit)]]

    # ------------------------------------------------------------------ #
    # Life books: one shared function, one private book per member.       #
    # ------------------------------------------------------------------ #
    def _require_books(self):
        if self._files is None:
            raise GatewayError(404, "not_found", "this host keeps no drawer")
        from .. import lifebooks

        return lifebooks

    def _life_books(self, request, session, params) -> Response:
        """Every book at one glance: kind, title, the stable pointer,
        and how many rows the member's own file holds."""
        books = self._require_books()
        return json_response(
            200,
            {
                "official_owner": books.OFFICIAL_OWNER,
                "items": [
                    {
                        "kind": b.kind,
                        "title": b.title,
                        "unit": b.unit,
                        "pointer": books.pointer(b.kind),
                        "rows": len(
                            books.read_rows(
                                self._files,
                                tenant=session.tenant_id,
                                owner=session.principal_id,
                                kind=b.kind,
                            )
                        ),
                    }
                    for b in books.BOOKS.values()
                ],
            },
        )

    def _life_books_import(self, request, session, params) -> Response:
        """Everything the prebuilt nodes documented, imported into the
        member's OWN Life/Files books — reminders, calendar events, and
        standing automation triggers today; idempotent by dedup."""
        books = self._require_books()
        now = request.now or self._clock()
        imported: dict[str, int] = {}
        if self._reminders is not None:
            rows = [
                {
                    "at": r.due_at.isoformat(),
                    "label": r.text[:120],
                    "value": None,
                    "note": "reminder",
                }
                for r in self._reminders.upcoming(
                    tenant=session.tenant_id,
                    principal=session.principal_id,
                    now=now,
                )
            ]
            imported["reminder"] = books.append_rows(
                self._files,
                tenant=session.tenant_id,
                owner=session.principal_id,
                kind="reminder",
                rows=rows,
            )
        events = self._calendar.between(
            tenant=session.tenant_id,
            owner=session.principal_id,
            start=now - timedelta(days=365),
            end=now + timedelta(days=365),
        )
        imported["calendar"] = books.append_rows(
            self._files,
            tenant=session.tenant_id,
            owner=session.principal_id,
            kind="calendar",
            rows=[
                {
                    "at": e.starts_at.isoformat(),
                    "label": e.title[:120],
                    "value": None,
                    "note": e.source,
                }
                for e in events
            ],
        )
        imported["automation"] = books.append_rows(
            self._files,
            tenant=session.tenant_id,
            owner=session.principal_id,
            kind="automation",
            rows=[
                {
                    "at": "",
                    "label": (s.label or s.goal)[:120],
                    "value": None,
                    "note": f"{s.cadence} trigger: {s.goal}"[:200],
                }
                for s in self._pulse.list_for(
                    session.tenant_id, session.principal_id
                )
            ],
        )
        return json_response(200, {"imported": imported})

    def _life_book_rows(self, request, session, params) -> Response:
        books = self._require_books()
        kind = params["kind"]
        if kind not in books.BOOKS:
            raise GatewayError(404, "not_found", f"no such book: {kind}")
        return json_response(
            200,
            {
                "kind": kind,
                "pointer": books.pointer(kind),
                "rows": books.read_rows(
                    self._files,
                    tenant=session.tenant_id,
                    owner=session.principal_id,
                    kind=kind,
                ),
            },
        )

    def _life_book_chart(self, request, session, params) -> Response:
        books = self._require_books()
        kind = params["kind"]
        if kind not in books.BOOKS:
            raise GatewayError(404, "not_found", f"no such book: {kind}")
        rows = books.read_rows(
            self._files,
            tenant=session.tenant_id,
            owner=session.principal_id,
            kind=kind,
        )
        book = books.BOOKS[kind]
        return json_response(
            200,
            {
                "kind": kind,
                "title": book.title,
                "unit": book.unit,
                "points": books.chart_points(kind, rows),
            },
        )

    def _book_command(self, session, message):
        """OoLu's own conversation shows a book on demand — "show
        cashflow", "chart my stock" — as a CHART BLOCK in the bubble:
        the member's data, drawn from their own book, never invented."""
        if self._files is None:
            return None
        from .. import lifebooks

        normal = re.sub(
            r"\s+", " ", str(message or "").strip().casefold()
        ).rstrip(".!?")
        for verb in ("show", "chart", "graph"):
            if normal.startswith(f"{verb} "):
                asked = normal[len(verb) + 1 :].removeprefix("my ").strip()
                key = asked.replace(" ", "_")
                if key in lifebooks.BOOKS:
                    rows = lifebooks.read_rows(
                        self._files,
                        tenant=session.tenant_id,
                        owner=session.principal_id,
                        kind=key,
                    )
                    book = lifebooks.BOOKS[key]
                    if not rows:
                        return (
                            f"Your {book.title} book is empty — say "
                            "nothing was lost: it fills as the node "
                            "documents, and “import my books” pulls in "
                            "what already exists.",
                            None,
                        )
                    return (
                        f"{book.title} — {len(rows)} row"
                        f"{'s' if len(rows) != 1 else ''}, from your own "
                        "Life/Files book.",
                        {
                            "kind": "chart",
                            "title": book.title,
                            "unit": book.unit,
                            "points": lifebooks.chart_points(key, rows),
                        },
                    )
        return None

    def _commerce_search(self, request, session, params) -> Response:
        query = str(request.query.get("q") or "").strip()
        if not query:
            raise GatewayError(400, "invalid_request", "q is required")
        return json_response(
            200,
            {
                "items": [
                    x.model_dump(mode="json")
                    for x in self._closest_listings(query)
                ]
            },
        )

    def _followable_categories(self, session, now) -> list[dict]:
        from ..explorer import EXPLORER_BRIEF_PREFIX

        categories = {
            x.category
            for x in self._commerce_catalog.store.active()
            if x.category
        } | {
            r.specification.category
            for r in self._commerce_rfq.open_requests(
                tenant=session.tenant_id, now=now
            )
            if r.specification.category
        }
        followed = {
            s.goal[len(EXPLORER_BRIEF_PREFIX) :].rsplit(":", 1)[0]
            for s in self._pulse.list_for(
                session.tenant_id, session.principal_id
            )
            if s.goal.startswith(EXPLORER_BRIEF_PREFIX)
        }
        return [
            {"category": c, "followed": c in followed}
            for c in sorted(categories)
        ]

    def _explorer_categories(self, request, session, params) -> Response:
        return json_response(
            200,
            {
                "items": self._followable_categories(
                    session, request.now or self._clock()
                )
            },
        )

    _EXPLORER_CATEGORY_ASKS = frozenset(
        {"categories", "follow", "interests", "what can i follow"}
    )

    def _explorer_desk_turn(self, session, message, now):
        """The Explorer desk's hand on one thread message — or None for
        ordinary conversation. The categories to follow arrive as a
        MESSAGE BLOCK (a chip's tap speaks "follow …" back); "follow
        {category}" lays the standing daily brief; any other words find
        the CLOSEST existing products and open a comparison with an
        inferred lens and a real deadline — decisions end, by design."""
        from ..explorer import (
            DECISION_TTL_HOURS,
            EXPLORER_BRIEF_PREFIX,
            infer_mode,
        )

        normal = re.sub(
            r"\s+", " ", str(message or "").strip().casefold()
        ).rstrip(".!?")
        if normal in self._EXPLORER_CATEGORY_ASKS:
            items = self._followable_categories(session, now)
            if not items:
                return (
                    "Nothing exists to follow yet — categories appear as "
                    "real listings and requests are created.",
                    None,
                    "desk",
                )
            return (
                "These categories exist right now — tap one and I follow "
                "it: the brief arrives here daily.",
                None,
                "desk",
                {"kind": "categories", "items": items},
            )
        if normal.startswith("follow "):
            category = normal[len("follow ") :].strip()
            known = {
                c["category"].casefold(): c["category"]
                for c in self._followable_categories(session, now)
            }
            if category not in known:
                return (
                    f"No listing or request carries “{category}” yet — "
                    "say “categories” to see what exists to follow.",
                    None,
                    "desk",
                )
            mode = infer_mode(
                message,
                last_mode=self._comparisons().last_decided_mode(
                    tenant=session.tenant_id,
                    principal=session.principal_id,
                ),
            )
            goal = f"{EXPLORER_BRIEF_PREFIX}{known[category]}:{mode}"
            if not any(
                s.goal == goal
                for s in self._pulse.list_for(
                    session.tenant_id, session.principal_id
                )
            ):
                self._pulse.add(
                    session.tenant_id,
                    session.principal_id,
                    cadence="daily",
                    at_minute=9 * 60,
                    goal=goal,
                    tz_offset_minutes=0,
                    label=f"Explorer brief: {known[category]}",
                    now=now,
                )
            return (
                f"Following “{known[category]}” — the {mode} brief "
                "arrives in this thread daily.",
                None,
                "desk",
            )
        if normal in ("decide", "decided", "done", "stop comparing"):
            closed = self._comparisons().close(
                tenant=session.tenant_id, principal=session.principal_id
            )
            return (
                "Marked decided — the comparison is closed."
                if closed
                else "No comparison stands open.",
                None,
                "desk",
            )
        # Anything else with substance is a product search — but only
        # when the shelf actually holds something close; a question the
        # shelf cannot answer stays a conversation.
        if len(normal) < 3:
            return None
        hits = self._closest_listings(message)
        if not hits:
            return None
        comparisons = self._comparisons()
        mode = infer_mode(
            message,
            last_mode=comparisons.last_decided_mode(
                tenant=session.tenant_id, principal=session.principal_id
            ),
        )
        opened = comparisons.open(
            tenant=session.tenant_id,
            principal=session.principal_id,
            query=message,
            mode=mode,
            listing_ids=[x.listing_id for x in hits],
        )
        return (
            f"The closest existing products, compared through your "
            f"{mode} lens (read from your words and history — no menu). "
            f"This comparison stays open {DECISION_TTL_HOURS} hours and "
            "then lapses on its own: say “decide” when you've chosen, "
            "or let it expire — no decision debt.",
            None,
            "desk",
            {
                "kind": "products",
                "items": [x.model_dump(mode="json") for x in hits],
                "mode": mode,
                "expires_at": opened["expires_at"],
            },
        )

    # ------------------------------------------------------------------ #
    # The byline: a published face and name, tenant-scoped.               #
    # ------------------------------------------------------------------ #
    def _profile_principal(self, session, username: str) -> str:
        """The named account must exist, enabled, in the caller's own
        tenant — your own name included (a byline preview is yourself)."""
        username = str(username or "").strip()
        if username == session.principal_id:
            return username
        account = (
            self._accounts.user(username) if self._accounts is not None else None
        )
        if (
            account is None
            or account.tenant_id != session.tenant_id
            or account.disabled
        ):
            raise GatewayError(404, "not_found", "no one by that name here")
        return username

    def _profile_get(self, request, session, params) -> Response:
        """The byline's words: display name (the account's own setting)
        and whether a photo exists — enough to render an attribution."""
        username = self._profile_principal(session, params["username"])
        display_name = ""
        if self._settings is not None:
            effective = self._settings.effective(session.tenant_id, username)
            display_name = str(effective.get("account.display_name", "") or "")
        has_photo = bool(
            self._profile_photos is not None
            and self._profile_photos.get(
                tenant=session.tenant_id, principal=username
            )
        )
        return json_response(
            200,
            {
                "username": username,
                "display_name": display_name,
                "has_photo": has_photo,
            },
        )

    def _profile_photo_get(self, request, session, params) -> Response:
        """The photo's true bytes, typed honestly — any signed-in member
        of the same tenant may look, because that is what a byline is."""
        username = self._profile_principal(session, params["username"])
        if self._profile_photos is None:
            raise GatewayError(404, "not_found", "this host keeps no photos")
        found = self._profile_photos.get(
            tenant=session.tenant_id, principal=username
        )
        if found is None:
            raise GatewayError(404, "not_found", "no photo on this account")
        media_type, data = found
        return Response(status=200, body=data, content_type=media_type)

    def _profile_photo_put(self, request, session, params) -> Response:
        """Set YOUR photo: the body is the image, exactly as picked (the
        files-upload discipline — no base64 inflation on the wire)."""
        if self._profile_photos is None:
            raise GatewayError(404, "not_found", "this host keeps no photos")
        data = request.raw
        if not data:
            raise GatewayError(
                400, "invalid_request", "the body is the photo — it is empty"
            )
        if len(data) > MAX_PHOTO_BYTES:
            raise GatewayError(
                413,
                "too_large",
                f"a profile photo is at most {MAX_PHOTO_BYTES} bytes",
            )
        media_type = str(request.header("content-type") or "")
        try:
            self._profile_photos.save(
                tenant=session.tenant_id,
                principal=session.principal_id,
                media_type=media_type,
                data=data,
            )
        except ValueError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        return json_response(
            201,
            {
                "username": session.principal_id,
                "media_type": media_type.split(";")[0].strip().lower(),
                "size": len(data),
            },
        )

    def _profile_photo_delete(self, request, session, params) -> Response:
        if self._profile_photos is None:
            raise GatewayError(404, "not_found", "this host keeps no photos")
        removed = self._profile_photos.remove(
            tenant=session.tenant_id, principal=session.principal_id
        )
        return json_response(200, {"removed": bool(removed)})

    # ------------------------------------------------------------------ #
    # The press (A1): publish, attribute, weigh.                          #
    # ------------------------------------------------------------------ #
    def _require_press(self):
        if self._press is None:
            raise GatewayError(404, "not_found", "this host keeps no press")
        return self._press

    @staticmethod
    def _press_refused(exc) -> GatewayError:
        code = {404: "not_found", 403: "forbidden"}.get(
            exc.status, "invalid_request"
        )
        return GatewayError(exc.status, code, str(exc))

    @staticmethod
    def _contribution_dict(record) -> dict:
        return {
            "contribution_id": record.contribution_id,
            "author": record.author,  # the Byline resolves face + name
            "title": record.title,
            "body": record.body,
            "genres": list(record.genres),
            "license": record.license,
            "media": [
                {
                    "file_id": m.file_id,
                    "media_type": m.media_type,
                    "name": m.name,
                    "blob_ref": m.blob_ref,
                }
                for m in record.media
            ],
            "similar_to": record.similar_to,
            "similarity": record.similarity,
            "taxonomy_version": record.taxonomy_version,
            "created_at": record.created_at.isoformat(),
            "superseded_at": (
                record.superseded_at.isoformat()
                if record.superseded_at
                else None
            ),
        }

    def _press_genres(self, request, session, params) -> Response:
        """The taxonomy, versioned, and the stated licenses — everything
        the contribute picker needs to render consent honestly."""
        self._require_press()
        from ..press import LICENSES, TAXONOMY_VERSION, taxonomy_items

        return json_response(
            200,
            {
                "taxonomy_version": TAXONOMY_VERSION,
                "items": taxonomy_items(),
                "licenses": [
                    {"key": lic.key, "name": lic.name, "terms": lic.terms}
                    for lic in LICENSES.values()
                ],
            },
        )

    def _press_list(self, request, session, params) -> Response:
        """Live contributions, newest first — the tenant's shelf. `mine=1`
        narrows to the caller's own (their resting records included, so
        an author always sees what they unpublished)."""
        press = self._require_press()
        mine = str(request.query.get("mine") or "") in ("1", "true")
        genre = str(request.query.get("genre") or "") or None
        limit = min(int(request.query.get("limit") or 50), 200)
        items = press.store.list(
            tenant=session.tenant_id,
            genre=genre,
            author=session.principal_id if mine else None,
            limit=limit,
            include_superseded=mine,
        )
        return json_response(
            200, {"items": [self._contribution_dict(r) for r in items]}
        )

    def _drawer_refs(self, session, file_ids, *, missing="refuse"):
        """Drawer files as press MediaRefs, the wall held at the door:
        another account's file — or a node's — is indistinguishable from
        a missing one. ``missing="refuse"`` is the loud 404 the publish
        door keeps; ``missing="drop"`` leaves the gone ones out (the
        intake's publish moment, where the reply names the drop)."""
        from ..press import MediaRef

        media: list[MediaRef] = []
        for file_id in file_ids:
            file = (
                self._files.get(str(file_id), tenant=session.tenant_id)
                if self._files is not None
                else None
            )
            if file is not None and file.node_id is None:
                if file.owner not in ("", session.principal_id):
                    file = None
            elif file is not None:
                file = None
            if file is None:
                if missing == "drop":
                    continue
                raise GatewayError(
                    404, "not_found", f"no such file: {file_id}"
                )
            media.append(
                MediaRef(
                    file_id=file.file_id,
                    blob_ref=file.blob_ref or "",
                    media_type=file.media_type or "",
                    name=file.name,
                )
            )
        return media

    def _press_publish(self, request, session, params) -> Response:
        """The publication gate, walked in order — refusals are loud and
        name the fix. Media rides as drawer REFS (never copies): each
        named file must be the caller's own at publish time."""
        press = self._require_press()
        from ..press import PressError

        body = request.body or {}
        file_ids = body.get("file_ids") or []
        if not isinstance(file_ids, list):
            raise GatewayError(400, "invalid_request", "file_ids must be a list")
        media = self._drawer_refs(session, file_ids)
        try:
            record = press.publish(
                tenant=session.tenant_id,
                author=session.principal_id,
                title=str(body.get("title") or ""),
                body=str(body.get("body") or ""),
                genres=body.get("genres") or [],
                license=str(body.get("license") or ""),
                consent=body.get("consent") is True,
                media=media,
            )
        except PressError as exc:
            raise self._press_refused(exc) from exc
        # The provenance row: publication is a public act and lands on
        # the tamper-evident chain (leaving conversation privacy alone).
        self._durable.audit.append(
            "press.contribution_published",
            {
                "contribution_id": record.contribution_id,
                "author": record.author,
                "genres": list(record.genres),
                "license": record.license,
                "similar_to": record.similar_to,
            },
        )
        return json_response(201, self._contribution_dict(record))

    def _press_detail(self, request, session, params) -> Response:
        """A live contribution — or the author's own resting one: the
        author always sees their record; everyone else sees only what
        stands."""
        press = self._require_press()
        record = press.store.get(
            params["contribution_id"],
            tenant=session.tenant_id,
            include_superseded=True,
        )
        if record is None or (
            record.superseded_at is not None
            and record.author != session.principal_id
        ):
            raise GatewayError(404, "not_found", "no such contribution")
        return json_response(200, self._contribution_dict(record))

    def _press_unpublish(self, request, session, params) -> Response:
        press = self._require_press()
        from ..press import PressError

        try:
            record = press.unpublish(
                params["contribution_id"],
                tenant=session.tenant_id,
                author=session.principal_id,
            )
        except PressError as exc:
            raise self._press_refused(exc) from exc
        self._durable.audit.append(
            "press.contribution_unpublished",
            {
                "contribution_id": record.contribution_id,
                "author": record.author,
            },
        )
        return json_response(200, self._contribution_dict(record))

    # -- the newsroom (A2) --------------------------------------------- #
    def _require_newsroom(self):
        press = self._require_press()
        if press.stories is None:
            raise GatewayError(
                404, "not_found", "this host keeps no newsroom"
            )
        return press

    def _story_dict(self, story) -> dict:
        # The rubric's factor breakdown stays recorded (durable, on the
        # audit trail) but never renders to the member: the scoring is
        # the house's own working — the order speaks for itself.
        payload = {
            "story_id": story.story_id,
            "headline": story.headline,
            "prose": story.prose,
            "genres": list(story.genres),
            # Every cited contributor's byline, weights and all — the
            # attribution set the dividend (A5) will split over.
            "lineage": [
                {
                    "contribution_id": s.contribution_id,
                    "author": s.author,
                    "weight": s.weight,
                }
                for s in story.lineage
            ],
            "rubric_version": story.rubric_version,
            "source": story.source,
            "created_at": story.created_at.isoformat(),
            "media": [],
        }
        # The lineage's attached media, addressable through the press
        # media door — refs, never copies: a contribution the author has
        # since unpublished honestly drops out of the strip.
        press = self._press
        if press is not None:
            for share in story.lineage:
                record = press.store.get(
                    share.contribution_id, tenant=story.tenant_id
                )
                if record is None:
                    continue
                for index, ref in enumerate(record.media):
                    payload["media"].append(
                        {
                            "contribution_id": record.contribution_id,
                            "index": index,
                            "media_type": ref.media_type,
                            "name": ref.name,
                        }
                    )
        return payload

    def _press_personalized(self, session) -> bool:
        """The consent switch, read where it is enforced: personalization
        exists only while `press.personalize` is on."""
        if self._settings is None:
            return False
        effective = self._settings.effective(
            session.tenant_id, session.principal_id
        )
        return effective.get("press.personalize") is True

    def _member_taste(self, session, press):
        """The member's semantic taste: their taps (liked and skipped
        story texts) plus their OWN words from the OoLu and News threads
        — never the assistant's. Gathered only under the one consent
        switch (the caller checks it); None when there is nothing to
        lean on, so the edition stays honestly neutral."""
        if press.preferences is None:
            return None
        spoken: list[str] = []
        if self._assistant_history is not None:
            for agent in ("oolu", "news"):
                for turn in self._assistant_history.history(
                    tenant=session.tenant_id,
                    principal=session.principal_id,
                    limit=100,
                    agent=agent,
                ):
                    if turn["kind"] == "user" and turn["body"]:
                        spoken.append(turn["body"])
        taste = press.preferences.taste(
            tenant=session.tenant_id,
            principal=session.principal_id,
            spoken=spoken,
        )
        return taste or None

    def _edition_schedule_row(self, session):
        from ..press import EDITION_PULSE_GOAL

        for schedule in self._pulse.list_for(
            session.tenant_id, session.principal_id
        ):
            if schedule.goal == EDITION_PULSE_GOAL:
                return schedule
        return None

    def _press_stories(self, request, session, params) -> Response:
        """The caller's edition: neutral rubric order for everyone; bent
        only under the member's own consent — by genre affinity AND by
        semantic taste (their taps, and their own words in the OoLu and
        News threads), with the serendipity slice standing."""
        from ..press import EDITION_SIZE, rank_edition

        press = self._require_newsroom()
        size = min(int(request.query.get("limit") or EDITION_SIZE), 20)
        stories = press.stories.list(tenant=session.tenant_id, limit=100)
        personalized = self._press_personalized(session)
        affinity = None
        taste = None
        if personalized and press.preferences is not None:
            affinity = press.preferences.genre_affinity(
                tenant=session.tenant_id, principal=session.principal_id
            )
            taste = self._member_taste(session, press)
        edition = rank_edition(
            stories, affinity=affinity or None, taste=taste, size=size
        )
        schedule = self._edition_schedule_row(session)
        return json_response(
            200,
            {
                "items": [self._story_dict(s) for s in edition],
                "personalized": bool(affinity) or taste is not None,
                "edition_schedule": (
                    self._pulse_row(schedule, request.now or self._clock())
                    if schedule is not None
                    else None
                ),
            },
        )

    def _press_story_detail(self, request, session, params) -> Response:
        press = self._require_newsroom()
        story = press.stories.get(
            params["story_id"], tenant=session.tenant_id
        )
        if story is None:
            raise GatewayError(404, "not_found", "no such story")
        return json_response(200, self._story_dict(story))

    def _press_story_feedback(self, request, session, params) -> Response:
        """One tap, honestly handled: recorded only under the member's
        own consent — and the answer says which it was. The tap keeps
        the story's own words next to the signal, so it adjusts the
        member's SEMANTIC taste, not just their genre leaning."""
        from ..press import taste_snippet

        press = self._require_newsroom()
        story = press.stories.get(
            params["story_id"], tenant=session.tenant_id
        )
        if story is None:
            raise GatewayError(404, "not_found", "no such story")
        signal = str((request.body or {}).get("signal") or "").strip()
        if signal not in ("like", "read", "skip"):
            raise GatewayError(
                400, "invalid_request", "signal must be like, read, or skip"
            )
        if not self._press_personalized(session) or press.preferences is None:
            return json_response(
                200, {"recorded": False, "reason": "personalization is off"}
            )
        press.preferences.record(
            tenant=session.tenant_id,
            principal=session.principal_id,
            signal=signal,
            subject=f"story:{story.story_id}",
            genres=story.genres,
            snippet=taste_snippet(story.headline, story.prose),
        )
        return json_response(200, {"recorded": True})

    def _press_newsroom_run(self, request, session, params) -> Response:
        """The editor's crank: judge the shelf, tell the untold. The
        composition speaks through the news.compose seat when this
        tenant has a brain; the desk composes otherwise."""
        from ..press import Newsroom

        press = self._require_newsroom()
        newsroom = Newsroom(press.store, press.stories)
        model = self._seat_actor(
            self._tenant_model(session.tenant_id, purpose="news.compose"),
            session.principal_id,
        )
        composed = newsroom.run(tenant=session.tenant_id, model=model)
        for story in composed:
            self._durable.audit.append(
                "press.story_composed",
                {
                    "story_id": story.story_id,
                    "source": story.source,
                    "rubric_version": story.rubric_version,
                    "lineage": [
                        {"contribution_id": s.contribution_id, "weight": s.weight}
                        for s in story.lineage
                    ],
                },
            )
        return json_response(
            200,
            {
                "composed": len(composed),
                "items": [self._story_dict(s) for s in composed],
            },
        )

    def _press_edition_schedule(self, request, session, params) -> Response:
        """The member's morning-edition rhythm: one standing pulse
        schedule with the edition sentinel goal — created, retimed, or
        removed through this one door."""
        from ..press import EDITION_LABEL, EDITION_PULSE_GOAL

        self._require_newsroom()
        body = request.body or {}
        standing = self._edition_schedule_row(session)
        if body.get("enabled") is False:
            if standing is not None:
                self._pulse.delete(
                    standing.schedule_id,
                    tenant=session.tenant_id,
                    principal=session.principal_id,
                )
            return json_response(200, {"edition_schedule": None})
        at_minute = int(body.get("at_minute", 8 * 60))
        tz_offset = _tz_minutes(body.get("tz_offset_minutes"))
        if standing is not None:
            self._pulse.delete(
                standing.schedule_id,
                tenant=session.tenant_id,
                principal=session.principal_id,
            )
        try:
            schedule = self._pulse.add(
                session.tenant_id,
                session.principal_id,
                cadence="daily",
                at_minute=at_minute,
                goal=EDITION_PULSE_GOAL,
                tz_offset_minutes=tz_offset,
                label=EDITION_LABEL,
                now=request.now or self._clock(),
            )
        except ValueError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        return json_response(
            200,
            {
                "edition_schedule": self._pulse_row(
                    schedule, request.now or self._clock()
                )
            },
        )

    def _fire_edition(self, schedule, occurrence: str, skipped: int) -> None:
        """The edition fires: compose what is untold, rank for THIS
        member under THEIR consent, and land the edition as the News
        agent's own thread message — with a short reminder ping so the
        standing poll surfaces it. Failures are audited, never raised
        into the serving request."""
        from types import SimpleNamespace

        from ..press import Newsroom, edition_message, rank_edition

        press = self._press
        if press is None or press.stories is None:
            return
        session = SimpleNamespace(
            tenant_id=schedule.tenant, principal_id=schedule.principal
        )
        try:
            model = self._seat_actor(
                self._tenant_model(schedule.tenant, purpose="news.compose"),
                schedule.principal,
            )
            Newsroom(press.store, press.stories).run(
                tenant=schedule.tenant, model=model
            )
            stories = press.stories.list(tenant=schedule.tenant, limit=100)
            affinity = None
            taste = None
            if (
                self._press_personalized(session)
                and press.preferences is not None
            ):
                affinity = press.preferences.genre_affinity(
                    tenant=schedule.tenant, principal=schedule.principal
                )
                taste = self._member_taste(session, press)
            edition = rank_edition(
                stories, affinity=affinity or None, taste=taste
            )
            message = edition_message(edition, skipped=skipped)
            if self._assistant_history is not None:
                self._assistant_history.append(
                    tenant=schedule.tenant,
                    principal=schedule.principal,
                    kind="assistant",
                    body=message,
                    agent="news",
                )
            if self._reminders is not None and edition:
                try:
                    self._reminders.add(
                        tenant=schedule.tenant,
                        principal=schedule.principal,
                        text=(
                            f"Your edition is ready — {len(edition)} "
                            "stories in the News thread."
                        )[:490],
                        due_at=(self._clock() + timedelta(minutes=2)),
                    )
                except ValueError:
                    pass  # a full reminder book is not a failed edition
            self._durable.audit.append(
                "pulse.fired",
                {
                    "schedule_id": schedule.schedule_id,
                    "occurrence": occurrence,
                    "run_id": "",
                    "skipped": skipped,
                    "tenant": schedule.tenant,
                    "principal": schedule.principal,
                    "goal": schedule.goal,
                },
            )
        except Exception:  # noqa: BLE001 - the tick must keep serving
            logging.getLogger("oolu.gateway").exception(
                "edition pulse failed for %s", schedule.schedule_id
            )
            self._durable.audit.append(
                "pulse.fire_failed",
                {
                    "schedule_id": schedule.schedule_id,
                    "occurrence": occurrence,
                    "tenant": schedule.tenant,
                    "principal": schedule.principal,
                    "goal": schedule.goal,
                    "code": "edition_error",
                    "reason": "the edition fire raised — the log has the story",
                },
            )

    # -- the poll floor (A3) ------------------------------------------- #
    def _require_polls(self):
        press = self._require_press()
        if press.polls is None:
            raise GatewayError(404, "not_found", "this host keeps no polls")
        return press

    @staticmethod
    def _pair_dict(pair) -> dict:
        return {
            "pair_id": pair.pair_id,
            "genre": pair.genre,
            "left": pair.left.model_dump(),
            "right": pair.right.model_dump(),
            "created_at": pair.created_at.isoformat(),
        }

    def _press_poll_next(self, request, session, params) -> Response:
        """A pair this member has not voted on — in their named genre,
        or the Thompson draw's pick when they name none. The switch is
        immediate: the genre parameter IS the stream."""
        press = self._require_polls()
        from ..press import GENRES, PressError

        genre = str(request.query.get("genre") or "") or None
        if genre is not None and genre not in GENRES:
            raise GatewayError(400, "invalid_request", f"unknown genre: {genre}")
        corpus = press.store.list(tenant=session.tenant_id, limit=500)
        try:
            pair = press.polls.next_pair(
                tenant=session.tenant_id,
                principal=session.principal_id,
                corpus=corpus,
                genre=genre,
            )
        except PressError as exc:
            raise self._press_refused(exc) from exc
        return json_response(200, self._pair_dict(pair))

    def _press_poll_vote(self, request, session, params) -> Response:
        """One idempotent vote. The aggregate always counts it; the
        preference event is written only under the member's own
        consent — and their edition feels it (the engagement signal)."""
        press = self._require_polls()
        from ..press import PressError

        choice = str((request.body or {}).get("choice") or "").strip()
        learning = self._press_personalized(session)
        try:
            verdict = press.polls.vote(
                params["pair_id"],
                tenant=session.tenant_id,
                principal=session.principal_id,
                choice=choice,
                learning=learning,
            )
        except PressError as exc:
            raise self._press_refused(exc) from exc
        # The consented vote also feeds the member's OWN edition ranking
        # — a vote is engagement with the genre, recorded as such.
        if learning and press.preferences is not None:
            pair = press.polls.store.get_pair(
                params["pair_id"], tenant=session.tenant_id
            )
            if pair is not None:
                press.preferences.record(
                    tenant=session.tenant_id,
                    principal=session.principal_id,
                    signal="read",
                    subject=f"poll:{pair.pair_id}",
                    genres=(pair.genre,),
                )
        verdict["learning"] = bool(learning)
        # The vote just grew the evidence: the scientist re-judges the
        # floor, and a NEW verdict (or a flipped one) is reported into
        # the Poll thread — worth sharing, like a news brief or a
        # debate statement; the same conclusion twice stays silent.
        for report in self._poll_science_reports(session):
            if self._assistant_history is not None:
                self._assistant_history.append(
                    tenant=session.tenant_id,
                    principal=session.principal_id,
                    kind="assistant",
                    body=report,
                    agent="poll",
                )
        return json_response(200, verdict)

    def _poll_findings_store(self):
        if self._poll_findings is None:
            from ..press import FindingStore

            self._poll_findings = FindingStore(self._durable.conn)
        return self._poll_findings

    def _poll_science_judge(self, session):
        """The floor judged now: findings worth words (pattern or
        debate), over decided k-anonymous comparisons only."""
        from ..press import judge

        press = self._press
        polls = press.polls.store
        findings, _ = judge(
            polls.all_pairs(tenant=session.tenant_id),
            counts_of=lambda pid: polls.counts(pid, tenant=session.tenant_id),
            contribution_of=lambda cid: press.store.get(
                cid, tenant=session.tenant_id
            ),
        )
        return findings

    def _poll_science_reports(self, session) -> list[str]:
        """The NEWLY newsworthy: findings whose verdict just opened or
        flipped — each reported once, the field-note words."""
        press = self._press
        if press is None or press.polls is None:
            return []
        store = self._poll_findings_store()
        reports: list[str] = []
        for finding in self._poll_science_judge(session):
            if store.note(tenant=session.tenant_id, finding=finding):
                reports.append(finding.report())
        return reports

    def _press_poll_stats(self, request, session, params) -> Response:
        press = self._require_polls()
        from ..press import PressError

        try:
            verdict = press.polls.reveal(
                params["pair_id"],
                tenant=session.tenant_id,
                principal=session.principal_id,
            )
        except PressError as exc:
            raise self._press_refused(exc) from exc
        return json_response(200, verdict)

    def _press_preferences_export(self, request, session, params) -> Response:
        """The member's own pairwise preferences in the DPO dataset
        shape — consent-gated, per-member, scrubbed on the way out."""
        press = self._require_polls()
        if press.polls.pairwise is None:
            raise GatewayError(
                404, "not_found", "this host keeps no preference pairs"
            )
        if not self._press_personalized(session):
            raise GatewayError(
                403,
                "forbidden",
                "personalization is off — there is nothing consented to export",
            )
        pairs = press.polls.pairwise.export(
            tenant=session.tenant_id, principal=session.principal_id
        )
        return json_response(200, {"items": pairs, "count": len(pairs)})

    # -- the ad house (A4) --------------------------------------------- #
    @staticmethod
    def _adhouse_refused(exc) -> GatewayError:
        code = {404: "not_found", 403: "forbidden", 429: "rate_limited"}.get(
            exc.status, "invalid_request"
        )
        return GatewayError(exc.status, code, str(exc))

    def _legal_consent_get(self, request, session, params) -> Response:
        from ..legal import LEGAL_VERSIONS

        accepted = self._legal_acceptances.accepted_version(
            tenant=session.tenant_id,
            principal=session.principal_id,
            document="privacy",
        )
        return json_response(
            200,
            {
                "privacy_version": LEGAL_VERSIONS["privacy"],
                "accepted_version": accepted,
                # Ads stand behind the CURRENT version — the conservative
                # default for pre-amendment accounts (decision-log item 2
                # resolved as default-off until explicit acceptance).
                "ads_enabled": self._ads_consented(session),
            },
        )

    def _legal_consent_post(self, request, session, params) -> Response:
        """Accept the CURRENT version, named explicitly — accepting a
        version you have not read is not a flow this door offers."""
        from ..legal import LEGAL_VERSIONS

        body = request.body or {}
        document = str(body.get("document") or "privacy")
        if document not in LEGAL_VERSIONS:
            raise GatewayError(400, "invalid_request", "unknown document")
        version = body.get("version")
        if version != LEGAL_VERSIONS[document]:
            raise GatewayError(
                409,
                "conflict",
                f"the current {document} version is "
                f"{LEGAL_VERSIONS[document]} — read it and accept that one",
            )
        standing = self._legal_acceptances.accept(
            tenant=session.tenant_id,
            principal=session.principal_id,
            document=document,
            version=int(version),
        )
        self._durable.audit.append(
            "legal.accepted",
            {
                "principal": session.principal_id,
                "document": document,
                "version": int(version),
            },
        )
        return json_response(
            200, {"document": document, "accepted_version": standing}
        )

    def _ads_consented(self, session) -> bool:
        return self._legal_acceptances.is_current(
            tenant=session.tenant_id,
            principal=session.principal_id,
            document="privacy",
        )

    def _require_advertiser(self, session) -> None:
        """WHO may buy attention: a seller-KYC-verified principal — the
        marketplace's standing trust gate, reused whole."""
        if not self._commerce_seller_kyc.is_verified(
            tenant=session.tenant_id, principal=session.principal_id
        ):
            raise GatewayError(
                403,
                "forbidden",
                "advertising needs a verified seller identity — apply at "
                "/v1/commerce/seller/kyc",
            )

    @staticmethod
    def _campaign_dict(campaign) -> dict:
        return {
            "campaign_id": campaign.campaign_id,
            "advertiser": campaign.advertiser,
            "name": campaign.name,
            "creative": campaign.creative,
            "offer_ref": campaign.offer_ref,
            "genres": list(campaign.genres),
            "bid_micros": campaign.bid_micros,
            "funded_micros": campaign.funded_micros,
            # Display-only recognition — the ledger holds the liability.
            "charged_micros_preview": campaign.charged_micros,
            "remaining_micros_preview": campaign.remaining_micros,
            "status": campaign.status,
            "flight_start": campaign.flight_start.isoformat(),
            "flight_end": campaign.flight_end.isoformat(),
        }

    def _adhouse_campaign_create(self, request, session, params) -> Response:
        """Fund a campaign: the whole gate in one door — verified seller,
        taxonomy targeting, scrub-gated creative, and the funding posted
        on the double-entry book as cash against standing liability."""
        from ..adhouse import MAX_CREATIVE_CHARS, MIN_BID_MICROS
        from ..press import GENRES, leak_report

        self._require_advertiser(session)
        body = request.body or {}
        name = str(body.get("name") or "").strip()
        creative = str(body.get("creative") or "").strip()
        offer_ref = str(body.get("offer_ref") or "").strip()
        genres = tuple(
            str(g).strip() for g in (body.get("genres") or []) if str(g).strip()
        )
        bid = int(body.get("bid_micros") or 0)
        budget = int(body.get("budget_micros") or 0)
        days = int(body.get("flight_days") or 30)
        if not name:
            raise GatewayError(400, "invalid_request", "a campaign needs a name")
        if not creative:
            raise GatewayError(400, "invalid_request", "a campaign needs its words")
        if len(creative) > MAX_CREATIVE_CHARS:
            raise GatewayError(
                400,
                "invalid_request",
                f"the creative is an ad, not an article (cap "
                f"{MAX_CREATIVE_CHARS} chars)",
            )
        leaks = leak_report(f"{name}\n{creative}")
        if leaks:
            raise GatewayError(
                400,
                "invalid_request",
                "the creative would leak " + ", ".join(leaks),
            )
        if not offer_ref:
            raise GatewayError(
                400,
                "invalid_request",
                "a campaign points at a marketplace offer (offer_ref)",
            )
        if not genres:
            raise GatewayError(400, "invalid_request", "pick target genres")
        for key in genres:
            if key not in GENRES:
                raise GatewayError(
                    400, "invalid_request", f"unknown genre: {key}"
                )
        if bid < MIN_BID_MICROS:
            raise GatewayError(
                400,
                "invalid_request",
                f"the bid floor is {MIN_BID_MICROS} micros per impression",
            )
        if budget < bid:
            raise GatewayError(
                400, "invalid_request", "the budget must cover at least one bid"
            )
        if not 1 <= days <= 365:
            raise GatewayError(
                400, "invalid_request", "the flight is 1 to 365 days"
            )
        now = request.now or self._clock()
        campaign = self._ad_campaigns.create(
            tenant=session.tenant_id,
            advertiser=session.principal_id,
            name=name,
            creative=creative,
            offer_ref=offer_ref,
            genres=genres,
            bid_micros=bid,
            funded_micros=budget,
            flight_start=now,
            flight_end=now + timedelta(days=days),
        )
        # The funding is a ledger fact: cash in, liability standing —
        # unspent budget is OWED, not earned. Recognition is A5's post.
        from ..billing.doubleentry import LedgerEntry, LedgerTransaction

        self._commerce_ledger.post(
            LedgerTransaction(
                txn_id=f"adfund-{campaign.campaign_id}",
                idempotency_key=f"ad-fund:{campaign.campaign_id}",
                kind="ad.campaign_funded",
                order_id=campaign.campaign_id,
                entries=(
                    LedgerEntry(
                        account="marketplace_cash", amount_micros=budget
                    ),
                    LedgerEntry(
                        account="ad_budget_liability", amount_micros=-budget
                    ),
                ),
                memo=f"campaign {name!r} funded by {session.principal_id}",
                created_at=now,
            )
        )
        self._durable.audit.append(
            "ad.campaign_funded",
            {
                "campaign_id": campaign.campaign_id,
                "advertiser": session.principal_id,
                "budget_micros": budget,
                "genres": list(genres),
            },
        )
        return json_response(201, self._campaign_dict(campaign))

    def _adhouse_campaigns_list(self, request, session, params) -> Response:
        """The advertiser's own campaigns, spend previews included."""
        items = self._ad_campaigns.list(
            tenant=session.tenant_id, advertiser=session.principal_id
        )
        return json_response(
            200, {"items": [self._campaign_dict(c) for c in items]}
        )

    def _adhouse_campaign_status(self, request, session, params) -> Response:
        from ..adhouse import AdError

        campaign = self._ad_campaigns.get(
            params["campaign_id"], tenant=session.tenant_id
        )
        if campaign is None or campaign.advertiser != session.principal_id:
            raise GatewayError(404, "not_found", "no such campaign")
        try:
            updated = self._ad_campaigns.set_status(
                campaign.campaign_id,
                tenant=session.tenant_id,
                status=str((request.body or {}).get("status") or ""),
            )
        except AdError as exc:
            raise self._adhouse_refused(exc) from exc
        return json_response(200, self._campaign_dict(updated))

    def _ad_content_genres(self, session, surface: str, content_ref: str):
        """The content's genres — the ONLY editorial fact the matcher
        ever sees (invariant 5: no rubric scores cross this line)."""
        press = self._press
        if surface == "edition" and press is not None and press.stories:
            story = press.stories.get(content_ref, tenant=session.tenant_id)
            return tuple(story.genres) if story is not None else None
        if surface == "poll" and press is not None and press.polls:
            pair = press.polls.store.get_pair(
                content_ref, tenant=session.tenant_id
            )
            return (pair.genre,) if pair is not None else None
        return None

    def _press_ads(self, request, session, params) -> Response:
        """The render-time merge: one placement for one content view —
        or an honest nothing, with the reason named. The consent gate
        comes first; nothing sponsored exists for a member who has not
        accepted the current privacy version."""
        from ..adhouse import MAX_PER_VIEWER_PER_DAY, match, placement_view

        surface = str(request.query.get("surface") or "")
        content_ref = str(request.query.get("content") or "")
        if surface not in ("edition", "poll"):
            raise GatewayError(
                400, "invalid_request", "surface must be edition or poll"
            )
        if not content_ref:
            raise GatewayError(400, "invalid_request", "content is required")
        if not self._ads_consented(session):
            return json_response(
                200, {"placement": None, "reason": "consent"}
            )
        genres = self._ad_content_genres(session, surface, content_ref)
        if genres is None:
            raise GatewayError(404, "not_found", "no such content")
        now = request.now or self._clock()
        window = self._ad_placements.day_window(now)
        if (
            self._ad_placements.viewer_count_since(
                tenant=session.tenant_id,
                viewer=session.principal_id,
                since=window,
            )
            >= MAX_PER_VIEWER_PER_DAY
        ):
            return json_response(200, {"placement": None, "reason": "capped"})
        affinity = None
        if (
            self._press_personalized(session)
            and self._press is not None
            and self._press.preferences is not None
        ):
            affinity = self._press.preferences.genre_affinity(
                tenant=session.tenant_id, principal=session.principal_id
            )
        result = match(
            self._ad_campaigns.list(tenant=session.tenant_id),
            content_genres=genres,
            now=now,
            affinity=affinity or None,
            exclude=self._ad_placements.campaigns_seen_since(
                tenant=session.tenant_id,
                viewer=session.principal_id,
                since=window,
            ),
        )
        if result is None:
            return json_response(
                200, {"placement": None, "reason": "no_match"}
            )
        placement = self._ad_placements.create(
            tenant=session.tenant_id,
            campaign_id=result.campaign.campaign_id,
            surface=surface,
            content_ref=content_ref,
            viewer=session.principal_id,
            price_micros=result.price_micros,
            breakdown=result.breakdown,
        )
        return json_response(
            200, {"placement": placement_view(placement, result.campaign)}
        )

    def _ad_deliver(self, request, session, params, *, kind: str) -> Response:
        from ..adhouse import AdError

        placement = self._ad_placements.get(
            params["placement_id"], tenant=session.tenant_id
        )
        if placement is None:
            raise GatewayError(404, "not_found", "no such placement")
        try:
            recorded = self._ad_events.record(
                placement, kind=kind, viewer=session.principal_id
            )
        except AdError as exc:
            raise self._adhouse_refused(exc) from exc
        if recorded and kind == "impression":
            # Display-only spend recognition: the budget depletes so the
            # matcher stops over-serving. The ledger waits for A5.
            self._ad_campaigns.add_charge(
                placement.campaign_id,
                tenant=session.tenant_id,
                amount_micros=placement.price_micros,
            )
        return json_response(200, {"recorded": bool(recorded), "kind": kind})

    def _adhouse_impression(self, request, session, params) -> Response:
        return self._ad_deliver(request, session, params, kind="impression")

    def _adhouse_click(self, request, session, params) -> Response:
        return self._ad_deliver(request, session, params, kind="click")

    def _ad_lineage_of(self, session):
        """How a placement resolves to the contributors it ran against:
        edition → the story's recorded lineage weights; poll → the two
        sides, evenly. The same set A5's real split will pay."""
        press = self._press

        def lineage(placement) -> list[tuple[str, float]]:
            if (
                placement.surface == "edition"
                and press is not None
                and press.stories is not None
            ):
                story = press.stories.get(
                    placement.content_ref, tenant=placement.tenant_id
                )
                if story is None:
                    return []
                return [(s.author, s.weight) for s in story.lineage]
            if (
                placement.surface == "poll"
                and press is not None
                and press.polls is not None
            ):
                pair = press.polls.store.get_pair(
                    placement.content_ref, tenant=placement.tenant_id
                )
                if pair is None:
                    return []
                return [(pair.left.author, 0.5), (pair.right.author, 0.5)]
            return []

        return lineage

    def _adhouse_settle(self, request, session, params) -> Response:
        """The dividend crank (A5): every impressed, lineage-resolvable
        placement settles once through the standing pipeline — fraud
        gates, conserved split, holdback accrual, balanced recognition.
        Idempotent end to end; local-only infra refuses honestly."""
        if self._ad_dividend is None:
            raise GatewayError(
                404, "not_found", "this host keeps no ad settlement stack"
            )
        from ..billing import MoneyModeError

        lineage = self._ad_lineage_of(session)
        impressed = self._ad_events.impressed_placements(
            tenant=session.tenant_id
        )
        settled, refused, skipped = 0, 0, 0
        try:
            for placement in self._ad_placements.list(tenant=session.tenant_id):
                if placement.placement_id not in impressed:
                    continue
                shares = lineage(placement)
                if not shares:
                    skipped += 1  # content gone: nothing to attribute
                    continue
                outcome = self._ad_dividend.settle_impression(
                    placement,
                    shares=shares,
                    engaged=self._ad_events.has(
                        placement.placement_id, kind="click"
                    ),
                )
                if outcome.get("settled"):
                    settled += 1
                    self._durable.audit.append(
                        "ad.settled",
                        {
                            "event_id": outcome["event_id"],
                            "campaign_id": placement.campaign_id,
                            "net_micros": outcome["net_micros"],
                            "platform_micros": outcome["platform_micros"],
                        },
                    )
                else:
                    refused += 1
        except MoneyModeError as exc:
            # The plan's invariant 11, at the door: no ad money on
            # local-only infra — named, never silently skipped.
            raise GatewayError(403, "forbidden", str(exc)) from exc
        return json_response(
            200, {"settled": settled, "refused": refused, "skipped": skipped}
        )

    # -- the explorer desk (A6) ---------------------------------------- #
    @staticmethod
    def _explorer_refused(exc) -> GatewayError:
        code = {404: "not_found", 403: "forbidden", 409: "conflict"}.get(
            exc.status, "invalid_request"
        )
        return GatewayError(exc.status, code, str(exc))

    def _explorer_trust_for(self, tenant: str):
        """The trust read model: derive per seller from the durable
        order book — finished, refunded, disputed — never from a claim."""
        from ..explorer import trust_from_book

        store = self._commerce_orders.orders

        def trust(seller: str) -> dict:
            finished = refunded = disputed = 0
            for order in store.list_for(tenant=tenant, principal=seller):
                if order.record.seller_principal != seller:
                    continue  # their buying is not their selling record
                if order.state in ("accepted", "completed"):
                    finished += 1
                elif order.state == "refunded":
                    refunded += 1
                elif order.state == "disputed":
                    disputed += 1
            return trust_from_book(
                finished=finished, refunded=refunded, disputed=disputed
            )

        return trust

    def _explorer_rows(self, session, *, category: str):
        from ..explorer import build_rows, feedback_of, lab_of

        listings = [
            listing
            for listing in self._commerce_catalog.store.active()
            if listing.tenant_id == session.tenant_id
            and (not category or listing.category == category)
        ]
        tenant = session.tenant_id
        return build_rows(
            listings,
            feedback_for=lambda lid: feedback_of(
                self._explorer_reviews.store.for_listing(lid, tenant=tenant)
            ),
            trust_for=self._explorer_trust_for(tenant),
            lab_for=lambda lid: lab_of(
                self._explorer_lab.for_listing(lid, tenant=tenant)
            ),
        )

    def _explorer_compare(self, request, session, params) -> Response:
        """The matrix and the brief in one read: every candidate a
        normalized row (ineligible ones keep their named gaps), the
        eligible ones ranked by the named mode with the full factor
        breakdown. Deterministic end to end — and nothing sponsored is
        anywhere near these inputs (the import scan holds the wall)."""
        from ..explorer import best_buy, infer_mode

        category = str(request.query.get("category") or "")
        # The lens is READ, never a menu: an explicit mode still wins
        # (the API stays whole), but absent one the member's own words
        # (?instruction=) weigh first, their last decided comparison's
        # mode second, "balanced" third.
        mode = str(request.query.get("mode") or "") or infer_mode(
            str(request.query.get("instruction") or ""),
            last_mode=self._comparisons().last_decided_mode(
                tenant=session.tenant_id, principal=session.principal_id
            ),
        )
        rows = self._explorer_rows(session, category=category)
        try:
            brief = best_buy(rows, mode=mode)
        except ValueError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        return json_response(
            200,
            {
                "category": category,
                "rows": [row.model_dump(mode="json") for row in rows],
                "brief": brief.model_dump(mode="json"),
            },
        )

    def _explorer_reviews_list(self, request, session, params) -> Response:
        listing_id = str(request.query.get("listing") or "")
        if not listing_id:
            raise GatewayError(400, "invalid_request", "listing is required")
        reviews = self._explorer_reviews.store.for_listing(
            listing_id, tenant=session.tenant_id
        )
        return json_response(
            200,
            {"items": [r.model_dump(mode="json") for r in reviews]},
        )

    def _explorer_review_create(self, request, session, params) -> Response:
        """The verified-buyer gate at the door: the order and the
        listing resolve HERE, from the durable stores — the desk judges
        exactly what stands."""
        from ..explorer import ExplorerError

        body = request.body or {}
        listing = self._commerce_catalog.store.get(
            str(body.get("listing_id") or "")
        )
        if listing is None or listing.tenant_id != session.tenant_id:
            raise GatewayError(404, "not_found", "no such listing")
        order = self._commerce_orders.orders.get(
            str(body.get("order_id") or ""), tenant=session.tenant_id
        )
        try:
            review = self._explorer_reviews.review(
                tenant=session.tenant_id,
                reviewer=session.principal_id,
                listing=listing,
                order=order,
                rating=int(body.get("rating") or 0),
                words=str(body.get("words") or ""),
            )
        except ExplorerError as exc:
            raise self._explorer_refused(exc) from exc
        self._durable.audit.append(
            "explorer.reviewed",
            {
                "review_id": review.review_id,
                "listing_id": review.listing_id,
                "order_id": review.order_id,
                "rating": review.rating,
            },
        )
        return json_response(201, review.model_dump(mode="json"))

    def _explorer_lab_list(self, request, session, params) -> Response:
        listing_id = str(request.query.get("listing") or "")
        if not listing_id:
            raise GatewayError(400, "invalid_request", "listing is required")
        reports = self._explorer_lab.for_listing(
            listing_id, tenant=session.tenant_id
        )
        return json_response(
            200,
            {"items": [r.model_dump(mode="json") for r in reports]},
        )

    def _explorer_lab_create(self, request, session, params) -> Response:
        """Lab evidence: a LIVE results contribution, attached by its
        own author — byline, provenance, and dividend-eligibility are
        the contribution's, structurally."""
        from ..explorer import ExplorerError, lab_report

        press = self._require_press()
        body = request.body or {}
        listing = self._commerce_catalog.store.get(
            str(body.get("listing_id") or "")
        )
        if listing is None or listing.tenant_id != session.tenant_id:
            raise GatewayError(404, "not_found", "no such listing")
        contribution = press.store.get(
            str(body.get("contribution_id") or ""), tenant=session.tenant_id
        )
        try:
            report = lab_report(
                tenant=session.tenant_id,
                listing_id=listing.listing_id,
                contribution=contribution,
                caller=session.principal_id,
                score=int(body.get("score") or -1),
                metrics=body.get("metrics") or {},
                now=self._explorer_lab.now(),
            )
            self._explorer_lab.insert(report)
        except ExplorerError as exc:
            raise self._explorer_refused(exc) from exc
        self._durable.audit.append(
            "explorer.lab_attached",
            {
                "report_id": report.report_id,
                "listing_id": report.listing_id,
                "contribution_id": report.contribution_id,
                "score": report.score,
            },
        )
        return json_response(201, report.model_dump(mode="json"))

    def _explorer_interest(self, request, session, params) -> Response:
        """Follow an interest: one standing daily pulse whose goal names
        the category and the mode — the brief arrives in Explorer's own
        thread. enabled:false lays the interest down."""
        from ..explorer import BRIEF_MODES, EXPLORER_BRIEF_PREFIX

        body = request.body or {}
        category = str(body.get("category") or "").strip()
        mode = str(body.get("mode") or "balanced")
        if mode not in BRIEF_MODES:
            raise GatewayError(400, "invalid_request", f"unknown mode: {mode}")
        goal = f"{EXPLORER_BRIEF_PREFIX}{category}:{mode}"
        standing = [
            s
            for s in self._pulse.list_for(
                session.tenant_id, session.principal_id
            )
            if s.goal == goal
        ]
        if body.get("enabled") is False:
            for schedule in standing:
                self._pulse.delete(
                    schedule.schedule_id,
                    tenant=session.tenant_id,
                    principal=session.principal_id,
                )
            return json_response(200, {"interest": None})
        if standing:
            schedule = standing[0]
        else:
            try:
                schedule = self._pulse.add(
                    session.tenant_id,
                    session.principal_id,
                    cadence="daily",
                    at_minute=int(body.get("at_minute", 9 * 60)),
                    goal=goal,
                    tz_offset_minutes=_tz_minutes(body.get("tz_offset_minutes")),
                    label=f"Explorer: {category or 'everything'} ({mode})",
                    now=request.now or self._clock(),
                )
            except ValueError as exc:
                raise GatewayError(400, "invalid_request", str(exc)) from exc
        return json_response(
            200,
            {
                "interest": self._pulse_row(
                    schedule, request.now or self._clock()
                )
            },
        )

    def _fire_explorer(self, schedule, occurrence: str, skipped: int) -> None:
        """A followed interest fires: build the matrix, crown the brief,
        land it as Explorer's own thread message. Audited, never raised
        into the serving request."""
        from types import SimpleNamespace

        from ..explorer import EXPLORER_BRIEF_PREFIX, best_buy

        try:
            spec = schedule.goal[len(EXPLORER_BRIEF_PREFIX) :]
            category, _, mode = spec.rpartition(":")
            session = SimpleNamespace(
                tenant_id=schedule.tenant, principal_id=schedule.principal
            )
            rows = self._explorer_rows(session, category=category)
            brief = best_buy(rows, mode=mode or "balanced")
            if brief.winner_listing_id is None:
                message = (
                    f"Nothing to compare in {category or 'the catalog'} "
                    "today — the shelf is empty or out of stock."
                )
            else:
                lines = [
                    f"Your {mode or 'balanced'} brief for "
                    f"{category or 'the catalog'}:"
                ]
                for i, item in enumerate(brief.ranked[:5], start=1):
                    lines.append(
                        f"{i}. {item['title']} — {item['seller']} · "
                        f"score {item['score']}"
                    )
                if skipped:
                    lines.append(
                        f"(Catching up: {skipped} earlier brief"
                        f"{'s were' if skipped != 1 else ' was'} missed.)"
                    )
                message = "\n".join(lines)
            if self._assistant_history is not None:
                self._assistant_history.append(
                    tenant=schedule.tenant,
                    principal=schedule.principal,
                    kind="assistant",
                    body=message,
                    agent="explorer",
                )
            self._durable.audit.append(
                "pulse.fired",
                {
                    "schedule_id": schedule.schedule_id,
                    "occurrence": occurrence,
                    "run_id": "",
                    "skipped": skipped,
                    "tenant": schedule.tenant,
                    "principal": schedule.principal,
                    "goal": schedule.goal,
                },
            )
        except Exception:  # noqa: BLE001 - the tick must keep serving
            logging.getLogger("oolu.gateway").exception(
                "explorer brief failed for %s", schedule.schedule_id
            )
            self._durable.audit.append(
                "pulse.fire_failed",
                {
                    "schedule_id": schedule.schedule_id,
                    "occurrence": occurrence,
                    "tenant": schedule.tenant,
                    "principal": schedule.principal,
                    "goal": schedule.goal,
                    "code": "explorer_error",
                    "reason": "the brief fire raised — the log has the story",
                },
            )

    # -- the travel desk (A7) and the calendar records ----------------- #
    @staticmethod
    def _records_refused(exc) -> GatewayError:
        code = {404: "not_found", 403: "forbidden"}.get(
            exc.status, "invalid_request"
        )
        return GatewayError(exc.status, code, str(exc))

    @staticmethod
    def _parse_moment(value, field: str) -> datetime:
        try:
            moment = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            raise GatewayError(
                400, "invalid_request", f"{field} must be an ISO datetime"
            ) from None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment

    def _calendar_list(self, request, session, params) -> Response:
        start = self._parse_moment(
            request.query.get("from") or "1970-01-01T00:00:00+00:00", "from"
        )
        end = self._parse_moment(
            request.query.get("to") or "2200-01-01T00:00:00+00:00", "to"
        )
        events = self._calendar.between(
            tenant=session.tenant_id,
            owner=session.principal_id,
            start=start,
            end=end,
        )
        return json_response(
            200, {"items": [e.model_dump(mode="json") for e in events]}
        )

    def _calendar_add(self, request, session, params) -> Response:
        from ..records import RecordsError

        body = request.body or {}
        try:
            event = self._calendar.add(
                tenant=session.tenant_id,
                owner=session.principal_id,
                title=str(body.get("title") or ""),
                starts_at=self._parse_moment(body.get("starts_at"), "starts_at"),
                ends_at=self._parse_moment(body.get("ends_at"), "ends_at"),
            )
        except RecordsError as exc:
            raise self._records_refused(exc) from exc
        return json_response(201, event.model_dump(mode="json"))

    def _calendar_delete(self, request, session, params) -> Response:
        removed = self._calendar.delete(
            params["event_id"],
            tenant=session.tenant_id,
            owner=session.principal_id,
        )
        if not removed:
            raise GatewayError(404, "not_found", "no such event")
        return json_response(200, {"removed": True})

    def _freebusy_grants(self, request, session, params) -> Response:
        return json_response(
            200,
            {
                "granted_to": self._freebusy.granted_by(
                    tenant=session.tenant_id, owner=session.principal_id
                )
            },
        )

    def _freebusy_grant_set(self, request, session, params) -> Response:
        """Grant (or revoke) a PEER the right to read your busy/free —
        intervals only, never event contents; that door does not exist."""
        body = request.body or {}
        peer = self._profile_principal(session, str(body.get("peer") or ""))
        if peer == session.principal_id:
            raise GatewayError(
                400, "invalid_request", "your own time is already yours"
            )
        if body.get("enabled") is False:
            self._freebusy.revoke(
                tenant=session.tenant_id,
                owner=session.principal_id,
                peer=peer,
            )
        else:
            self._freebusy.grant(
                tenant=session.tenant_id,
                owner=session.principal_id,
                peer=peer,
            )
        return json_response(
            200,
            {
                "granted_to": self._freebusy.granted_by(
                    tenant=session.tenant_id, owner=session.principal_id
                )
            },
        )

    def _travel_plan(self, request, session, params) -> Response:
        """The brief: travel candidates judged against the window, the
        party's CONSENTED shared free time, and the budget. A party
        member who has not shared availability refuses the plan by
        name — silence is never treated as free time."""
        from ..explorer import (
            TRAVEL_CATEGORY,
            TravelConstraints,
            plan_trip,
        )
        from ..records import busy_intervals, common_free

        query = request.query
        window_start = self._parse_moment(query.get("window_start"), "window_start")
        window_end = self._parse_moment(query.get("window_end"), "window_end")
        if window_end <= window_start:
            raise GatewayError(
                400, "invalid_request", "the window ends after it starts"
            )
        nights = max(1, int(query.get("nights") or 2))
        budget = int(query.get("budget_micros") or 0)
        if budget <= 0:
            raise GatewayError(
                400, "invalid_request", "the party needs a budget"
            )
        mode = str(query.get("mode") or "balanced")
        party = tuple(
            dict.fromkeys(
                p.strip()
                for p in str(query.get("party") or "").split(",")
                if p.strip()
            )
        ) or (session.principal_id,)
        busy_by_member: dict[str, list] = {}
        for member in party:
            if member != session.principal_id:
                self._profile_principal(session, member)  # exists, enabled
                if not self._freebusy.allows(
                    tenant=session.tenant_id,
                    owner=member,
                    peer=session.principal_id,
                ):
                    raise GatewayError(
                        403,
                        "forbidden",
                        f"{member} has not shared their availability with "
                        "you — ask them to grant free-busy sharing",
                    )
            busy_by_member[member] = busy_intervals(
                self._calendar.between(
                    tenant=session.tenant_id,
                    owner=member,
                    start=window_start,
                    end=window_end,
                ),
                start=window_start,
                end=window_end,
            )
        constraints = TravelConstraints(
            window_start=window_start,
            window_end=window_end,
            nights=nights,
            party=party,
            budget_micros=budget,
        )
        slots = common_free(
            busy_by_member,
            start=window_start,
            end=window_end,
            min_length=constraints.trip_length(),
        )
        rows = self._explorer_rows(session, category=TRAVEL_CATEGORY)
        try:
            brief = plan_trip(
                rows, constraints=constraints, open_slots=slots, mode=mode
            )
        except ValueError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        return json_response(200, brief.model_dump(mode="json"))

    def _travel_confirm(self, request, session, params) -> Response:
        """A booked trip becomes calendar events. The booking itself
        walked the standing spine (intent → digest → approval → order —
        a declined approval books nothing, the marketplace's own pinned
        law); this door only asks the durable order book whether YOUR
        order stands finished, then writes the trip onto YOUR calendar."""
        from ..records import RecordsError

        body = request.body or {}
        order = self._commerce_orders.orders.get(
            str(body.get("order_id") or ""), tenant=session.tenant_id
        )
        if order is None or order.record.buyer_principal != session.principal_id:
            raise GatewayError(404, "not_found", "no such order of yours")
        if order.state not in ("accepted", "completed", "confirmed"):
            raise GatewayError(
                400,
                "invalid_request",
                f"the order is {order.state!r} — a trip lands on the "
                "calendar once the booking stands",
            )
        starts_at = self._parse_moment(body.get("starts_at"), "starts_at")
        nights = max(1, int(body.get("nights") or 1))
        listing = self._commerce_catalog.store.get(order.record.offer.item_id)
        title = str(
            body.get("title")
            or f"Trip: {listing.title if listing else order.record.offer.item_id}"
        )
        try:
            event = self._calendar.add(
                tenant=session.tenant_id,
                owner=session.principal_id,
                title=title,
                starts_at=starts_at,
                ends_at=starts_at + timedelta(days=nights),
                source="trip",
            )
        except RecordsError as exc:
            raise self._records_refused(exc) from exc
        self._durable.audit.append(
            "travel.confirmed",
            {
                "order_id": order.record.order_id,
                "event_id": event.event_id,
                "nights": nights,
            },
        )
        return json_response(201, event.model_dump(mode="json"))

    def _adhouse_preview(self, request, session, params) -> Response:
        """The display-only forecast — the whole tenant's split, plus
        the caller's own line pulled out. Labeled a forecast; nothing
        here is a balance and no ledger was consulted to make it."""
        from ..adhouse import preview_earnings

        preview = preview_earnings(
            self._ad_placements.list(tenant=session.tenant_id),
            impressed=self._ad_events.impressed_placements(
                tenant=session.tenant_id
            ),
            lineage_of=self._ad_lineage_of(session),
        )
        preview["mine_micros"] = preview["contributors"].get(
            session.principal_id, 0
        )
        return json_response(200, preview)

    def _press_media(self, request, session, params) -> Response:
        """A published contribution's attached file, by reference — the
        publication is the consent that crosses the drawer wall. A file
        the author has since deleted is honestly gone (refs, never
        copies)."""
        press = self._require_press()
        record = press.store.get(
            params["contribution_id"], tenant=session.tenant_id
        )
        if record is None:
            raise GatewayError(404, "not_found", "no such contribution")
        try:
            ref = record.media[int(params["index"])]
        except (IndexError, ValueError):
            raise GatewayError(404, "not_found", "no such attachment") from None
        file = (
            self._files.get(ref.file_id, tenant=session.tenant_id)
            if self._files is not None
            else None
        )
        if file is None:
            raise GatewayError(
                404, "not_found", "the referenced file is gone"
            )
        return self._serve_drawer_bytes(file)

    def _serve_drawer_bytes(self, file) -> Response:
        """A drawer file's TRUE bytes, typed honestly, whichever shape it
        is stored in — an inline row's data-URL content is decoded, so a
        photo or clip renders the same as its blob-backed twin. Shared by
        every published-media door (press attachments, listing media)."""
        try:
            data = self._files.read_bytes(file)
        except FileTooLargeError as exc:
            raise GatewayError(404, "not_found", str(exc)) from exc
        media_type = file.media_type or "application/octet-stream"
        if not file.blob_ref and file.content.startswith("data:"):
            from base64 import b64decode

            header, _, encoded = file.content.partition(",")
            if ";base64" in header:
                try:
                    data = b64decode(encoded)
                    media_type = (
                        header[len("data:") :].split(";")[0] or media_type
                    )
                except ValueError:
                    pass  # served as stored — never a 500 over one bad row
        return Response(status=200, body=data, content_type=media_type)

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
                    self._tenant_model(session.tenant_id, purpose="rep.draft"),
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
            # The representative's own seat: drafts meter under rep.draft
            # (not the conversation) and take that seat's effort profile.
            model=self._seat_actor(
                    self._tenant_model(session.tenant_id, purpose="rep.draft"),
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

        def last_result() -> str:
            # B3: the standing result — the drawer's newest verified
            # outputs, spoken in words. No run yet is a plain answer,
            # not an error: a new node simply has no story to tell.
            standing = self._node_last_result(session.tenant_id, node_id)
            if not standing:
                return (
                    "This node has not produced a verified result yet — "
                    "run it once and its outputs will live in its drawer "
                    "under runs/."
                )
            run_id = str(standing.get("run_id") or "")
            when = str(standing.get("at") or "")
            payload = standing.get("result")
            if isinstance(payload, (dict, list)):
                spoken = json.dumps(payload, ensure_ascii=False, default=str)
            else:
                spoken = str(payload)
            say = f"Last verified result: {spoken}"
            if run_id:
                say += f" (run {run_id[:8]}"
                if when:
                    say += f", {when}"
                say += ")"
            say += " — the full record is in this node's drawer under runs/."
            # B4: the chain is visible — what this node received, from
            # whom, and what it passed on, cited with run ids from the
            # graph's handoff edges.
            graph = self._temporal_graph()
            if graph is not None:
                try:
                    edges = graph.neighbors(node_id, edge_types=("handoff",))
                except Exception:  # noqa: BLE001 - the chain is decoration
                    edges = []
                for edge in edges[:4]:
                    attrs = edge.get("attributes") or {}
                    cited = str(attrs.get("run_id") or "")[:8]
                    if edge["target_id"] == node_id:
                        say += (
                            f" It received “{attrs.get('port')}” from node "
                            f"“{self._node_title(edge['source_id'])}”"
                            + (f" in run {cited}" if cited else "")
                            + "."
                        )
                    elif edge["source_id"] == node_id:
                        say += (
                            f" Its “{attrs.get('port')}” fed node "
                            f"“{self._node_title(edge['target_id'])}”"
                            + (f" in run {cited}" if cited else "")
                            + "."
                        )
            return say

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
            '  {"tool": "last_result", "args": {}}\n'
            '  {"tool": "create_folder", "args": {"path": "<folder path>"}}\n'
            '  {"tool": "create_member", "args": {"title": "<member '
            'name>", "authority": 1, "is_supernode": false}}\n'
            '  {"tool": "grant_host", "args": {"host": "api.example.com"}}\n'
            '  {"tool": "block_host", "args": {"host": "bad.example.com"}}\n'
            '  {"tool": "block_user", "args": {"user": "<principal>"}}\n'
            "write_file also takes an optional \"folder\" to upload into "
            "a folder of this node's drawer. last_result answers \"what "
            "did you produce last\" from this node's own run records "
            "(runs/<id>/ in the drawer) — use it instead of guessing "
            "from memory. create_member mints a new "
            "node under this org's Supernode (unclaimed until someone "
            "onboards it); grant_host/block_host move this node's egress "
            "consent, and block_user refuses a principal — all through "
            "the same walls and audit as the Access desk's own controls. "
            "Never decide or sign a held request the user did not ask you "
            "to. When automation fails, give the user the error code so "
            "they can fix it later. Never ask the user TECHNICAL "
            "questions — file formats, APIs, endpoints, schemas, "
            "encodings, credentials-shape: those are the builder's "
            "decisions to make and default (named afterward in one plain "
            "sentence, revisable in words). Ask only for values in the "
            "user's world — which folder, which account, what date range, "
            "what to call the result."
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
            last_result=last_result,
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

    # ------------------------------------------------------------------ #
    # B4 — the hand-off: standing outputs offered as defaults.            #
    # ------------------------------------------------------------------ #
    def _node_title(self, node_id: str) -> str:
        """A node's spoken name, best-effort — the id's first eight
        characters when the registry cannot answer."""
        try:
            version = self._nodeplace.latest_version(node_id)
            if version is not None:
                skill = ReusableSkill.model_validate_json(
                    version.sanitized_skill_json
                )
                if skill.name:
                    return skill.name
        except Exception:  # noqa: BLE001 - a name is decoration
            pass
        return node_id[:8]

    def _handoff_bindings(self, session, function) -> list[dict]:
        """The offerable hand-offs (B4): declared inputs of THIS node
        that another node's standing output already answers — the port
        index's newest value per input name, never the node's own
        output, never an input the function already binds. Each entry
        carries the words the offer speaks: the input's plain label,
        the producer's name, and a short preview of the exact value."""
        if self._values is None or not isinstance(function, dict):
            return []
        node_id = str(function.get("node_id") or "")
        bound = set((function.get("bindings") or {}).keys())
        offers: list[dict] = []
        for spec in function.get("_input_ports") or []:
            name = str(spec.get("name") or "")
            if not name or name in bound:
                continue
            try:
                producers = self._values.producers_of(
                    session.tenant_id, name
                )
                pick = next(
                    (p for p in producers if p["producer"] != node_id), None
                )
                if pick is None:
                    continue
                record = self._values.get(
                    pick["ref"], tenant=session.tenant_id
                )
                value = record.value
                if value in ("", None, [], {}):
                    continue  # an empty standing value is nothing to offer
                preview = (
                    value
                    if isinstance(value, str)
                    else json.dumps(value, ensure_ascii=False, default=str)
                )
            except Exception:  # noqa: BLE001 - an offer is advisory
                continue
            if len(preview) > 78:
                preview = preview[:77] + "…"
            offers.append(
                {
                    "name": name,
                    "label": str(spec.get("label") or ""),
                    "producer": str(pick["producer"]),
                    "ref": pick["ref"],
                    "title": self._node_title(str(pick["producer"])),
                    "preview": preview,
                }
            )
        return offers

    def _offer_handoff(self, session, goal: str) -> str | None:
        """B4 — running a node alone whose declared input another node's
        standing output answers OFFERS that newest output as the
        default: in words, in the conversation, bound only on the
        user's yes — never silently. The offer stands for exactly one
        message, like every standing question."""
        goal = (goal or "").strip()
        if not goal or self._values is None or self._nodeplace is None:
            return None
        function = self._resolve_node_function(session, goal)
        if function is None:
            return None
        offers = self._handoff_bindings(session, function)
        if not offers:
            return None
        self._growth_offers.put(
            session.tenant_id,
            session.principal_id,
            kind="handoff",
            goal=goal,
            original_goal=goal,
        )
        named = "; ".join(
            f"your node “{o['title']}” last produced "
            f"“{o['label'] or o['name']}” — {o['preview']}"
            for o in offers
        )
        return (
            f"Before I run “{function['title']}”: {named}. Use that for "
            "this run? (yes / no — “no” runs it without)"
        )

    def _offer_task_reminder(self, say: str, session, run: dict | None) -> str:
        """P2 — a task with a date OFFERS a reminder, never files one
        silently: the run's result carried ``reminder_offer``; the
        question stands for exactly one message like every offer, and
        only the user's yes creates the row."""
        if run is None or self._reminders is None:
            return say
        state = next(
            (
                s
                for s in self._durable.runs.list(limit=10_000)
                if s.run_id == run.get("run_id")
            ),
            None,
        )
        result = self._completed_result(state) if state is not None else None
        offer = (result or {}).get("reminder_offer")
        if not (isinstance(offer, dict) and str(offer.get("text") or "").strip()):
            return say
        self._growth_offers.put(
            session.tenant_id,
            session.principal_id,
            kind="task_reminder",
            goal=json.dumps(
                {
                    "text": str(offer["text"]),
                    "day": str(offer.get("day") or ""),
                    "time": str(offer.get("time") or "09:00"),
                }
            ),
            original_goal=str(offer["text"]),
        )
        return (
            f"{say} That task carries a date — want a reminder "
            f"“{offer['text']}” on {offer.get('day')} at "
            f"{offer.get('time') or '09:00'}? (yes / no)"
        )

    def _answer_task_reminder(
        self, session, offered: str, *, accept: bool
    ) -> ChatTurn:
        """The answered offer: yes files the reminder into the standing
        store and reads the ROW back; no leaves things exactly as they
        were."""
        if not accept:
            return ChatTurn(
                say="Okay — no reminder. The task stands on its own.",
                source="tool",
            )
        try:
            ask = json.loads(offered)
            due = datetime.fromisoformat(
                f"{ask.get('day')}T{ask.get('time') or '09:00'}"
            ).replace(tzinfo=UTC)
            row = self._reminders.add(
                tenant=session.tenant_id,
                principal=session.principal_id,
                text=str(ask.get("text") or ""),
                due_at=due,
            )
        except (TypeError, ValueError) as exc:
            return ChatTurn(
                say=f"I couldn't set that reminder: {exc}", source="tool"
            )
        return ChatTurn(
            say=(
                f"Reminder set: “{row.text}” — "
                f"{row.due_at:%Y-%m-%d %H:%M} UTC."
            ),
            source="tool",
            actions=[{"tool": "create_reminder"}],
        )

    def _run_with_handoff(
        self, session, goal: str, *, bind: bool
    ) -> tuple[ChatTurn, dict | None]:
        """The answered offer: yes binds the standing output onto the
        declared input (an ``output://`` edge the exact-value binder
        resolves at execution — the newest filed value, verbatim); no
        runs the node exactly as it would have run before the question
        was asked."""
        function = self._resolve_node_function(session, goal)
        title = function["title"] if function is not None else concise_name(goal)
        offers = (
            self._handoff_bindings(session, function)
            if bind and function is not None
            else []
        )
        extra = {
            o["name"]: f"output://{o['producer']}/{o['name']}" for o in offers
        }
        try:
            run = self._start_intent_run(session, goal, extra_bindings=extra)
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
        if offers:
            named = "; ".join(
                f"“{o['label'] or o['name']}” from “{o['title']}”"
                for o in offers
            )
            say = f"Bound {named} and ran “{title}”."
        else:
            say = f"Okay — ran “{title}” without it."
        say = self._describe_run_failure(say, run, autobuild_hint=False)
        return ChatTurn(say=say, source="tool"), run

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
        # The negative-knowledge check (plan M3), before any authoring
        # spend: a REPRODUCED failure blocks an identical retry in words;
        # one failure never blocks, and a material difference (another
        # model in the seat, a reopen condition) allows the retest.
        spine_now = self._memory_spine()
        if spine_now is not None:
            from ..negative import negative_check

            verdict = negative_check(
                spine_now,
                tenant=session.tenant_id,
                subject=skill_id,
                context={"model": self._author_model_id(author)},
            )
            if verdict.get("blocked"):
                return f"error: {verdict['reason']}"
        # Writing the function is the expensive step; meter it so the user
        # sees what building the node actually drew (the resource question).
        meter = getattr(self, "_model_meter", None)
        spent_before = len(meter.charges()) if meter is not None else 0
        script, io, refusal, already_verified = self._author_function(
            session, author, goal, demonstrated
        )
        if script is None:
            # The earliest refusal — the model wrote nothing usable — is
            # still a FAILED BUILD: it lands on the ledger (and so on the
            # audit chain as node.build_failed) like every later gate's
            # refusal, or the inbox would never see the most common way
            # a build dies.
            self._ledger_note(
                session.tenant_id,
                skill_id,
                goal,
                status="refused",
                problem=refusal,
                states=("proposed", "author-refused"),
                model=self._author_model_id(author),
            )
            return f"error: {refusal}"
        # --- the birth gate (context-harness plan, Phase 4) ------------- #
        # No node publishes without its function having proven it executes
        # and speaks the contract. Static walls first (safety screen, mock
        # smells, contract presence, interface honesty), then verify by
        # execution where this host carries a script runtime — the same
        # verify-before-trust bar runs already live by, moved to BIRTH. A
        # failure buys bounded repair rounds (the runtime's edit-don't-
        # rewrite discipline, before publish instead of after), then an
        # honest refusal: an unpublished node beats an unstable one. The
        # transaction states land on the audit log with the publish.
        transaction: list[str] = ["proposed", "generated"]
        repair_rounds = 0
        while True:
            problem = self._birth_problem(script, io)
            validated = "validated-static"
            if problem is None and already_verified and repair_rounds == 0:
                # The agent's finish gate already ran this exact script
                # in the sandbox — the walls stand; the run is not paid
                # twice. A repaired script always re-verifies.
                validated = "validated"
            elif problem is None:
                verify = self._author_verifier(ports=(io or {}).get("outputs"))
                if verify is not None:
                    report = verify(script)
                    if report.get("ok"):
                        validated = "validated"
                        note = report.get("honest_error")
                        if note:
                            # Executed, spoke the contract, and honestly
                            # named the data it cannot reach at birth —
                            # recorded, never punished.
                            transaction.append(
                                f"honest-error:{str(note)[:120]}"
                            )
                    else:
                        problem = str(
                            report.get("error")
                            or "the function failed in the sandbox"
                        )
            if problem is None:
                transaction.append(validated)
                break
            if repair_rounds >= 2:
                self._ledger_note(
                    session.tenant_id,
                    skill_id,
                    goal,
                    status="refused",
                    script=script,
                    problem=problem,
                    states=transaction,
                    model=self._author_model_id(author),
                )
                return (
                    "error: the function failed birth verification — "
                    f"{problem} — and repair could not close the gap, so "
                    "nothing was published"
                )
            repair_rounds += 1
            transaction.append(f"repair:{problem[:120]}")
            edited, edited_io = repair_node_function(
                author, goal, script, problem
            )
            if not edited:
                self._ledger_note(
                    session.tenant_id,
                    skill_id,
                    goal,
                    status="refused",
                    script=script,
                    problem=problem,
                    states=transaction,
                    model=self._author_model_id(author),
                )
                return (
                    "error: the function failed birth verification — "
                    f"{problem} — and the model produced no usable repair, "
                    "so nothing was published"
                )
            script = edited
            if edited_io is not None:
                io = edited_io
        # --- draft → review (context-harness plan, Phase 6) ------------- #
        # A seated reviewer judges the VERIFIED function before it lists:
        # contract fit, the exact-value rule, slot-vocabulary reuse — a
        # different consultation under its own purpose, possibly a
        # different provider than the author. Availability is advisory
        # (no reviewer seated → publish as before; an unreachable
        # reviewer never blocks); a seated reviewer's block is final and
        # its reason becomes the goal's next lesson.
        reviewer = self._seat_actor(
            self._node_reviewer(session.tenant_id), session.principal_id
        )
        if reviewer is not None:
            from ..reviewer import review_node_function

            approved, concern = review_node_function(reviewer, goal, script, io)
            if approved:
                transaction.append("reviewed")
            else:
                transaction.append(f"review-blocked:{concern[:120]}")
                self._ledger_note(
                    session.tenant_id,
                    skill_id,
                    goal,
                    status="refused",
                    script=script,
                    problem=f"the publish reviewer blocked it: {concern}",
                    states=transaction,
                    model=self._author_model_id(author),
                )
                return (
                    "error: the publish reviewer blocked this function — "
                    f"{concern or 'no reason given'} — nothing was published"
                )
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
            Slot(
                name=item["name"],
                value_type=item["type"],
                role="input",
                # The plain-word ask, declared once at birth (B1): every
                # form and conversation from here on asks with THESE
                # words, never its own invention.
                label=str(item.get("label", "")),
                example=str(item.get("example", "")),
            )
            for item in io.get("inputs", [])
        ]
        produces = [
            Slot(
                name=item["name"],
                value_type=item["type"],
                role="result",
                label=str(item.get("label", "")),
                example=str(item.get("example", "")),
            )
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
        # The publish closes the book: this goal's open lessons are
        # superseded on the ledger — a warning about a problem that no
        # longer exists must never enter another context pack.
        self._ledger_note(
            session.tenant_id,
            skill_id,
            goal,
            status="published",
            states=tuple(transaction) + ("published",),
            node_id=new_id,
            model=self._author_model_id(author),
        )
        # The publish lands its relations on the temporal graph — the
        # registry row is the provenance every edge cites.
        self._graph_note_publish(new_id, skill_id, io, f"nodeplace:{new_id}")
        # The function becomes a FILE the human can open: src/main.py in
        # the node's own drawer — written through the node.build SEAT, so
        # the write is scope-checked, attested, and audited like every
        # seated model act. The drawer copy is the function's HOME from
        # here on: runs read it first, so editing the file edits the node.
        # B2 law: this write is part of the publish — a miss is LOUD
        # (node.src_unlanded on the audit chain, the operator inbox, and
        # the receipt), never a silent divergence, and the run-time heal
        # rewrites the copy from the version.
        src_note = self._land_src(
            session, new_id, script, goal=goal, transaction=transaction
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
        # The B0 receipt line: every mechanism inside was DECIDED, never
        # asked — and the decision stays revisable in plain words.
        decided_note = (
            " Every technical choice inside — how it reads, what it "
            "calls, what it writes — was decided for you; say "
            "“revise …” to change any of it in plain words."
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
                + decided_note
                + src_note
                + web_note
                + cost_note
            )
        return (
            f"Built a NEW node “{name}” ({new_id[:8]}) WITH its own "
            f"execution function ({interface}), {placing}. It starts "
            "needs-verification and becomes a callable, routable step as "
            "its runs verify." + decided_note + src_note + web_note + cost_note
        )

    def _build_program_node(self, session, goal: str) -> str:
        """Author and publish ONE program node (F1) — the explicit-request
        counterpart to :meth:`_build_function_node`. The explicit "build me
        a program …" IS the consent (one goal, one build), exactly as the
        explicit node request is; the model plans the spec, writes each
        module verified one at a time, and the deterministic dispatcher is
        the one face — then the F0 door verifies the whole tree and lands
        it. Returns words; an ``error: …`` prefix means refusal."""
        from ..programbuilder import ProgramAuthor

        goal = (goal or "").strip()
        if not goal:
            return "error: tell me what the program should do"
        if obviously_chat(goal):
            return (
                "error: that reads as conversation, not a buildable "
                "program"
            )
        if self._nodeplace is None or self._desk is None:
            return "error: nodes are not enabled on this host"
        author = self._seat_actor(
            self._seated_program_planner(session.tenant_id),
            session.principal_id,
        )
        if author is None:
            return (
                "error: building a program means a model to plan and write "
                "it, and none is configured — add a model key (or a local "
                "model) in Settings"
            )
        runner = self._contract_executors.get("script")
        verify_fn = getattr(runner, "verify_function", None) if runner else None
        if not callable(verify_fn):
            return (
                "error: this host has no script runtime to verify a "
                "program against — a program node is verified by execution "
                "before it can be trusted"
            )
        meter = getattr(self, "_model_meter", None)
        spent_before = len(meter.charges()) if meter is not None else 0
        # The birth-verify primitive itself (the F0 seam), so each module's
        # check runs with the partial tree staged; the door re-verifies the
        # whole tree against its declared ports at publish.
        builder = ProgramAuthor(author, verify=verify_fn)
        build = builder.build(goal)
        cost_note = self._build_cost_note(meter, spent_before)
        if not build.ok:
            return f"error: {build.problem}"
        result = self.publish_program_node(
            session,
            goal=goal,
            script=build.script,
            files=build.files,
            program=build.spec,
            io=build.io,
        )
        if not result.get("ok"):
            return f"error: {result.get('problem', 'the program did not publish')}"
        modules = len(build.spec.modules) if build.spec else 0
        note = (
            (" " + "; ".join(build.notes[:2])) if build.notes else ""
        )
        return (
            f"Built a NEW program node “{concise_name(goal)}” "
            f"({result['node_id'][:8]}) — {modules} internal modules behind "
            "one interface, each verified as it was written, the whole tree "
            f"verified in the sandbox. It authored across {build.consultations} "
            "model consultations. It starts needs-verification and becomes a "
            "callable, routable step as its runs verify."
            + note
            + result.get("receipt_note", "")
            + cost_note
        )

    def _seated_program_planner(self, tenant: str):
        """The model that plans and writes a program's modules — routed
        under the ``node.plan_program`` purpose so its spend and audit
        stand apart, a seam tests can supply their own for."""
        return self._tenant_model(tenant, purpose="node.plan_program")

    def _land_src(
        self,
        session,
        node_id: str,
        script: str,
        *,
        goal: str,
        transaction: list | None = None,
        revision: bool = False,
        instruction: str | None = None,
    ) -> str:
        """Land a function in its drawer home (``src/main.py``) as part
        of the publish/revise transaction — B2's law, via the one tree
        landing (:meth:`_land_tree` with a single file)."""
        return self._land_tree(
            session,
            node_id,
            {"src/main.py": script},
            goal=goal,
            transaction=transaction,
            revision=revision,
            instruction=instruction,
        )

    def _land_tree(
        self,
        session,
        node_id: str,
        tree: dict[str, str],
        *,
        goal: str,
        transaction: list | None = None,
        revision: bool = False,
        instruction: str | None = None,
    ) -> str:
        """Land a function's WHOLE tree in its drawer home as part of the
        publish/revise transaction — B2's law generalized (F0): every
        write goes through one seat-walled ``DeskFiles`` pass and one
        audit event, and the transaction either commits or misses LOUDLY
        (``node.src_unlanded`` on the audit chain, echoed in the receipt,
        standing in the operator inbox until the run-time heal closes
        it). ``tree`` maps drawer paths (``src/main.py``,
        ``src/lib/ingest.py``, ``src/program.json``) to content. Returns
        the receipt's note: empty on success, the warning sentence on a
        miss (naming the WHOLE tree and what did land, F0.1 — a
        multi-file miss must not read as a lone main.py miss)."""
        problem = None
        landed: list[str] = []
        if self._files is None:
            problem = "this host keeps no file store"
        else:
            try:
                desk_files = DeskFiles(
                    self._files,
                    tenant=session.tenant_id,
                    node_id=node_id,
                    seat=SEATS["node.build"],
                    # Consent was the door that let this builder run at
                    # all — the settings switch, the growth-offer "yes",
                    # or the user's explicit ask; the caller attests.
                    consented=True,
                )
                for path in sorted(tree):
                    desk_files.write(path, tree[path])
                    landed.append(path)
                if transaction is not None:
                    transaction.append("published")
                self._durable.audit.append(
                    "model.seat",
                    {
                        "purpose": "node.build",
                        "tenant": session.tenant_id,
                        "by": session.principal_id,
                        "node_id": node_id,
                        "written": desk_files.written,
                        **({"revision": True} if revision else {}),
                        **(
                            {"transaction": list(transaction)}
                            if transaction is not None
                            else {}
                        ),
                    },
                )
                self._file_node_commit(
                    session.tenant_id,
                    node_id,
                    kind="revise" if revision else "build",
                    instruction=instruction or goal,
                    by=session.principal_id,
                )
                return ""
            except Exception as exc:  # noqa: BLE001 - the miss must be LOUD
                problem = str(exc)
        if transaction is not None:
            transaction.append("src-unlanded")
        # A tree may have landed PARTIALLY before the miss — the audit
        # names exactly what did (so the operator inbox and any heal see
        # the divergence), never a blanket "nothing landed" that a
        # half-written drawer would make a lie.
        expected = sorted(tree)
        missed = [p for p in expected if p not in landed]
        try:
            self._durable.audit.append(
                "node.src_unlanded",
                {
                    "tenant": session.tenant_id,
                    "node_id": node_id,
                    "goal": str(goal)[:200],
                    "problem": str(problem)[:400],
                    "landed": landed,
                    "missed": missed,
                },
            )
        except Exception:  # noqa: BLE001 - the audit chain outranks nothing here
            pass
        if len(expected) == 1:
            what = "the function's drawer copy (src/main.py)"
        elif landed:
            what = (
                f"part of the program's drawer tree ({len(missed)} of "
                f"{len(expected)} files, incl. {missed[0]})"
            )
        else:
            what = "the program's drawer tree"
        return (
            f" One thing to know: {what} did not land — {problem}. The "
            "node still runs from its registered version, and the copy "
            "heals on its next run."
        )

    def _heal_drawer_src(self, session, node_id: str, script: str) -> None:
        """The run-time reconciliation (B2): a node whose drawer is
        missing its ``src/main.py`` gets the copy rewritten FROM the
        registered version before the run stages files — deletion (or a
        publish-time miss) heals instead of diverging, and the heal is
        on the audit chain. Advisory: a broken file store never blocks
        the run the version can already serve."""
        if self._files is None or not script:
            return
        try:
            for file in self._files.list(
                tenant=session.tenant_id, node_id=node_id
            ):
                if file.folder == "src" and file.name == "main.py":
                    return
            desk_files = DeskFiles(
                self._files,
                tenant=session.tenant_id,
                node_id=node_id,
                seat=SEATS["node.build"],
                consented=True,
            )
            desk_files.write("src/main.py", script)
            self._durable.audit.append(
                "node.src_healed",
                {
                    "tenant": session.tenant_id,
                    "node_id": node_id,
                    "healed": "src/main.py",
                    "from": "version",
                },
            )
        except Exception:  # noqa: BLE001 - healing is advisory
            pass

    def _src_issues(self, session) -> list[dict]:
        """Nodes whose function has no drawer copy — the standing
        divergences the operator inbox shows. A projection, so an item
        leaves the moment the heal (or any write) lands the file; a
        drawer copy that DIFFERS from the version is deliberately not
        an issue — the file is the node, and editing it is the design."""
        if self._files is None or self._nodeplace is None:
            return []
        issues: list[dict] = []
        for node in self._nodeplace.all_nodes():
            if node.tenant_id != session.tenant_id or node.revoked_at is not None:
                continue
            version = self._nodeplace.latest_version(node.node_id)
            if version is None:
                continue
            try:
                skill = ReusableSkill.model_validate_json(
                    version.sanitized_skill_json
                )
            except Exception:  # noqa: BLE001
                continue
            script = self._skill_script(skill)
            if not script:
                continue
            if any(
                f.folder == "src" and f.name == "main.py"
                for f in self._files.list(
                    tenant=session.tenant_id, node_id=node.node_id
                )
            ):
                continue
            issues.append(
                {
                    "node_id": node.node_id,
                    "title": skill.name,
                    "problem": (
                        "the function's drawer copy (src/main.py) is "
                        "missing — it heals on the node's next run"
                    ),
                }
            )
        return issues

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
        script, io, refusal, _verified = self._author_function(
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
        # The same transactional landing as build (B2): the drawer write
        # succeeds through the seat or misses LOUDLY — and on a host
        # without a file store the revision still lands in the registry
        # (the version_note above) instead of crashing on the write.
        src_note = self._land_src(
            session,
            node_id,
            script,
            goal=entry.title,
            revision=True,
            instruction=change,
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
        landed = (
            "its execution function (src/main.py) was rewritten with the "
            "change applied, through the node.build seat, and the act is "
            "audited"
            if not src_note
            else "the revision is registered"
        )
        return (
            f"Revised “{entry.title}” — {landed}. The node's next "
            "run executes the updated code."
            + version_note
            + src_note
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
        # B2 reconciliation: a drawer missing its src/main.py heals from
        # the version BEFORE this run stages files — deletion or a
        # publish-time miss closes here, audited, instead of diverging.
        self._heal_drawer_src(session, node.node_id, str(script))
        # The declared slot vocabulary lives on the LISTING (a bare Node
        # row carries no io) — the run-time stamps read it from there.
        try:
            listing = self._nodeplace.listing_for_version(version.version_id)
        except Exception:  # noqa: BLE001 - stamps are best-effort
            listing = None
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
                **self._declared_ports(listing),
                **self._declared_inputs(listing),
            },
            tenant=session.tenant_id,
        )

    def publish_program_node(
        self,
        session,
        *,
        goal: str,
        script: str,
        files: dict[str, str],
        program,
        io: dict | None = None,
    ) -> dict:
        """The internal publish door for PROGRAM NODES (F0): a hand- (or
        later, F1: model-) authored tree passes the same birth gate as any
        function — judged AS ITSELF, with its tree staged — and lands as
        one citizen with one face.

        ``script`` is the entry (``src/main.py``); ``files`` the tree
        beyond it, in run-staging form (``lib/ingest.py``,
        ``tests/check_ingest.py``); ``program`` a :class:`ProgramSpec` or
        its raw dict; ``io`` the optional declared interface
        (``{"inputs": [...], "outputs": [...]}``, outputs defaulting to
        the spec's interface ports).

        The gate, in order — each wall refuses by name:
        1. the spec parses (ceilings, cycles, reserved ports, B0 labels);
        2. spec/tree coherence (every declared module and check exists);
        3. build-time TEXT screening of every Python file in the tree —
           the entry-only screening gap closed for programs: the modules
           are where an author actually writes;
        4. tree-true birth verification: per-module checks first (fail
           fast), then the entry against its declared ports — inline
           within the staging walls, else PRE-PUBLISH FROZEN to a bundle
           and verified via ``bundle=``;
        5. publish: contribute + node account + the whole tree landed
           transactionally (B2, ``_land_tree``).

        Zero model consultations — this door verifies and lands what it
        was handed; authoring is F1's job. It carries no twin guard: like
        every internal builder, reuse-vs-new is the CALLER's decision (the
        explicit "build me a program …" request is that decision, the same
        way the explicit node request is for ``_build_function_node``).
        Returns ``{"ok": False, "problem": ...}`` on refusal, else
        ``{"ok": True, "node_id", "skill_id", "version_id", "bundle_id",
        "healed", "notes", "receipt_note"}``."""
        import hashlib
        from uuid import uuid4 as _uuid4

        from ..nodeplace.screening import mock_smells, screen_script
        from ..runtime.isolation import _MAX_STAGED_BYTES, _MAX_STAGED_FILES
        from ..skills.program import (
            RESERVED_PAYLOAD_KEYS,
            canonical_program_json,
            parse_program_spec,
        )

        if self._nodeplace is None:
            return {"ok": False, "problem": "this host has no nodeplace"}
        runner = self._contract_executors.get("script")
        verify = getattr(runner, "verify_function", None)
        if not callable(verify):
            return {
                "ok": False,
                "problem": "this host has no script runtime to verify with",
            }

        spec, problem = parse_program_spec(program)
        if problem:
            return {"ok": False, "problem": problem}
        # Every tree key is validated BEFORE anything stages (F0.1): the
        # entry is ``script``, distinct from the tree, so the tree may not
        # carry ``main.py`` (it would override the verified entry in the
        # drawer AND ride the sandbox unscreened); it may not shadow the
        # harness or the run-time side channels; it may not escape the
        # tree; and ``program.json`` is the door's to write, not the
        # author's. Refuse by name — a hostile key never reaches staging.
        from ..runtime.contract import SHIM_MODULE_NAME

        reserved_tree_keys = {
            "main.py",
            "user_script.py",
            f"{SHIM_MODULE_NAME}.py",  # derive the shim name, never drift
            "bindings.json",
            "records.json",
            "program.json",
        }
        tree: dict[str, str] = {}
        for raw_path, content in (files or {}).items():
            path = str(raw_path).replace("\\", "/")
            if _program_tree_path_unsafe(path):
                return {
                    "ok": False,
                    "problem": f"tree path '{raw_path}' escapes the tree — "
                    "paths are POSIX-relative, inside the drawer, no '..'",
                }
            if path in reserved_tree_keys or path.startswith("state/"):
                return {
                    "ok": False,
                    "problem": f"tree path '{path}' is reserved — the entry, "
                    "the harness, program.json, and run-time side channels "
                    "are not author-supplied files",
                }
            tree[path] = str(content)
        tree["program.json"] = canonical_program_json(spec)
        for module in spec.modules:
            if module.path not in tree:
                return {
                    "ok": False,
                    "problem": f"declared module '{module.path}' is not in "
                    "the tree",
                }
            if module.check is not None and module.check not in tree:
                return {
                    "ok": False,
                    "problem": f"declared check '{module.check}' is not in "
                    "the tree",
                }

        # Build-time text screening — every Python byte the author wrote,
        # not just the entry. A module returning fabricated "computed"
        # data must refuse HERE. Declared CHECK scripts keep the safety
        # screen but skip the mock screen: a check's emit_result is a
        # status constant by nature — its worth is in the asserts it
        # makes against the real modules, not in the answer it emits.
        # The entry is distinct from the tree (main.py is a refused tree
        # key), so ``(None, script)`` screens it and each tree file once.
        check_paths = {m.check for m in spec.modules if m.check is not None}
        for path, text in ((None, script), *sorted(tree.items())):
            label = path or "main.py"
            if path is not None and not path.endswith(".py"):
                continue
            flags = screen_script(text)
            if flags:
                return {
                    "ok": False,
                    "problem": f"'{label}' refused by the safety screen: "
                    + "; ".join(flags),
                }
            if path in check_paths:
                continue
            smells = mock_smells(text)
            if smells:
                return {
                    "ok": False,
                    "problem": f"'{label}' smells mocked: " + "; ".join(smells),
                }

        io = dict(io or {})
        outputs = list(io.get("outputs") or spec.interface.ports or [])
        inputs = list(io.get("inputs") or [])
        for port in outputs:
            if str(port.get("name", "")) in RESERVED_PAYLOAD_KEYS:
                return {
                    "ok": False,
                    "problem": f"the output '{port.get('name')}' is a "
                    "reserved payload key — name the result something else",
                }

        # Tree-true birth verification: inline within the staging walls,
        # else pre-publish frozen — a bundle without a node yet, adopted
        # at publish, so a big program is judged exactly as it will run.
        total_bytes = sum(len(v.encode("utf-8", "replace")) for v in tree.values())
        bundle_id: str | None = None
        stage_kwargs: dict = {"files": tree}
        # Headroom for the two files a RUN stages that birth does not:
        # bindings.json and the node's own records.json (script_node.py).
        # A tree that fits birth but not its runs would pass here and fail
        # every run — freeze earlier so births judge a stage-able tree.
        if (
            len(tree) + 2 > _MAX_STAGED_FILES
            or total_bytes > _MAX_STAGED_BYTES
        ):
            bundle_id = self._freeze_tree(tree)
            prepared = (
                self._bundle_store.prepare(bundle_id)
                if bundle_id is not None and self._bundle_store is not None
                else None
            )
            if prepared is None:
                return {
                    "ok": False,
                    "problem": "the tree exceeds inline staging "
                    f"({len(tree)} files / {total_bytes} bytes) and this "
                    "host has no bundle store to freeze it into",
                }
            # The runner stages a PREPARED bundle (one packed archive) —
            # the same seam runs use, so birth judges the exact artifact.
            stage_kwargs = {"bundle": prepared}

        notes: list[str] = []
        healed: list[str] = []
        digest = hashlib.sha256(
            (script + tree["program.json"]).encode()
        ).hexdigest()[:16]

        def _verify(label, code, session_id, **kw):
            """Wrap the birth primitive: an infrastructure failure (the
            backend down, a bad stage) is a REFUSAL with the reason
            named, never a crash that escapes the door — matching
            _author_verifier's discipline (F0.1)."""
            try:
                return verify(label, code, session_id=session_id, **kw)
            except Exception as exc:  # noqa: BLE001 - answered, never fatal
                return {"ok": False, "error": f"verification failed: {exc}"}

        for module in spec.modules:
            if module.check is None:
                continue
            report = _verify(
                f"check module {module.path}",
                tree[module.check],
                f"program-birth:{digest}:{module.path}",
                **stage_kwargs,
            )
            healed.extend(report.get("healed") or [])
            if report.get("ok"):
                continue
            if report.get("honest_error"):
                # Checks run without real bindings: an honest structured
                # refusal is the module naming its missing data, not a
                # broken module (the birth-gate rule, applied per module).
                notes.append(
                    f"check {module.check}: honest error — "
                    + str(report.get("error", ""))[:200]
                )
                continue
            return {
                "ok": False,
                "problem": f"module '{module.path}' failed its check "
                f"({module.check}): " + str(report.get("error", "")),
            }
        # The ENTRY's dispatcher reads ./bindings.json when the program
        # declares inputs (F1) — so birth must STAGE one, or the entry
        # dies on the open() before it ever dispatches or checks a port,
        # and the whole-program contract goes unverified (F1.1). An empty
        # object is the honest birth binding: the same no-real-data birth
        # a single-file node gets — the entry dispatches, and a function
        # that needs a value it wasn't given names it via emit_error (the
        # honest-error rule), never a FileNotFoundError.
        entry_kwargs = dict(stage_kwargs)
        if inputs:
            import json as _json

            entry_kwargs["files"] = {
                **entry_kwargs.get("files", {}),
                "bindings.json": _json.dumps({}),
            }
        report = _verify(
            goal,
            script,
            f"program-birth:{digest}",
            ports=[
                {"name": str(p.get("name")), "type": str(p.get("type", "str"))}
                for p in outputs
            ],
            **entry_kwargs,
        )
        healed.extend(report.get("healed") or [])
        if not report.get("ok"):
            if report.get("honest_error"):
                notes.append(
                    "birth ran honest: " + str(report.get("error", ""))[:200]
                )
            else:
                return {
                    "ok": False,
                    "problem": "the program failed birth verification: "
                    + str(report.get("error", "")),
                }

        # Publish: the citizen face is ONE contract — a single script
        # action, exactly like every function node; the tree is drawer
        # truth the runs promote and the bundle store freezes.
        skill_id = f"program-{_uuid4().hex[:12]}"
        name = concise_name(goal)
        skill = ReusableSkill.model_validate(
            {
                "id": skill_id,
                "name": name,
                "description": goal,
                "signature": {"application": "script", "adapter": "script"},
                "parameters": [
                    {
                        "name": str(item.get("name")),
                        "value_type": str(item.get("type", "str")),
                        "required": True,
                    }
                    for item in inputs
                ],
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
        transaction: list[str] = ["verified"]
        try:
            result = self._nodeplace.contribute(
                noder_principal=session.principal_id,
                tenant_id=session.tenant_id,
                skill=skill,
                semver="1.0.0",
                title=name,
                summary=goal,
                consumes=[
                    Slot(
                        name=str(item.get("name")),
                        value_type=str(item.get("type", "str")),
                        role="input",
                        label=str(item.get("label", "")),
                    )
                    for item in inputs
                ]
                or None,
                produces=[
                    Slot(
                        name=str(item.get("name")),
                        value_type=str(item.get("type", "str")),
                        role="result",
                        label=str(item.get("label", "")),
                    )
                    for item in outputs
                ]
                or None,
            )
        except Exception as exc:  # noqa: BLE001 - refusal over half-publish
            return {"ok": False, "problem": f"contribute refused: {exc}"}
        node_id = result.node.node_id
        if self._desk is not None:
            try:
                self._desk.create_account(
                    node_id,
                    principal=session.principal_id,
                    tenant=session.tenant_id,
                    policy_version=NODE_POLICY_VERSION,
                )
            except Exception:  # noqa: BLE001 - account is desk bookkeeping
                notes.append("the node account did not open; runs still work")
        receipt_note = self._land_tree(
            session,
            node_id,
            {"src/main.py": script, **{f"src/{p}": tree[p] for p in tree}},
            goal=goal,
            transaction=transaction,
        )
        return {
            "ok": True,
            "node_id": node_id,
            "skill_id": skill_id,
            "version_id": result.version.version_id,
            "bundle_id": bundle_id,
            "healed": sorted(set(healed)),
            "notes": notes,
            "receipt_note": receipt_note,
        }

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
                elif folder == "records" and file.name == "rows.json":
                    # P2: the node's OWN book rides the run as DATA
                    # (staged as ./records.json by the runner) — never
                    # part of the frozen code tree.
                    extras["_records"] = file.content
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
        try:
            listing = self._nodeplace.listing_for_version(version.version_id)
        except Exception:  # noqa: BLE001 - stamps are best-effort
            listing = None
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
                **self._declared_ports(listing),
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

    @staticmethod
    def _declared_inputs(node) -> dict:
        """The node's declared inputs, riding the resolved function with
        their plain-word labels (B4) — what the hand-off offer matches
        against the desk's standing outputs. Gateway-side only; the
        engine never reads this key."""
        inputs = [
            {
                "name": s.name,
                "type": s.value_type,
                "label": getattr(s, "label", "") or "",
            }
            for s in (getattr(node, "consumes", None) or ())
        ]
        return {"_input_ports": inputs} if inputs else {}

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

    def _start_intent_run(
        self,
        session,
        intent: str,
        *,
        max_recovery: int = 1,
        extra_bindings: dict | None = None,
        pulse: dict | None = None,
    ) -> dict:
        """Submit a plain intent as a run: the non-marketplace core of
        ``_submit_run``, shared with the chat surface.

        Asking a goal again after it FAILED revives the same run — the
        same run_id, the same Noder thread — instead of minting a fresh
        one: the retry lands where the failure lives, the thread rises
        (its moment moves), and the list stops filling with dead
        siblings of one goal.

        ``extra_bindings`` (B4) are values the CONVERSATION bound onto
        the node's declared inputs — the user's yes to an offered
        standing output — merged over the function's own bindings so the
        binder resolves them at execution.

        ``pulse`` (P0) marks a SCHEDULED fire: the schedule, the
        occurrence, and how many occurrences the host slept through —
        stamped on the run's metadata so the record says why it ran."""
        metadata: dict = {"tenant_id": session.tenant_id}
        if pulse:
            metadata["pulse"] = dict(pulse)
        # A goal the user already built a node for runs THAT node's own
        # function — the route is the stored code, not a fresh plan.
        function = self._resolve_node_function(session, intent)
        self._refuse_revoked(function)
        if function is not None and extra_bindings:
            function = {
                **function,
                "bindings": {
                    **(function.get("bindings") or {}),
                    **extra_bindings,
                },
            }
        if function is not None:
            # P3: a binding that NAMES a delivered document (a file in
            # the node's message drawer) stages that document into the
            # run — the sandbox reads what was forwarded, nothing more.
            attachments = self._binding_attachments(session, function)
            if attachments:
                function = {**function, "_attachments": attachments}
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

    def _checked_run_values(
        self, session, intent: str, provided
    ) -> dict | None:
        """The form's values, through B1's ONE strict check: each key
        must be a declared input of the node this goal resolves to, each
        value type-checked in words against the input's own plain label
        — then bound onto the run exactly as a conversation's yes would
        bind them. None when nothing was provided."""
        if not provided:
            return None
        if not isinstance(provided, dict):
            raise GatewayError(
                400, "invalid_request", "values must be an object"
            )
        function = self._resolve_node_function(session, str(intent))
        inputs = (function or {}).get("_input_ports") or []
        if not inputs:
            raise GatewayError(
                400,
                "invalid_request",
                "values only bind to a node's declared inputs — no node "
                "with declared inputs answers this goal",
            )
        from ..skills.contract import ValueInput
        from ..skills.inputs import BoundInput, validate_user_inputs

        manifest = [
            BoundInput(
                str(function.get("node_id") or ""),
                str(function.get("title") or ""),
                str(item.get("name") or ""),
                ValueInput(
                    name=str(item.get("name") or ""),
                    value_type=(
                        "number"
                        if item.get("type") == "number"
                        else "string"
                    ),
                    label=str(item.get("label") or ""),
                ),
            )
            for item in inputs
        ]
        try:
            return validate_user_inputs(manifest, provided)
        except ValueError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc

    def _submit_run(self, request, session, params) -> Response:
        body = request.body or {}
        intent = body.get("intent")
        if not intent:
            raise GatewayError(400, "invalid_request", "intent is required")
        # P2: a form's values ride the run through the B1 strict check —
        # the manifest is the ask, nothing undeclared smuggles in.
        run_values = self._checked_run_values(
            session, str(intent), body.get("values")
        )
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
                    session,
                    intent,
                    max_recovery=max_recovery,
                    extra_bindings=run_values,
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
        # capacity limit on the host, not a visibility rule.) The ONE
        # exception is explicit operator oversight: scope=tenant lists
        # every account's workflows, and only for stored users:manage
        # authority — the same authority that already administers those
        # accounts — because an operator cannot steward activity they
        # cannot see.
        tenant_wide = request.query.get("scope") == "tenant"
        if tenant_wide and not self._resolver.has_permission(
            session, "users:manage"
        ):
            raise GatewayError(
                403, "forbidden", "tenant-wide runs need users:manage authority"
            )
        runs = [
            s
            for s in self._durable.runs.list(limit=10_000)
            if s.contract.metadata.get("tenant_id") == session.tenant_id
            and (tenant_wide or s.contract.submitted_by == session.principal_id)
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
            # Every agent's thread, each row naming whose it is — the
            # export is the account's WHOLE conversation record.
            export["chat"] = self._assistant_history.history(
                tenant=tenant, principal=principal, limit=10_000, agent=None
            )
        if self._profile_photos is not None:
            photo = self._profile_photos.get(tenant=tenant, principal=principal)
            if photo is not None:
                from base64 import b64encode

                export["profile_photo"] = {
                    "media_type": photo[0],
                    "body_b64": b64encode(photo[1]).decode("ascii"),
                }
        export["calendar"] = [
            e.model_dump(mode="json")
            for e in self._calendar.between(
                tenant=tenant,
                owner=principal,
                start=datetime(1970, 1, 1, tzinfo=UTC),
                end=datetime(2200, 1, 1, tzinfo=UTC),
            )
        ]
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
        if self._profile_photos is not None:
            erased["profile_photo"] = int(
                self._profile_photos.remove(tenant=tenant, principal=principal)
            )
        if self._press is not None and self._press.preferences is not None:
            erased["press_preferences"] = self._press.preferences.erase(
                tenant=tenant, principal=principal
            )
        if self._press is not None and self._press.polls is not None:
            erased["poll_votes"] = self._press.polls.store.erase_votes(
                tenant=tenant, principal=principal
            )
            if self._press.polls.pairwise is not None:
                erased["preference_pairs"] = self._press.polls.pairwise.erase(
                    tenant=tenant, principal=principal
                )
        erased["calendar_events"] = self._calendar.erase(
            tenant=tenant, owner=principal
        )
        erased["freebusy_grants"] = self._freebusy.erase(
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
        """``(script, io, refusal, verified)`` through the strongest path
        the seated model supports: a tool-calling brain works as the
        :class:`NodeAuthorAgent` — the desk's contracts and upstream
        outputs in hand, plus a drawer read for revisions — while a model
        without reliable native tool calling keeps the one-shot
        ``author_node_function`` gates unchanged.

        "Supports" is the MANIFEST's answer (``consult_ready``: which
        model would answer, what its registry entry says), not an object
        probe — every router exposes ``consult``, so ``hasattr`` never
        distinguished models at all; a small local model now honestly
        routes to the fenced-code path built for it. Injected authors
        without the manifest port (test stubs, custom seams) keep the
        old shape-based dispatch.

        ``verified``: the returned script already passed the sandbox
        verify hand (the agent's finish gate runs it on the exact script
        delivered) — the birth gate keeps its static walls but skips a
        redundant execution."""
        ready = getattr(author, "consult_ready", None)
        agentic = (
            bool(ready()) if callable(ready) else hasattr(author, "consult")
        )
        context = self._author_context(session, author, goal)
        if not agentic:
            script, io, refusal = author_node_function(
                author, goal, demonstrated=demonstrated, context=context
            )
            return script, io, refusal, False
        verify = self._author_verifier()
        agent = NodeAuthorAgent(
            author,
            catalog=lambda: self._author_catalog(session),
            outputs=lambda node_id: self._author_node_outputs(session, node_id),
            read_file=read_file,
            verify=verify,
        )
        authored = agent.author(
            goal, demonstrated=demonstrated, context=context
        )
        already_verified = verify is not None and authored.script is not None
        return authored.script, authored.io, authored.refusal, already_verified

    def _author_embedder(self, session):
        """The model-backed embedder for authoring recall, when the
        operator turned it on — ``OOLU_EMBEDDINGS=openai`` (the tenant's
        keyed adapter) or ``local`` (the machine's own OpenAI-compatible
        server), ``OOLU_EMBEDDING_MODEL`` naming the model. Off, unset,
        unkeyed, or broken all mean None: ranking stays lexical, builds
        never wait on an embedding endpoint that isn't there."""
        import os

        choice = os.environ.get("OOLU_EMBEDDINGS", "").strip().lower()
        if choice in ("", "off"):
            return None
        cache = getattr(self, "_embedders", None)
        if cache is None:
            cache = self._embedders = {}
        key = (session.tenant_id, choice)
        if key in cache:
            return cache[key]
        embedder = None
        try:
            from ..providers.apikey import OpenAiAdapter
            from ..providers.embeddings import (
                DEFAULT_OPENAI_EMBEDDING_MODEL,
                ModelEmbedder,
                openai_embedding_fn,
            )
            from ..providers.vault import SecretVault

            transport = self._model_transport
            if transport is None:
                from ..providers.transport import HttpxTransport

                transport = HttpxTransport()
            adapter = None
            model = os.environ.get("OOLU_EMBEDDING_MODEL", "").strip()
            if choice == "openai" and self._model_keys is not None:
                secret = self._model_keys.secret_for(session.tenant_id, "openai")
                if secret:
                    vault = SecretVault()
                    adapter = OpenAiAdapter(
                        vault=vault,
                        transport=transport,
                        api_key_ref=vault.put(secret, kind="api_key"),
                    )
                    model = model or DEFAULT_OPENAI_EMBEDDING_MODEL
            elif choice == "local" and self._settings is not None:
                url = str(
                    self._settings.effective(session.tenant_id).get(
                        "model.local_url", ""
                    )
                ).strip().rstrip("/")
                if url and model:
                    vault = SecretVault()
                    adapter = OpenAiAdapter(
                        vault=vault,
                        transport=transport,
                        api_key_ref=vault.put("local", kind="api_key"),
                        base_url=url,
                    )
            if adapter is not None:
                embedder = ModelEmbedder(
                    openai_embedding_fn(adapter, model=model)
                )
        except Exception:  # noqa: BLE001 - recall stays lexical, never fatal
            embedder = None
        cache[key] = embedder
        return embedder

    def _author_context(self, session, author, goal: str) -> str:
        """The compiled desk pack for one build (contextpack.py): slot
        vocabulary, upstream shapes for nodes the goal names, similar
        contracts, and verified example functions read seat-scoped from
        their drawers — PUSHED into the request on both authoring paths,
        so the author starts informed instead of writing blind. An empty
        desk (or a missing nodeplace) compiles nothing and the build
        proceeds exactly as before."""
        from ..contextpack import ContextPackCompiler, NodeExample
        from ..retrieval import score as _pack_score

        # Model-backed when the operator configured one; lexical always
        # otherwise — same seam, same call, per the retrieval contract.
        embedder = self._author_embedder(session)

        def _pack_similarity(a: str, b: str) -> float:
            return _pack_score(a, b, embedder=embedder)

        catalog = self._author_catalog(session)
        window = 32_000
        manifest = getattr(author, "manifest_now", None)
        if callable(manifest):
            try:
                window = int(manifest().context_window) or window
            except Exception:  # noqa: BLE001 - advisory sizing, never fatal
                pass

        goal_words = set(re.findall(r"[a-z0-9]+", goal.casefold()))
        upstream = []
        for node in catalog:
            title = str(node.get("title") or "").strip()
            title_words = {
                w for w in re.findall(r"[a-z0-9]+", title.casefold()) if len(w) > 2
            }
            if not title_words or not title_words.issubset(goal_words):
                continue
            outputs = self._author_node_outputs(session, node["node_id"])
            if outputs:
                upstream.append(
                    {
                        "node_id": node["node_id"],
                        "title": title,
                        "outputs": outputs,
                    }
                )

        ranked = sorted(
            catalog,
            key=lambda node: _pack_similarity(
                goal, f"{node.get('title', '')} {node.get('goal', '')}"
            ),
            reverse=True,
        )
        examples = []
        for node in ranked[:3]:
            script = self._node_drawer_read(
                session, node["node_id"], "src/main.py"
            )
            if script:
                examples.append(
                    NodeExample(
                        card=node,
                        script=script,
                        score=_pack_similarity(
                            goal, f"{node.get('title', '')} {node.get('goal', '')}"
                        ),
                    )
                )

        # The goal's standing lessons — read from the atomic memory
        # spine (the one reader, plan M0), where superseded and expired
        # records are excluded by the query's shape, not a caller's
        # discipline; the ledger remains the fallback for hosts whose
        # spine could not open.
        lessons: list[str] = []
        goal_key = self._function_skill_id(session.tenant_id, goal)
        spine = self._memory_spine()
        if spine is not None:
            try:
                lessons = [
                    m["statement"]
                    for m in spine.recall(
                        (session.tenant_id, goal_key),
                        goal,
                        kinds=("lesson",),
                        limit=3,
                    )
                ]
            except Exception:  # noqa: BLE001 - memory is advisory
                lessons = []
        if not lessons:
            ledger = self._build_ledger()
            if ledger is not None:
                try:
                    lessons = ledger.lessons_for(session.tenant_id, goal_key)
                except Exception:  # noqa: BLE001 - memory is advisory
                    lessons = []

        pack = ContextPackCompiler(window=window, embedder=embedder).compile(
            goal,
            catalog=catalog,
            examples=examples,
            upstream=upstream,
            lessons=lessons,
        )
        if not pack.empty:
            # The per-call trace the plan's observability starts from:
            # what rode, what the budget dropped, and the tokens it cost.
            logging.getLogger("oolu.gateway").info(
                "node.build context pack: %d tokens, included=%s, excluded=%s",
                pack.tokens,
                list(pack.included),
                list(pack.excluded),
            )
        return pack.text

    def _node_reviewer(self, tenant: str):
        """The publish reviewer's brain — the tenant's model seated
        APART under ``node.review`` (its own purpose in the meter and
        the books), or None when no model is configured: review is
        advisory in availability, decisive in verdict."""
        try:
            return self._tenant_model(tenant, purpose="node.review")
        except TypeError:
            # An injected single-purpose chat brain (test seams stub
            # _tenant_model without the purpose keyword) is the CHAT's
            # model, not a reviewer — review stays unseated rather than
            # conscripting whatever sat closest.
            return None

    @staticmethod
    def _author_model_id(author) -> str:
        """WHO sat in the seat, for the ledger's per-model outcome
        history — the router names its answering model; injected stubs
        stay anonymous rather than invented."""
        fn = getattr(author, "answering_model", None)
        if callable(fn):
            try:
                provider, model = fn()
                return str(model or provider or "")
            except Exception:  # noqa: BLE001 - identity is telemetry
                return ""
        return ""

    def _ledger_note(self, tenant: str, goal_key: str, goal: str, **kwargs) -> None:
        """One build outcome onto the ledger — advisory memory, so a
        broken ledger never breaks a build. Refusals first land a
        ``model.memory`` event on the hash-chained audit log, and its
        id rides as the lesson's provenance: every memory answers
        "where did you come from" with a link the chain can verify."""
        ledger = self._build_ledger()
        if ledger is None:
            return
        try:
            if kwargs.get("status") == "refused":
                # The DEFINED failure event, distinct from the memory
                # entry below: node.build_failed is what attention
                # surfaces (the operator inbox) key on — a build that
                # refused is something someone should SEE, not only
                # something the next attempt remembers.
                self._durable.audit.append(
                    "node.build_failed",
                    {
                        "tenant": tenant,
                        "goal_key": goal_key,
                        "goal": str(goal)[:200],
                        "problem": str(kwargs.get("problem", ""))[:400],
                        "model": str(kwargs.get("model", "")),
                    },
                )
                record = self._durable.audit.append(
                    "model.memory",
                    {
                        "kind": "build-lesson",
                        "tenant": tenant,
                        "goal_key": goal_key,
                        "problem": str(kwargs.get("problem", ""))[:400],
                    },
                )
                audit_id = getattr(record, "entry_id", None) or getattr(
                    record, "id", ""
                )
                if audit_id:
                    kwargs = {
                        **kwargs,
                        "provenance": (f"audit:{audit_id}",),
                    }
            ledger.record(tenant, goal_key, goal, **kwargs)
            # The episode writer (plan M2): the same outcome lands as a
            # stretch-of-work record on the spine — objective, outcome,
            # unresolved problem verbatim — so a project interrupted for
            # weeks restores from the stack, not a transcript.
            spine = self._memory_spine()
            status = str(kwargs.get("status", ""))
            if spine is not None and status in ("published", "refused"):
                from ..episodes import record_episode

                problem = str(kwargs.get("problem", ""))
                record_episode(
                    spine,
                    tenant=tenant,
                    subject=goal_key,
                    kind="build",
                    objective=goal,
                    outcome=status,
                    unresolved=(problem,) if problem else (),
                    sources=tuple(kwargs.get("provenance", ()))
                    or (f"goal:{goal_key}",),
                )
                if status == "refused" and problem:
                    from ..negative import record_failure

                    record_failure(
                        spine,
                        tenant=tenant,
                        subject=goal_key,
                        problem=problem,
                        applicability={
                            "model": str(kwargs.get("model", ""))
                        },
                        sources=tuple(kwargs.get("provenance", ())),
                    )
        except Exception:  # noqa: BLE001 - memory is advisory
            pass

    def _build_ledger(self):
        """The durable build ledger (buildledger.py), lazily over the
        same connection every other promise rides — a failed build's
        state survives unrelated turns, restarts, and processes, and its
        lessons feed the next attempt's context pack. None only when the
        durable service carries no usable connection (exotic test
        doubles) — the door then simply builds without memory."""
        cached = getattr(self, "_build_ledger_obj", None)
        if cached is not None:
            return cached
        conn = getattr(self._durable, "conn", None)
        if conn is None or not hasattr(conn, "transaction"):
            return None
        try:
            from ..buildledger import BuildLedger

            self._build_ledger_obj = BuildLedger(
                conn, spine=self._memory_spine()
            )
        except Exception:  # noqa: BLE001 - memory is advisory, never fatal
            return None
        return self._build_ledger_obj

    def _temporal_graph(self):
        """The temporal graph (temporalgraph.py, plan M1), lazily on the
        same durable connection — time-scoped relations the retrieval
        layer reads for proximity and the cards project from."""
        cached = getattr(self, "_temporal_graph_obj", None)
        if cached is not None:
            return cached
        conn = getattr(self._durable, "conn", None)
        if conn is None or not hasattr(conn, "transaction"):
            return None
        try:
            from ..temporalgraph import TemporalGraph

            self._temporal_graph_obj = TemporalGraph(conn)
        except Exception:  # noqa: BLE001 - relations are advisory
            return None
        return self._temporal_graph_obj

    def _graph_note_publish(
        self, node_id: str, goal_key: str, io: dict, provenance: str
    ) -> None:
        """A publish lands its relations: the node satisfies its goal,
        and consumes/produces its slots — the edges proximity ranking
        and route position read from now on. Advisory, never fatal."""
        graph = self._temporal_graph()
        if graph is None:
            return
        try:
            graph.connect(
                "satisfies", node_id, f"goal:{goal_key}", provenance=(provenance,)
            )
            for item in (io or {}).get("inputs", []):
                graph.connect(
                    "consumes",
                    node_id,
                    f"slot:{item.get('name')}",
                    provenance=(provenance,),
                )
            for item in (io or {}).get("outputs", []):
                graph.connect(
                    "produces",
                    node_id,
                    f"slot:{item.get('name')}",
                    provenance=(provenance,),
                )
        except Exception:  # noqa: BLE001 - relations are advisory
            pass

    def _node_state_card(self, session, node_id: str) -> dict:
        """One node's current truth, PROJECTED — derived from the stores
        on every call, never stored, so rebuild-equals-read holds by
        construction (plan M1: state is a projection, not a transcript
        summary)."""
        card: dict = {"node_id": node_id, "contract": None, "relations": []}
        for node in self._author_catalog(session):
            if node.get("node_id") == node_id:
                card["contract"] = node
                break
        graph = self._temporal_graph()
        if graph is not None:
            card["relations"] = [
                {k: e[k] for k in ("edge_type", "source_id", "target_id")}
                for e in graph.neighbors(node_id)
            ]
        spine = self._memory_spine()
        if spine is not None:
            card["open_lessons"] = [
                m["statement"]
                for m in spine.recall(
                    (session.tenant_id, node_id), kinds=("lesson",)
                )
            ]
        return card

    def _memory_spine(self):
        """The atomic memory spine (memoryspine.py, plan M0), lazily on
        the same durable connection — the one table every memory tier's
        records meet, and the one reader the context pack consults."""
        cached = getattr(self, "_memory_spine_obj", None)
        if cached is not None:
            return cached
        conn = getattr(self._durable, "conn", None)
        if conn is None or not hasattr(conn, "transaction"):
            return None
        try:
            from ..memoryspine import MemorySpine

            self._memory_spine_obj = MemorySpine(conn)
        except Exception:  # noqa: BLE001 - memory is advisory, never fatal
            return None
        return self._memory_spine_obj

    @staticmethod
    def _birth_problem(script: str, io: dict) -> str | None:
        """The birth gate's static walls, one correctable sentence each —
        every path, every model, before any publish. The one-shot path
        used to skip the safety screen and the emit_result check
        entirely (they ran only at first execution, which is exactly
        when 'node creation is unstable' was felt); and a script that
        reads bindings the interface never declared is the silent
        degradation Phase 2 made loud, now held at the door."""
        from ..nodeplace.screening import mock_smells, screen_script

        flags = screen_script(script)
        if flags:
            return "refused by the safety screen: " + "; ".join(flags)
        smells = mock_smells(script)
        if smells:
            return "the function only pretends — " + "; ".join(smells)
        if "emit_result" not in script and "emit_error" not in script:
            return (
                "the script never calls emit_result — it must import "
                "emit_result from _oolu_runtime and call it exactly once "
                "with its final answer"
            )
        if "bindings.json" in script and not (io or {}).get("inputs"):
            return (
                "the script reads ./bindings.json but the declared "
                "interface lists no inputs — declare the inputs it "
                "actually consumes so routes can bind them"
            )
        return None

    def _author_verifier(self, ports: list[dict] | None = None):
        """The author's finish gate made real: a sandbox dry-run of the
        candidate script — safety screen, dependency healing, contract
        classification — with NO web grant and NO staged files, so
        nothing leaves the box. No script runtime on this host → None:
        the caller's gate degrades to its static checks.

        Where the runner speaks ``verify_function`` (the birth-verify
        primitive), the function under test is the function judged — no
        repair, no resynthesis substituting a different script — and an
        HONEST structured error passes: at birth no real bindings are
        staged, so a function that names its missing data has proven it
        executes and speaks the contract (the Phase 0 finding that an
        honest input-reading function could never pass this hand,
        fixed). Legacy runners without the primitive keep the old
        execute-based dry run."""
        runner = self._contract_executors.get("script")
        if runner is None:
            return None
        verify_fn = getattr(runner, "verify_function", None)

        def verify(script: str) -> dict:
            import hashlib

            digest = hashlib.sha256(script.encode()).hexdigest()[:16]
            if callable(verify_fn):
                try:
                    report = verify_fn(
                        "verify the authored function executes and speaks "
                        "the contract",
                        script,
                        session_id=f"author-verify:{digest}",
                        ports=list(ports or []),
                    )
                except Exception as exc:  # noqa: BLE001 - answered, never fatal
                    return {
                        "ok": False,
                        "error": f"the sandbox could not run the script: {exc}",
                    }
                if report.get("ok"):
                    return {"ok": True}
                if report.get("honest_error"):
                    return {
                        "ok": True,
                        "honest_error": str(report.get("error") or ""),
                    }
                return {
                    "ok": False,
                    "error": str(
                        report.get("error") or "the script failed in the sandbox"
                    ),
                }
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
        catalog: list[dict] = []
        for node in nodes[:40]:
            try:
                catalog.append(
                    {
                        "node_id": node.node_id,
                        "title": node.title,
                        "goal": node.summary,
                        "consumes": [
                            {"name": s.name, "type": s.value_type}
                            for s in node.consumes
                        ],
                        "produces": [
                            {"name": s.name, "type": s.value_type}
                            for s in node.produces
                        ],
                    }
                )
            except AttributeError:
                # A listing shape without the contract fields (older
                # stores, test doubles) is skipped, not fatal — the
                # catalog is advisory context, never a gate.
                continue
        return catalog

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
    # The starter shelf (personal-nodes plan P1): seven nodes at birth.   #
    # ------------------------------------------------------------------ #
    def _starter_ledger(self):
        cached = getattr(self, "_starter_ledger_obj", None)
        if cached is not None:
            return cached
        from ..nodeplace.personal_templates import StarterLedger

        self._starter_ledger_obj = StarterLedger(self._durable.conn)
        return self._starter_ledger_obj

    def _maybe_seed_starters(self, tenant: str, principal: str) -> None:
        """The sign-in hook: best-effort, never a reason a door fails."""
        try:
            self._seed_starter_shelf(tenant, principal)
        except Exception:  # noqa: BLE001 - seeding must not break sign-in
            logging.getLogger("oolu.gateway").exception(
                "starter seeding failed for %s", principal
            )

    def _seed_starter_shelf(self, tenant: str, principal: str) -> list[dict]:
        """Mint the seven personal starter nodes for one person, exactly
        once — the ledger's INSERT-OR-IGNORE claim decides, so racing
        sign-ins seed once and a DELETED starter is never resurrected
        (the claim never leaves). Each node is an ORDINARY node: the
        contribute door, the person as its responsible, declared io
        with plain-word labels, and the function landed in its drawer
        (B2). One audit line per node."""
        from types import SimpleNamespace

        from ..nodeplace.personal_templates import (
            STARTER_SHELF,
            starter_script,
        )

        if self._nodeplace is None or self._desk is None:
            return []
        if not self._starter_ledger().claim(tenant, principal):
            return []
        session = SimpleNamespace(tenant_id=tenant, principal_id=principal)
        created: list[dict] = []
        for spec in STARTER_SHELF:
            # Idempotent by goal: a node already answering (however it
            # was made) is never duplicated.
            if self._resolve_node_function(session, spec.goal) is not None:
                continue
            script = starter_script(spec)
            skill_id = self._function_skill_id(tenant, spec.goal)
            skill = ReusableSkill.model_validate(
                {
                    "id": skill_id,
                    "name": spec.name,
                    "description": spec.goal,
                    "signature": {
                        "application": "script",
                        "adapter": "script",
                    },
                    "parameters": [
                        {
                            "name": item.name,
                            "value_type": item.value_type,
                            "required": False,
                        }
                        for item in spec.inputs
                    ],
                    "actions": [
                        {
                            "correlation_id": "function",
                            "adapter": "script",
                            "operation": "run",
                            "parameters": {
                                "goal": spec.goal,
                                "script": script,
                                "node_key": f"node:{skill_id}",
                            },
                        }
                    ],
                }
            )
            consumes = [
                Slot(
                    name=item.name,
                    value_type=item.value_type,
                    role="input",
                    required=False,
                    label=item.label,
                    example=item.example,
                )
                for item in spec.inputs
            ]
            produces = [
                Slot(name=item.name, value_type=item.value_type, role="result")
                for item in spec.outputs
            ]
            try:
                result = self._nodeplace.contribute(
                    noder_principal=principal,
                    tenant_id=tenant,
                    skill=skill,
                    semver="1.0.0",
                    title=spec.name,
                    summary=spec.responsibility,
                    consumes=consumes,
                    produces=produces,
                )
                self._desk.create_account(
                    result.node.node_id,
                    principal=principal,
                    tenant=tenant,
                    policy_version=NODE_POLICY_VERSION,
                )
            except Exception as exc:  # noqa: BLE001 - one refusal never
                # blanks the shelf; the miss is named on the chain.
                self._durable.audit.append(
                    "node.starter_failed",
                    {
                        "starter": spec.key,
                        "tenant": tenant,
                        "principal": principal,
                        "reason": str(exc),
                    },
                )
                continue
            # B2: the function's drawer home lands with the publish.
            self._land_src(
                session, result.node.node_id, script, goal=spec.goal
            )
            self._durable.audit.append(
                "node.starter_seeded",
                {
                    "node_id": result.node.node_id,
                    "starter": spec.key,
                    "tenant": tenant,
                    "principal": principal,
                },
            )
            created.append(
                {
                    "node_id": result.node.node_id,
                    "key": spec.key,
                    "name": spec.name,
                }
            )
        # P4: the morning pulse ships with the shelf, DISABLED — one
        # sentence ("turn on the morning pulse") switches it on.
        from ..nodeplace.personal_templates import MORNING_PULSE_GOAL

        try:
            self._pulse.add(
                tenant,
                principal,
                cadence="daily",
                at_minute=9 * 60,
                goal=MORNING_PULSE_GOAL,
                label="the morning pulse — today's calendar and open tasks",
                enabled=False,
            )
        except ValueError:
            pass  # a full schedule book leaves the shelf standing
        self._starter_ledger().record_nodes(
            tenant, principal, [c["node_id"] for c in created]
        )
        return created

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

    def _paver_schedule_view(self, request, session, params) -> Response:
        view = self._paver_schedule.view()
        return json_response(200, view or {"enabled": False})

    def _paver_schedule_set(self, request, session, params) -> Response:
        """Stand up (or retune) the Paver's survey Routine — the consent
        for every future unattended survey, given once, audited, revocable.
        Passes the same approve gate as a sweep schedule (an operator act),
        though W1 only READS the registry and refreshes the map."""
        if self._nodeplace is None:
            raise GatewayError(404, "not_found", "nodes are not enabled")
        if self._approval is None:
            raise GatewayError(404, "not_found", "approval authority is not configured")
        now = request.now or self._clock()
        try:
            self._approval.approve(
                session,
                run_id="paver:schedule",
                policy="paver.survey",
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
        view = self._paver_schedule.enable(
            interval_hours=interval,
            granted_by=session.principal_id,
            tenant=session.tenant_id,
            now=now,
        )
        self._durable.audit.append(
            "paver.scheduled",
            {
                "run_id": "paver:schedule",
                "by": session.principal_id,
                "tenant": session.tenant_id,
                "interval_hours": view["interval_hours"],
            },
        )
        return json_response(200, view)

    def _paver_schedule_clear(self, request, session, params) -> Response:
        """Revoke the standing consent — same authority that granted it."""
        if self._approval is None:
            raise GatewayError(404, "not_found", "approval authority is not configured")
        try:
            self._approval.approve(
                session,
                run_id="paver:schedule",
                policy="paver.survey",
                requester_id="",
                now=request.now or self._clock(),
            )
        except AuthorizationError as exc:
            raise GatewayError(403, "forbidden", str(exc)) from exc
        disabled = self._paver_schedule.disable()
        if disabled:
            self._durable.audit.append(
                "paver.unscheduled",
                {"run_id": "paver:schedule", "by": session.principal_id},
            )
        return json_response(200, {"enabled": False, "disabled": disabled})

    def _paver_webs(self, request, session, params) -> Response:
        """The caller's own tenant's surveyed map — every web, its edges,
        and the near-misses a paver would have to bridge."""
        if self._pave_store is None:
            raise GatewayError(404, "not_found", "the Paver is not enabled")
        webs = self._pave_store.webs(session.tenant_id)
        return json_response(
            200,
            {
                "webs": [web.model_dump(mode="json") for web in webs],
                "count": len(webs),
            },
        )

    def _paver_webs_for_anchor(self, request, session, params) -> Response:
        """The webs a single anchor node fans out — the trigger-door view."""
        if self._pave_store is None:
            raise GatewayError(404, "not_found", "the Paver is not enabled")
        webs = self._pave_store.webs(session.tenant_id, anchor=params["anchor"])
        return json_response(
            200,
            {"webs": [web.model_dump(mode="json") for web in webs]},
        )

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
            # The pulse keeps its own minute gate too: user schedules
            # must fire whether or not a bundle sweep is configured.
            if now_mono >= self._pulse_gate:
                self._pulse_gate = now_mono + 60.0
                self._pulse_tick(request.now or self._clock())
            # The Paver's survey keeps its OWN minute gate — the map must
            # refresh whether or not a bundle sweep is configured.
            if now_mono >= self._paver_gate:
                self._paver_gate = now_mono + 60.0
                self._scheduled_survey_tick(request.now or self._clock())
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

    # ------------------------------------------------------------------ #
    # The Paver's survey heartbeat (W1): map, never author.               #
    # ------------------------------------------------------------------ #
    def _scheduled_survey_tick(self, now) -> None:
        """One due-check and, when this host wins the claim, one survey per
        tenant — under the schedule's standing consent, recorded like a
        manual run. Surveying only READS the registry and REPLACES the
        stored map; no code is authored, nothing is run, so the tick is
        cheap and effect-free by construction."""
        if self._nodeplace is None or self._pave_store is None:
            return
        if not self._paver_schedule.claim_due(now):
            return
        schedule = self._paver_schedule.view() or {}
        try:
            tenants = self._paver_tenants()
            total_webs = 0
            for tenant in tenants:
                report = self._surveyor.survey(
                    tenant, self._survey_nodes(tenant)
                )
                self._pave_store.replace_tenant(report, now=now)
                total_webs += len(report.webs)
        except Exception as exc:  # noqa: BLE001 - the Routine records its own
            # failure and waits for the next interval; it never raises.
            self._paver_schedule.record_result(now, error=str(exc))
            logging.getLogger("oolu.gateway").exception("paver survey failed")
            return
        summary = {"tenants": len(tenants), "webs": total_webs}
        self._paver_schedule.record_result(now, summary=summary)
        self._durable.audit.append(
            "paver.surveyed",
            {
                "run_id": "paver:schedule",
                "scheduled": True,
                "granted_by": schedule.get("granted_by", ""),
                **summary,
            },
        )

    def _paver_tenants(self) -> list[str]:
        """Every tenant with at least one node — the survey's field of
        view. Best-effort: an enumeration failure surveys nothing, never
        raises into the tick."""
        try:
            return sorted(
                {node.tenant_id for node in self._nodeplace.all_nodes()}
            )
        except Exception:  # noqa: BLE001 - the tick catches and records
            return []

    def _survey_nodes(self, tenant: str) -> list:
        """Build one tenant's :class:`SurveyNode` list: each live node as a
        contract, its canonical key (the node id — the same key W0 files
        under), and its trigger door (webhook or pulse) if it has one.

        A node's function is its latest version's script; a node with no
        script (revoked, empty) is skipped. Pulse anchors resolve by GOAL —
        schedules fire goals, not nodes — so a schedule whose goal names a
        node marks that node a pulse anchor."""
        from ..paver import SurveyNode

        nodes = [
            node
            for node in self._nodeplace.all_nodes()
            if node.tenant_id == tenant and node.revoked_at is None
        ]
        # Which nodes carry a webhook door.
        hook_nodes: set[str] = set()
        for node in nodes:
            try:
                if self._node_hooks.get(node.node_id) is not None:
                    hook_nodes.add(node.node_id)
            except Exception:  # noqa: BLE001 - a missing hook is just no door
                continue
        # Which nodes a pulse schedule fires, by goal → node resolution.
        pulse_nodes = self._pulse_anchor_nodes(tenant, nodes)

        survey_nodes = []
        for node in nodes:
            contract = self._survey_contract(node)
            if contract is None:
                continue
            kind = (
                "webhook"
                if node.node_id in hook_nodes
                else "pulse"
                if node.node_id in pulse_nodes
                else None
            )
            survey_nodes.append(
                SurveyNode(key=node.node_id, contract=contract, anchor_kind=kind)
            )
        return survey_nodes

    def _survey_contract(self, node):
        """One node's contract for the survey — the BODY from its latest
        version's skill (so the fileable-producer check sees the real
        script action), the INTERFACE from its listing (the published
        consumes/produces, the same slot vocabulary routing chains on).
        None when the node has no runnable function."""
        try:
            version = self._nodeplace.latest_version(node.node_id)
            if version is None:
                return None
            skill = ReusableSkill.model_validate_json(version.sanitized_skill_json)
            contract = NodeContract.from_skill(skill)
            listing = self._nodeplace.listing_for_version(version.version_id)
            if listing is not None:
                contract = contract.model_copy(
                    update={
                        "consumes": [
                            Slot(name=s.name, value_type=s.value_type, role=s.role)
                            for s in listing.consumes
                        ],
                        "produces": [
                            Slot(name=s.name, value_type=s.value_type, role=s.role)
                            for s in listing.produces
                        ],
                    }
                )
            return contract
        except Exception:  # noqa: BLE001 - a bad row is skipped, never fatal
            return None

    def _pulse_anchor_nodes(self, tenant: str, nodes) -> set[str]:
        """Node ids a pulse schedule fires, resolved by GOAL: a schedule's
        goal that names an existing node (the way the growth/build door
        resolves one) marks that node a pulse anchor."""
        anchors: set[str] = set()
        try:
            schedules = [
                s for s in self._pulse.all_enabled() if s.tenant == tenant
            ]
        except Exception:  # noqa: BLE001 - no pulse, no pulse anchors
            return anchors
        if not schedules:
            return anchors
        title_to_id = {}
        for node in nodes:
            try:
                version = self._nodeplace.latest_version(node.node_id)
                if version is None:
                    continue
                skill = ReusableSkill.model_validate_json(
                    version.sanitized_skill_json
                )
                title_to_id[skill.description.strip().casefold()] = node.node_id
                title_to_id[skill.name.strip().casefold()] = node.node_id
            except Exception:  # noqa: BLE001
                continue
        for schedule in schedules:
            goal = str(getattr(schedule, "goal", "")).strip().casefold()
            if goal in title_to_id:
                anchors.add(title_to_id[goal])
        return anchors

    # ------------------------------------------------------------------ #
    # The pulse (personal-nodes plan P0): schedules fire runs.            #
    # ------------------------------------------------------------------ #
    def _pulse_tick(self, now) -> None:
        """Fire every schedule whose newest occurrence is due — each
        elected by a durable (schedule, occurrence) claim, so a rhythm
        fires exactly once across processes and restarts. A host waking
        from sleep fires ONE catch-up per schedule, with the skipped
        count named — never a fabricated backlog."""
        from ..pulse import missed_between, occurrence_at

        for schedule in self._pulse.all_enabled():
            occurrence = occurrence_at(schedule, now)
            if occurrence is None:
                continue
            prior = self._pulse.newest_claim(
                schedule.schedule_id, before=occurrence
            )
            if not self._pulse.claim(schedule.schedule_id, occurrence):
                continue  # another process won this occurrence
            skipped = missed_between(
                schedule,
                prior["occurrence"] if prior else None,
                occurrence,
            )
            self._fire_schedule(schedule, occurrence, skipped)

    def _fire_morning(self, schedule, occurrence: str, skipped: int) -> None:
        """P4 — the morning pulse: fire the owner's Calendar and Tasks
        as ORDINARY pulse-stamped runs, then land the combined answer
        as OoLu's own message through the reminder channel (the row the
        client's standing poll already delivers). Two accounts get two
        different mornings from the same shelf, by construction: each
        fire runs the OWNER's nodes over the OWNER's books."""
        from types import SimpleNamespace

        from ..nodeplace.personal_templates import STARTER_SHELF

        session = SimpleNamespace(
            tenant_id=schedule.tenant, principal_id=schedule.principal
        )
        goals = [
            spec.goal
            for spec in STARTER_SHELF
            if spec.key in ("calendar", "tasks")
        ]
        stamp = {
            "schedule_id": schedule.schedule_id,
            "occurrence": occurrence,
            "label": schedule.label,
            "skipped": skipped,
        }
        lines: list[str] = []
        run_ids: list[str] = []
        for goal in goals:
            try:
                run = self._start_intent_run(session, goal, pulse=stamp)
            except GatewayError:
                continue  # a deleted starter is a quieter morning
            except Exception:  # noqa: BLE001 - the tick must keep serving
                logging.getLogger("oolu.gateway").exception(
                    "morning pulse run failed for %s", schedule.schedule_id
                )
                continue
            run_ids.append(str(run["run_id"]))
            state = next(
                (
                    s
                    for s in self._durable.runs.list(limit=10_000)
                    if s.run_id == run["run_id"]
                ),
                None,
            )
            result = (
                self._completed_result(state) if state is not None else None
            )
            answer = str((result or {}).get("answer") or "").strip()
            if answer:
                lines.append(answer)
        if not lines or self._reminders is None:
            self._durable.audit.append(
                "pulse.fire_failed",
                {
                    "schedule_id": schedule.schedule_id,
                    "occurrence": occurrence,
                    "tenant": schedule.tenant,
                    "principal": schedule.principal,
                    "goal": schedule.goal,
                    "code": "morning_empty",
                    "reason": (
                        "no reminder channel on this host"
                        if self._reminders is None
                        else "no starter answered — were they deleted?"
                    ),
                },
            )
            return
        combined = "Good morning — the day's shape: " + " • ".join(lines)
        try:
            self._reminders.add(
                tenant=schedule.tenant,
                principal=schedule.principal,
                text=combined[:490],
                due_at=(self._clock() + timedelta(minutes=2)),
            )
        except ValueError:
            pass  # a full reminder book is not a failed morning
        if run_ids:
            self._pulse.set_claim_run(
                schedule.schedule_id, occurrence, run_ids[0]
            )
        self._durable.audit.append(
            "pulse.fired",
            {
                "schedule_id": schedule.schedule_id,
                "occurrence": occurrence,
                "run_id": ",".join(run_ids),
                "skipped": skipped,
                "tenant": schedule.tenant,
                "principal": schedule.principal,
                "goal": schedule.goal,
            },
        )

    def _fire_schedule(self, schedule, occurrence: str, skipped: int) -> None:
        """One scheduled fire: an ORDINARY run, submitted as the owner
        through the standing doors — audited, metered, walled, and
        inbox-visible on failure exactly like a hand-started run. A
        refusal (the goal no longer resolves, the tenant is over its
        cap) is audited, never raised into the serving request. The
        morning pulse's own goal fires its composite instead."""
        from ..nodeplace.personal_templates import MORNING_PULSE_GOAL
        from ..press import EDITION_PULSE_GOAL

        if schedule.goal == MORNING_PULSE_GOAL:
            self._fire_morning(schedule, occurrence, skipped)
            return
        if schedule.goal == EDITION_PULSE_GOAL:
            self._fire_edition(schedule, occurrence, skipped)
            return
        from ..marketplace import MARKET_PULSE_GOAL

        if schedule.goal == MARKET_PULSE_GOAL:
            self._fire_market_brief(schedule, occurrence, skipped)
            return
        from ..explorer import EXPLORER_BRIEF_PREFIX

        if schedule.goal.startswith(EXPLORER_BRIEF_PREFIX):
            self._fire_explorer(schedule, occurrence, skipped)
            return
        from types import SimpleNamespace

        session = SimpleNamespace(
            tenant_id=schedule.tenant, principal_id=schedule.principal
        )
        stamp = {
            "schedule_id": schedule.schedule_id,
            "occurrence": occurrence,
            "label": schedule.label,
            "skipped": skipped,
        }
        try:
            run = self._start_intent_run(session, schedule.goal, pulse=stamp)
        except GatewayError as exc:
            self._durable.audit.append(
                "pulse.fire_failed",
                {
                    "schedule_id": schedule.schedule_id,
                    "occurrence": occurrence,
                    "tenant": schedule.tenant,
                    "principal": schedule.principal,
                    "goal": schedule.goal,
                    "code": exc.code,
                    "reason": exc.message,
                },
            )
            return
        except Exception as exc:  # noqa: BLE001 - the tick must keep serving
            logging.getLogger("oolu.gateway").exception(
                "pulse fire failed for schedule %s", schedule.schedule_id
            )
            self._durable.audit.append(
                "pulse.fire_failed",
                {
                    "schedule_id": schedule.schedule_id,
                    "occurrence": occurrence,
                    "tenant": schedule.tenant,
                    "principal": schedule.principal,
                    "goal": schedule.goal,
                    "code": "internal",
                    "reason": str(exc),
                },
            )
            return
        self._pulse.set_claim_run(
            schedule.schedule_id, occurrence, str(run["run_id"])
        )
        self._durable.audit.append(
            "pulse.fired",
            {
                "schedule_id": schedule.schedule_id,
                "occurrence": occurrence,
                "run_id": str(run["run_id"]),
                "skipped": skipped,
                "tenant": schedule.tenant,
                "principal": schedule.principal,
                "goal": schedule.goal,
            },
        )

    def _pulse_row(self, schedule, now) -> dict:
        from ..pulse import next_occurrence, speak_rhythm

        hh, mm = divmod(schedule.at_minute, 60)
        return {
            "schedule_id": schedule.schedule_id,
            "cadence": schedule.cadence,
            "at": f"{hh:02d}:{mm:02d}",
            "weekday": schedule.weekday,
            "day_of_month": schedule.day_of_month,
            "month": schedule.month,
            "day": schedule.day,
            "tz_offset_minutes": schedule.tz_offset_minutes,
            "goal": schedule.goal,
            "label": schedule.label,
            "enabled": schedule.enabled,
            "rhythm": speak_rhythm(schedule),
            "created_at": schedule.created_at.isoformat(),
            # The next local occurrence — on the schedule's own clock.
            "next_at_local": next_occurrence(schedule, now),
            "last": self._pulse.newest_claim(schedule.schedule_id),
        }

    _PULSE_ACT_RE = re.compile(
        r"^(?P<verb>cancel|delete|stop|enable|disable|pause|resume)"
        r"\s+(?:the\s+)?schedule\s+(?P<which>\S+)$",
        re.I,
    )
    _PULSE_LIST = frozenset(
        {"schedules", "my schedules", "list schedules", "show schedules"}
    )
    _MORNING_ON = frozenset(
        {"turn on the morning pulse", "enable the morning pulse",
         "start the morning pulse", "morning pulse on"}
    )
    _MORNING_OFF = frozenset(
        {"turn off the morning pulse", "disable the morning pulse",
         "pause the morning pulse", "stop the morning pulse",
         "morning pulse off"}
    )

    def _answer_morning_switch(self, session, *, enabled: bool, now) -> str:
        """P4 — the one-sentence switch: find the seeded morning-pulse
        schedule and flip it, reading the store back."""
        from ..nodeplace.personal_templates import MORNING_PULSE_GOAL
        from ..pulse import next_occurrence

        mine = [
            s
            for s in self._pulse.list_for(
                session.tenant_id, session.principal_id
            )
            if s.goal == MORNING_PULSE_GOAL
        ]
        if not mine:
            return (
                "There's no morning pulse on your desk — it arrives with "
                "the starter shelf."
            )
        updated = self._pulse.set_enabled(
            mine[0].schedule_id,
            tenant=session.tenant_id,
            principal=session.principal_id,
            enabled=enabled,
            now=now,
        )
        self._durable.audit.append(
            "pulse.toggled",
            {
                "schedule_id": mine[0].schedule_id,
                "tenant": session.tenant_id,
                "principal": session.principal_id,
                "enabled": enabled,
            },
        )
        if enabled and updated is not None:
            return (
                "The morning pulse is ON — every day at 09:00 I'll run "
                "your Calendar and Tasks and bring you the day's shape. "
                f"First one {next_occurrence(updated, now)} (your clock). "
                "“turn off the morning pulse” stops it."
            )
        return (
            "The morning pulse is OFF. “turn on the morning pulse” "
            "starts it again — the paused mornings stay skipped."
        )

    def _pulse_command(self, session, message, body, now) -> str | None:
        """The pulse's deterministic ear: create, list, cancel, pause,
        and resume — each answered by reading the STORE back, never by
        narration. Anything doubtful returns None and stays ordinary
        conversation."""
        from ..pulse import speak_rhythm, spoken_schedule

        text = (message or "").strip()
        lowered = text.casefold().rstrip(".!")
        if lowered in self._MORNING_ON or lowered in self._MORNING_OFF:
            return self._answer_morning_switch(
                session, enabled=lowered in self._MORNING_ON, now=now
            )
        if lowered in self._PULSE_LIST:
            rows = self._pulse.list_for(
                session.tenant_id, session.principal_id
            )
            if not rows:
                return (
                    "No standing schedules. Say “every day at 9 run …” "
                    "and I'll keep the rhythm."
                )
            spoken = "\n".join(
                f"• {speak_rhythm(s)} — {s.goal}"
                f" ({s.schedule_id[:8]}"
                + ("" if s.enabled else ", paused")
                + ")"
                for s in rows
            )
            return (
                f"Standing schedules:\n{spoken}\n"
                "“cancel schedule <id>” stops one; “pause schedule <id>” "
                "and “resume schedule <id>” hold and release it."
            )
        act = self._PULSE_ACT_RE.match(lowered)
        if act:
            which = act.group("which")
            mine = [
                s
                for s in self._pulse.list_for(
                    session.tenant_id, session.principal_id
                )
                if s.schedule_id.startswith(which)
            ]
            if len(mine) != 1:
                return (
                    "I don't know which schedule you mean — say "
                    "“schedules” and use the id in the parentheses."
                )
            schedule = mine[0]
            verb = act.group("verb")
            if verb in ("cancel", "delete", "stop"):
                self._pulse.delete(
                    schedule.schedule_id,
                    tenant=session.tenant_id,
                    principal=session.principal_id,
                )
                self._durable.audit.append(
                    "pulse.cancelled",
                    {
                        "schedule_id": schedule.schedule_id,
                        "tenant": session.tenant_id,
                        "principal": session.principal_id,
                    },
                )
                return (
                    f"Cancelled: {speak_rhythm(schedule)} — "
                    f"{schedule.goal}."
                )
            enabled = verb in ("enable", "resume")
            updated = self._pulse.set_enabled(
                schedule.schedule_id,
                tenant=session.tenant_id,
                principal=session.principal_id,
                enabled=enabled,
                now=now,
            )
            self._durable.audit.append(
                "pulse.toggled",
                {
                    "schedule_id": schedule.schedule_id,
                    "tenant": session.tenant_id,
                    "principal": session.principal_id,
                    "enabled": enabled,
                },
            )
            if updated is not None and enabled:
                from ..pulse import next_occurrence

                return (
                    f"Resumed: {speak_rhythm(updated)} — {updated.goal}. "
                    f"Next fire {next_occurrence(updated, now)} (its own "
                    "clock); the paused stretch stays skipped."
                )
            return (
                f"Paused: {speak_rhythm(schedule)} — {schedule.goal}. "
                "“resume schedule "
                f"{schedule.schedule_id[:8]}” starts it again."
            )
        spec = spoken_schedule(text)
        if spec is None:
            return None
        try:
            schedule = self._pulse.add(
                session.tenant_id,
                session.principal_id,
                tz_offset_minutes=_tz_minutes(
                    (body or {}).get("tz_offset_minutes")
                ),
                label=text[: 200],
                now=now,
                **spec,
            )
        except ValueError as exc:
            return f"I couldn't set that schedule: {exc}"
        self._durable.audit.append(
            "pulse.created",
            {
                "schedule_id": schedule.schedule_id,
                "tenant": session.tenant_id,
                "principal": session.principal_id,
                "goal": schedule.goal,
                "cadence": schedule.cadence,
            },
        )
        from ..pulse import next_occurrence

        say = (
            f"Done — {speak_rhythm(schedule)} I'll run: {schedule.goal}. "
            f"First fire {next_occurrence(schedule, now)} (your clock)."
        )
        function = self._resolve_node_function(session, schedule.goal)
        if function is not None:
            say += f" That's your node “{function['title']}”."
        say += (
            f" “cancel schedule {schedule.schedule_id[:8]}” stops it."
        )
        return say

    def _pulse_view(self, request, session, params) -> Response:
        now = request.now or self._clock()
        return json_response(
            200,
            {
                "items": [
                    self._pulse_row(s, now)
                    for s in self._pulse.list_for(
                        session.tenant_id, session.principal_id
                    )
                ]
            },
        )

    def _pulse_create(self, request, session, params) -> Response:
        body = request.body or {}
        at = str(body.get("at") or "")
        try:
            hh, _, mm = at.partition(":")
            at_minute = int(hh) * 60 + int(mm or 0)
        except (TypeError, ValueError):
            raise GatewayError(
                400, "bad_request", 'give the time as "HH:MM"'
            ) from None
        try:
            schedule = self._pulse.add(
                session.tenant_id,
                session.principal_id,
                cadence=str(body.get("cadence") or ""),
                at_minute=at_minute,
                goal=str(body.get("goal") or ""),
                weekday=body.get("weekday"),
                day_of_month=body.get("day_of_month"),
                month=body.get("month"),
                day=body.get("day"),
                tz_offset_minutes=_tz_minutes(body.get("tz_offset_minutes")),
                label=str(body.get("label") or ""),
                enabled=bool(body.get("enabled", True)),
                now=request.now or self._clock(),
            )
        except ValueError as exc:
            raise GatewayError(400, "bad_request", str(exc)) from exc
        self._durable.audit.append(
            "pulse.created",
            {
                "schedule_id": schedule.schedule_id,
                "tenant": session.tenant_id,
                "principal": session.principal_id,
                "goal": schedule.goal,
                "cadence": schedule.cadence,
            },
        )
        return json_response(
            201, self._pulse_row(schedule, request.now or self._clock())
        )

    def _pulse_toggle(self, request, session, params) -> Response:
        enabled = bool((request.body or {}).get("enabled", True))
        schedule = self._pulse.set_enabled(
            params["schedule_id"],
            tenant=session.tenant_id,
            principal=session.principal_id,
            enabled=enabled,
            now=request.now or self._clock(),
        )
        if schedule is None:
            raise GatewayError(404, "not_found", "no such schedule")
        self._durable.audit.append(
            "pulse.toggled",
            {
                "schedule_id": schedule.schedule_id,
                "tenant": session.tenant_id,
                "principal": session.principal_id,
                "enabled": enabled,
            },
        )
        return json_response(
            200, self._pulse_row(schedule, request.now or self._clock())
        )

    def _pulse_delete(self, request, session, params) -> Response:
        removed = self._pulse.delete(
            params["schedule_id"],
            tenant=session.tenant_id,
            principal=session.principal_id,
        )
        if not removed:
            raise GatewayError(404, "not_found", "no such schedule")
        self._durable.audit.append(
            "pulse.cancelled",
            {
                "schedule_id": params["schedule_id"],
                "tenant": session.tenant_id,
                "principal": session.principal_id,
            },
        )
        return json_response(200, {"cancelled": params["schedule_id"]})

    # ------------------------------------------------------------------ #
    # The commercial spine (marketplace-build-plan M0). Intents and       #
    # digest-bound approvals; deliberately no execution door — M1's       #
    # order service is the first consumer of an authorization.            #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _commerce_error(exc: MarketplaceError) -> GatewayError:
        if isinstance(exc, MarketNotFound):
            return GatewayError(404, "not_found", str(exc))
        if isinstance(exc, StrongAuthenticationRequired):
            return GatewayError(403, "step_up_required", str(exc))
        if isinstance(exc, SellerUnverified):
            return GatewayError(403, "seller_unverified", str(exc))
        if isinstance(exc, SelfApproval):
            return GatewayError(403, "self_approval", str(exc))
        if isinstance(exc, ProtocolViolation):
            return GatewayError(400, "protocol_violation", str(exc))
        return GatewayError(409, "conflict", str(exc))

    def _commerce_seller_verified(self, tenant: str, principal: str) -> bool:
        """The M1 seller gate: a seller is verified when their own
        seller-KYC application was approved by a human reviewer. No
        record, no publication; refusal is the default, not trust."""
        return self._commerce_seller_kyc.is_verified(
            tenant=tenant, principal=principal
        )

    @staticmethod
    def _commerce_intent_view(stored) -> dict:
        return {
            "intent": stored.intent.model_dump(mode="json"),
            "state": stored.state,
            "intent_digest": stored.digest,
            "verdict": stored.verdict.model_dump(mode="json"),
        }

    def _commerce_policy_get(self, request, session, params) -> Response:
        policy = self._commerce.purchase_policy(
            tenant=session.tenant_id, principal=session.principal_id
        )
        return json_response(200, policy.model_dump(mode="json"))

    def _commerce_policy_put(self, request, session, params) -> Response:
        try:
            policy = PurchasePolicy.model_validate(request.body or {})
        except ValidationError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        self._commerce.set_purchase_policy(
            policy, tenant=session.tenant_id, principal=session.principal_id
        )
        return json_response(200, policy.model_dump(mode="json"))

    def _commerce_delegations_list(self, request, session, params) -> Response:
        records = self._commerce.delegations.list_for(
            tenant=session.tenant_id, principal=session.principal_id
        )
        return json_response(
            200, {"items": [r.model_dump(mode="json") for r in records]}
        )

    def _commerce_delegation_grant(self, request, session, params) -> Response:
        body = dict(request.body or {})
        body.setdefault("delegation_id", uuid4().hex)
        # The delegation binds to the AUTHENTICATED principal — a caller
        # cannot mint authority for someone else by naming them.
        body["tenant_id"] = session.tenant_id
        body["principal_id"] = session.principal_id
        try:
            record = AgentDelegation.model_validate(body)
        except ValidationError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        self._commerce.grant_delegation(record)
        return json_response(201, record.model_dump(mode="json"))

    def _commerce_delegation_revoke(self, request, session, params) -> Response:
        try:
            blocked = self._commerce.revoke_delegation(
                params["delegation_id"],
                tenant=session.tenant_id,
                principal=session.principal_id,
                now=request.now or self._clock(),
            )
        except MarketplaceError as exc:
            raise self._commerce_error(exc) from exc
        return json_response(
            200,
            {"revoked": params["delegation_id"], "blocked_intents": blocked},
        )

    def _commerce_intents_list(self, request, session, params) -> Response:
        records = self._commerce.list_intents(
            tenant=session.tenant_id,
            principal=session.principal_id,
            state=request.query.get("state"),
        )
        return json_response(
            200, {"items": [self._commerce_intent_view(r) for r in records]}
        )

    def _commerce_intent_create(self, request, session, params) -> Response:
        body = request.body or {}
        try:
            offer = CommerceOffer.model_validate(body.get("offer") or {})
        except ValidationError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        idempotency_key = str(body.get("idempotency_key") or "")
        if not idempotency_key:
            raise GatewayError(
                400, "invalid_request", "idempotency_key is required"
            )
        maximum = body.get("maximum_total_micros")
        if maximum is not None:
            try:
                maximum = int(maximum)
            except (TypeError, ValueError):
                raise GatewayError(
                    400,
                    "invalid_request",
                    "maximum_total_micros must be a whole number of micros",
                ) from None
        risk_facts = body.get("risk_facts")
        if risk_facts is not None and not isinstance(risk_facts, dict):
            raise GatewayError(
                400, "invalid_request", "risk_facts must be an object"
            )
        permissions = body.get("data_permissions") or ()
        if not isinstance(permissions, (list, tuple)):
            raise GatewayError(
                400, "invalid_request", "data_permissions must be a list"
            )
        try:
            stored, created = self._commerce.create_purchase_intent(
                tenant=session.tenant_id,
                principal=session.principal_id,
                agent=str(body.get("agent_id") or "oolu"),
                offer=offer,
                idempotency_key=idempotency_key,
                now=request.now or self._clock(),
                category=str(body.get("category") or ""),
                delivery_destination=str(body.get("delivery_destination") or ""),
                data_permissions=tuple(str(p) for p in permissions),
                maximum_total_micros=maximum,
                risk_facts=risk_facts,
            )
        except (ValidationError, ValueError) as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        return json_response(
            201 if created else 200, self._commerce_intent_view(stored)
        )

    def _commerce_intent_get(self, request, session, params) -> Response:
        try:
            stored = self._commerce.get_intent(
                params["intent_id"], tenant=session.tenant_id
            )
        except MarketplaceError as exc:
            raise self._commerce_error(exc) from exc
        return json_response(200, self._commerce_intent_view(stored))

    def _commerce_approvals_inbox(self, request, session, params) -> Response:
        summaries = self._commerce.pending_approvals(
            tenant=session.tenant_id,
            principal=session.principal_id,
            now=request.now or self._clock(),
        )
        return json_response(200, {"items": summaries})

    def _commerce_intent_approve(self, request, session, params) -> Response:
        body = request.body or {}
        decision = str(body.get("decision") or "")
        if decision not in ("approve", "reject"):
            raise GatewayError(
                400, "invalid_request", "decision must be approve or reject"
            )
        try:
            record = self._commerce.record_approval(
                params["intent_id"],
                tenant=session.tenant_id,
                approver_id=session.principal_id,
                assurance_level=session.assurance_level,
                approve=decision == "approve",
                now=request.now or self._clock(),
            )
        except MarketplaceError as exc:
            raise self._commerce_error(exc) from exc
        stored = self._commerce.get_intent(
            params["intent_id"], tenant=session.tenant_id
        )
        return json_response(
            200,
            {
                "approval": record.model_dump(mode="json"),
                "state": stored.state,
            },
        )

    # ------------------------------------------------------------------ #
    # The fixed-price market (M1): catalog, orders, and the order's book. #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _commerce_order_view(stored) -> dict:
        return {
            "order": stored.record.model_dump(mode="json"),
            "state": stored.state,
        }

    def _commerce_seller_kyc_status(self, request, session, params) -> Response:
        record = self._commerce_seller_kyc.status_for(
            tenant=session.tenant_id, principal=session.principal_id
        )
        if record is None:
            return json_response(200, {"status": "not_applied"})
        return json_response(200, record.model_dump(mode="json"))

    def _commerce_seller_kyc_apply(self, request, session, params) -> Response:
        body = request.body or {}
        try:
            record = self._commerce_seller_kyc.apply(
                tenant=session.tenant_id,
                principal=session.principal_id,
                legal_name=str(body.get("legal_name") or ""),
                company_email=str(body.get("company_email") or ""),
                registration_no=str(body.get("registration_no") or ""),
            )
        except SellerKycError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        return json_response(201, record.model_dump(mode="json"))

    def _commerce_seller_kyc_queue(self, request, session, params) -> Response:
        records = self._commerce_seller_kyc.pending(tenant=session.tenant_id)
        return json_response(
            200, {"items": [r.model_dump(mode="json") for r in records]}
        )

    def _commerce_seller_kyc_decide(self, request, session, params) -> Response:
        """A human reviewer's verdict on a seller — approve authority
        required (the same authority seam every approval walks)."""
        if self._approval is None:
            raise GatewayError(
                404, "not_found", "approval authority is not configured"
            )
        body = request.body or {}
        principal = str(body.get("principal") or "")
        if not principal or "approved" not in body:
            raise GatewayError(
                400,
                "invalid_request",
                "principal and approved (true or false) are required",
            )
        current = self._commerce_seller_kyc.status_for(
            tenant=session.tenant_id, principal=principal
        )
        if current is None:
            raise GatewayError(404, "not_found", "no seller application here")
        try:
            self._approval.approve(
                session,
                run_id=f"seller-kyc:{session.tenant_id}:{principal}",
                policy="kyc.review",
                requester_id=current.applicant,
                required_assurance=int(body.get("required_assurance", 1)),
                now=request.now or self._clock(),
            )
        except AuthorizationError as exc:
            raise GatewayError(403, "forbidden", str(exc)) from exc
        try:
            record = self._commerce_seller_kyc.decide(
                tenant=session.tenant_id,
                principal=principal,
                reviewer=session.principal_id,
                approved=bool(body.get("approved")),
                note=str(body.get("note") or ""),
            )
        except SellerKycError as exc:
            raise GatewayError(409, "conflict", str(exc)) from exc
        return json_response(200, record.model_dump(mode="json"))

    def _commerce_catalog_browse(self, request, session, params) -> Response:
        return json_response(
            200,
            {
                "items": [
                    listing.model_dump(mode="json")
                    for listing in self._commerce_catalog.store.active()
                ]
            },
        )

    def _commerce_listings_list(self, request, session, params) -> Response:
        listings = self._commerce_catalog.store.for_seller(
            tenant=session.tenant_id, seller=session.principal_id
        )
        return json_response(
            200, {"items": [x.model_dump(mode="json") for x in listings]}
        )

    def _commerce_listing_create(self, request, session, params) -> Response:
        from ..marketplace import ListingMedia

        body = request.body or {}
        # Multimedia on the product: drawer refs, never copies, the wall
        # held at the door — photos, clips, or sound showing the real
        # thing (the press attachment law, applied to the shelf).
        file_ids = body.get("file_ids") or []
        if not isinstance(file_ids, list):
            raise GatewayError(400, "invalid_request", "file_ids must be a list")
        media = [
            ListingMedia(
                file_id=ref.file_id,
                blob_ref=ref.blob_ref,
                media_type=ref.media_type,
                name=ref.name,
            )
            for ref in self._drawer_refs(session, file_ids)
        ]
        try:
            listing = self._commerce_catalog.create_draft(
                tenant=session.tenant_id,
                seller_principal=session.principal_id,
                title=str(body.get("title") or ""),
                unit_price_micros=int(body.get("unit_price_micros") or 0),
                now=request.now or self._clock(),
                category=str(body.get("category") or ""),
                description=str(body.get("description") or ""),
                currency=str(body.get("currency") or "USD"),
                quantity_available=int(body.get("quantity_available") or 0),
                refund_terms=str(body.get("refund_terms") or "30-day returns"),
                fulfillment_terms=str(
                    body.get("fulfillment_terms") or "standard shipping"
                ),
                refundable=bool(body.get("refundable", True)),
                # The discount FACT's only source: a stated regular
                # price. Absent = no "was" price ever renders (A6).
                list_price_micros=(
                    int(body["list_price_micros"])
                    if body.get("list_price_micros") is not None
                    else None
                ),
                media=tuple(media),
            )
        except (ValidationError, ValueError, TypeError) as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        return json_response(201, listing.model_dump(mode="json"))

    def _commerce_listing_media(self, request, session, params) -> Response:
        """A listing's attached file, by reference — an ACTIVE listing is
        the public shelf (publication is the consent that crosses the
        drawer wall); a draft's media is the seller's own only."""
        listing = self._commerce_catalog.store.get(params["listing_id"])
        if listing is None or (
            listing.status != "active"
            and not (
                listing.tenant_id == session.tenant_id
                and listing.seller_principal == session.principal_id
            )
        ):
            raise GatewayError(404, "not_found", "no such listing")
        try:
            ref = listing.media[int(params["index"])]
        except (IndexError, ValueError):
            raise GatewayError(404, "not_found", "no such attachment") from None
        file = (
            self._files.get(ref.file_id, tenant=listing.tenant_id)
            if self._files is not None
            else None
        )
        if file is None:
            raise GatewayError(404, "not_found", "the referenced file is gone")
        return self._serve_drawer_bytes(file)

    # ------------------------------------------------------------------ #
    # The market desk: position meets demand, and the list-out.           #
    # ------------------------------------------------------------------ #
    def _market_desk_items(self, session, now):
        """The member's brief, gathered from the standing stores and
        judged by the deterministic briefing — the gateway only fetches;
        the matching lives in marketplace/briefing.py."""
        from ..marketplace import desk_briefing

        open_rfqs = self._commerce_rfq.open_requests(
            tenant=session.tenant_id, now=now
        )
        quote_counts = {
            r.rfq_id: len(
                self._commerce_rfq.quotes(r.rfq_id, tenant=session.tenant_id)
            )
            for r in open_rfqs
            if r.buyer_principal == session.principal_id
        }
        return desk_briefing(
            principal=session.principal_id,
            approvals=self._commerce.pending_approvals(
                tenant=session.tenant_id,
                principal=session.principal_id,
                now=now,
            ),
            orders=self._commerce_orders.orders.list_for(
                tenant=session.tenant_id, principal=session.principal_id
            ),
            open_rfqs=open_rfqs,
            my_listings=self._commerce_catalog.store.for_seller(
                tenant=session.tenant_id, seller=session.principal_id
            ),
            active_listings=self._commerce_catalog.store.active(),
            quote_counts=quote_counts,
        )

    def _market_brief_schedule_row(self, session):
        from ..marketplace import MARKET_PULSE_GOAL

        for schedule in self._pulse.list_for(
            session.tenant_id, session.principal_id
        ):
            if schedule.goal == MARKET_PULSE_GOAL:
                return schedule
        return None

    def _commerce_desk(self, request, session, params) -> Response:
        """The desk brief on demand — the same items the pulse pushes —
        and the standing brief schedule, named."""
        now = request.now or self._clock()
        items = self._market_desk_items(session, now)
        schedule = self._market_brief_schedule_row(session)
        return json_response(
            200,
            {
                "items": [
                    {"kind": i.kind, "ref": i.ref, "text": i.text}
                    for i in items
                ],
                "brief_schedule": (
                    self._pulse_row(schedule, now)
                    if schedule is not None
                    else None
                ),
            },
        )

    def _commerce_desk_schedule(self, request, session, params) -> Response:
        """The member's desk-brief rhythm: one standing pulse schedule
        with the market sentinel goal — created, retimed, or removed
        through this one door (the morning-edition shape)."""
        from ..marketplace import MARKET_BRIEF_LABEL, MARKET_PULSE_GOAL

        body = request.body or {}
        standing = self._market_brief_schedule_row(session)
        if body.get("enabled") is False:
            if standing is not None:
                self._pulse.delete(
                    standing.schedule_id,
                    tenant=session.tenant_id,
                    principal=session.principal_id,
                )
            return json_response(200, {"brief_schedule": None})
        at_minute = int(body.get("at_minute", 9 * 60))
        tz_offset = _tz_minutes(body.get("tz_offset_minutes"))
        if standing is not None:
            self._pulse.delete(
                standing.schedule_id,
                tenant=session.tenant_id,
                principal=session.principal_id,
            )
        try:
            schedule = self._pulse.add(
                session.tenant_id,
                session.principal_id,
                cadence="daily",
                at_minute=at_minute,
                goal=MARKET_PULSE_GOAL,
                tz_offset_minutes=tz_offset,
                label=MARKET_BRIEF_LABEL,
                now=request.now or self._clock(),
            )
        except ValueError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        return json_response(
            200,
            {
                "brief_schedule": self._pulse_row(
                    schedule, request.now or self._clock()
                )
            },
        )

    def _commerce_mine(self, request, session, params) -> Response:
        """The list-out: everything the caller created on the platform,
        grouped and named — listings, requests, orders, recurring
        obligations, delegations. What a member made is never
        invisible."""
        listings = self._commerce_catalog.store.for_seller(
            tenant=session.tenant_id, seller=session.principal_id
        )
        rfqs = self._commerce_rfq.mine(
            tenant=session.tenant_id, buyer=session.principal_id
        )
        orders = self._commerce_orders.orders.list_for(
            tenant=session.tenant_id, principal=session.principal_id
        )
        recurring = self._commerce_recurring.list_for(
            tenant=session.tenant_id, principal=session.principal_id
        )
        delegations = self._commerce.delegations.list_for(
            tenant=session.tenant_id, principal=session.principal_id
        )
        return json_response(
            200,
            {
                "listings": [x.model_dump(mode="json") for x in listings],
                "requests": [r.model_dump(mode="json") for r in rfqs],
                "orders": [self._commerce_order_view(o) for o in orders],
                "recurring": [r.model_dump(mode="json") for r in recurring],
                "delegations": [
                    d.model_dump(mode="json") for d in delegations
                ],
            },
        )

    def _fire_market_brief(self, schedule, occurrence: str, skipped: int) -> None:
        """The desk brief fires: gather the member's items and land them
        as the Market agent's OWN thread message, with a short reminder
        ping. An empty brief is silence — the desk never invents
        urgency. Failures are audited, never raised into the tick."""
        from types import SimpleNamespace

        from ..marketplace import briefing_message

        session = SimpleNamespace(
            tenant_id=schedule.tenant, principal_id=schedule.principal
        )
        try:
            items = self._market_desk_items(session, self._clock())
            if items and self._assistant_history is not None:
                self._assistant_history.append(
                    tenant=schedule.tenant,
                    principal=schedule.principal,
                    kind="assistant",
                    body=briefing_message(items, skipped=skipped),
                    agent="market",
                )
            if items and self._reminders is not None:
                try:
                    self._reminders.add(
                        tenant=schedule.tenant,
                        principal=schedule.principal,
                        text=(
                            f"The market desk sees {len(items)} "
                            f"thing{'s' if len(items) != 1 else ''} for "
                            "you — in the Market thread."
                        )[:490],
                        due_at=(self._clock() + timedelta(minutes=2)),
                    )
                except ValueError:
                    pass  # a full reminder book is not a failed brief
            self._durable.audit.append(
                "pulse.fired",
                {
                    "schedule_id": schedule.schedule_id,
                    "occurrence": occurrence,
                    "run_id": "",
                    "skipped": skipped,
                    "tenant": schedule.tenant,
                    "principal": schedule.principal,
                    "goal": schedule.goal,
                },
            )
        except Exception:  # noqa: BLE001 - the tick must keep serving
            logging.getLogger("oolu.gateway").exception(
                "market brief pulse failed for %s", schedule.schedule_id
            )
            self._durable.audit.append(
                "pulse.fire_failed",
                {
                    "schedule_id": schedule.schedule_id,
                    "occurrence": occurrence,
                    "tenant": schedule.tenant,
                    "principal": schedule.principal,
                    "goal": schedule.goal,
                },
            )

    def _commerce_listing_publish(self, request, session, params) -> Response:
        try:
            listing = self._commerce_catalog.publish(
                params["listing_id"],
                tenant=session.tenant_id,
                seller=session.principal_id,
                now=request.now or self._clock(),
            )
        except MarketplaceError as exc:
            raise self._commerce_error(exc) from exc
        return json_response(200, listing.model_dump(mode="json"))

    def _commerce_listing_offer(self, request, session, params) -> Response:
        body = request.body or {}
        try:
            offer = self._commerce_catalog.offer_for(
                params["listing_id"],
                quantity=int(body.get("quantity") or 1),
                now=request.now or self._clock(),
            )
        except MarketplaceError as exc:
            raise self._commerce_error(exc) from exc
        except (ValueError, TypeError) as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        return json_response(200, offer.model_dump(mode="json"))

    # ------------------------------------------------------------------ #
    # M2: RFQ and quotes, seller automation, evidence, invoices.          #
    # ------------------------------------------------------------------ #
    def _commerce_rfqs_list(self, request, session, params) -> Response:
        requests = self._commerce_rfq.open_requests(
            tenant=session.tenant_id, now=request.now or self._clock()
        )
        return json_response(
            200, {"items": [r.model_dump(mode="json") for r in requests]}
        )

    def _commerce_rfq_open(self, request, session, params) -> Response:
        body = request.body or {}
        attributes = body.get("required_attributes") or {}
        if not isinstance(attributes, dict):
            raise GatewayError(
                400, "invalid_request", "required_attributes must be an object"
            )
        try:
            specification = RfqSpecification(
                category=str(body.get("category") or ""),
                required_attributes=tuple(
                    sorted((str(k), str(v)) for k, v in attributes.items())
                ),
                quantity=int(body.get("quantity") or 1),
                destination_reference=str(body.get("destination_reference") or ""),
            )
            rfq = self._commerce_rfq.open(
                tenant=session.tenant_id,
                buyer=session.principal_id,
                specification=specification,
                now=request.now or self._clock(),
            )
        except (ValidationError, ValueError, TypeError) as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        return json_response(201, rfq.model_dump(mode="json"))

    def _commerce_quotes_list(self, request, session, params) -> Response:
        try:
            quotes = self._commerce_rfq.quotes(
                params["rfq_id"], tenant=session.tenant_id
            )
        except MarketplaceError as exc:
            raise self._commerce_error(exc) from exc
        return json_response(
            200, {"items": [q.model_dump(mode="json") for q in quotes]}
        )

    def _commerce_quote_submit(self, request, session, params) -> Response:
        body = request.body or {}
        try:
            offer = CommerceOffer.model_validate(body.get("offer") or {})
        except ValidationError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        attributes = body.get("attributes") or {}
        if not isinstance(attributes, dict):
            raise GatewayError(
                400, "invalid_request", "attributes must be an object"
            )
        try:
            quote = self._commerce_rfq.submit_quote(
                params["rfq_id"],
                tenant=session.tenant_id,
                seller_principal=session.principal_id,
                offer=offer,
                attributes={str(k): str(v) for k, v in attributes.items()},
                # The seller's own signed automation boundary gates the
                # quote — the absolute floor refuses without discretion.
                sales_policy=self._commerce_sales_policies.get(
                    tenant=session.tenant_id, principal=session.principal_id
                ),
                now=request.now or self._clock(),
            )
        except MarketplaceError as exc:
            raise self._commerce_error(exc) from exc
        return json_response(201, quote.model_dump(mode="json"))

    def _commerce_rfq_award(self, request, session, params) -> Response:
        body = request.body or {}
        quote_id = str(body.get("quote_id") or "")
        if not quote_id:
            raise GatewayError(400, "invalid_request", "quote_id is required")
        try:
            offer = self._commerce_rfq.award(
                params["rfq_id"],
                quote_id,
                tenant=session.tenant_id,
                buyer=session.principal_id,
                now=request.now or self._clock(),
            )
        except MarketplaceError as exc:
            raise self._commerce_error(exc) from exc
        return json_response(200, offer.model_dump(mode="json"))

    def _commerce_sales_policy_get(self, request, session, params) -> Response:
        policy = self._commerce_sales_policies.get(
            tenant=session.tenant_id, principal=session.principal_id
        )
        return json_response(200, policy.model_dump(mode="json"))

    def _commerce_sales_policy_put(self, request, session, params) -> Response:
        try:
            policy = SalesPolicy.model_validate(request.body or {})
        except ValidationError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        self._commerce_sales_policies.put(
            policy, tenant=session.tenant_id, principal=session.principal_id
        )
        self._durable.audit.append(
            "market.sales_policy.updated",
            {
                "tenant": session.tenant_id,
                "principal": session.principal_id,
                "policy_version": policy.policy_version,
            },
        )
        return json_response(200, policy.model_dump(mode="json"))

    def _commerce_order_evidence(self, request, session, params) -> Response:
        body = request.body or {}
        return self._commerce_order_transition(
            request,
            session,
            params,
            self._commerce_orders.attach_evidence,
            evidence=self._commerce_evidence_ref(params, body),
        )

    def _commerce_order_invoice(self, request, session, params) -> Response:
        stored = self._commerce_party_order(session, params["order_id"])
        invoice = self._commerce_invoices.for_order(stored.record.order_id)
        if invoice is None:
            raise GatewayError(404, "not_found", "no invoice yet")
        return json_response(200, invoice.model_dump(mode="json"))

    # ------------------------------------------------------------------ #
    # M3: milestones, recurring, payout changes, jobs, reconciliation.    #
    # ------------------------------------------------------------------ #
    def _commerce_milestones_list(self, request, session, params) -> Response:
        stored = self._commerce_party_order(session, params["order_id"])
        book = self._commerce_orders.milestones.for_order(stored.record.order_id)
        return json_response(
            200, {"items": [m.model_dump(mode="json") for m in book]}
        )

    def _commerce_milestone_index(self, params) -> int:
        try:
            return int(params["index"])
        except (TypeError, ValueError):
            raise GatewayError(
                400, "invalid_request", "the milestone index must be a number"
            ) from None

    def _commerce_milestone_deliver(self, request, session, params) -> Response:
        body = request.body or {}
        try:
            milestone = self._commerce_orders.deliver_milestone(
                params["order_id"],
                tenant=session.tenant_id,
                actor=session.principal_id,
                index=self._commerce_milestone_index(params),
                evidence=self._commerce_evidence_ref(params, body),
                now=request.now or self._clock(),
            )
        except MarketplaceError as exc:
            raise self._commerce_error(exc) from exc
        except PaymentError as exc:
            raise GatewayError(402, "payment_declined", str(exc)) from exc
        return json_response(200, milestone.model_dump(mode="json"))

    def _commerce_milestone_accept(self, request, session, params) -> Response:
        try:
            milestone = self._commerce_orders.accept_milestone(
                params["order_id"],
                tenant=session.tenant_id,
                actor=session.principal_id,
                index=self._commerce_milestone_index(params),
                now=request.now or self._clock(),
            )
        except MarketplaceError as exc:
            raise self._commerce_error(exc) from exc
        return json_response(200, milestone.model_dump(mode="json"))

    def _commerce_milestone_fail(self, request, session, params) -> Response:
        try:
            milestone = self._commerce_orders.fail_milestone(
                params["order_id"],
                tenant=session.tenant_id,
                actor=session.principal_id,
                index=self._commerce_milestone_index(params),
                now=request.now or self._clock(),
                reason=str((request.body or {}).get("reason") or ""),
            )
        except MarketplaceError as exc:
            raise self._commerce_error(exc) from exc
        return json_response(200, milestone.model_dump(mode="json"))

    def _commerce_refund_unreleased(self, request, session, params) -> Response:
        return self._commerce_order_transition(
            request,
            session,
            params,
            self._commerce_orders.refund_unreleased,
            reason=str((request.body or {}).get("reason") or ""),
        )

    def _commerce_order_adjudicate(self, request, session, params) -> Response:
        """The marketplace's verdict on a dispute — approve authority
        required, the same seam every consequential decision walks."""
        if self._approval is None:
            raise GatewayError(
                404, "not_found", "approval authority is not configured"
            )
        stored = self._commerce_orders.orders.get(
            params["order_id"], tenant=session.tenant_id
        )
        if stored is None:
            raise GatewayError(404, "not_found", "no such order")
        body = request.body or {}
        try:
            self._approval.approve(
                session,
                run_id=f"dispute:{session.tenant_id}:{params['order_id']}",
                policy="dispute.adjudication",
                requester_id=stored.record.buyer_principal,
                required_assurance=int(body.get("required_assurance", 1)),
                now=request.now or self._clock(),
            )
        except AuthorizationError as exc:
            raise GatewayError(403, "forbidden", str(exc)) from exc
        amount = body.get("amount_micros")
        try:
            order = self._commerce_orders.adjudicate(
                params["order_id"],
                tenant=session.tenant_id,
                adjudicator=session.principal_id,
                outcome=str(body.get("outcome") or ""),
                now=request.now or self._clock(),
                amount_micros=int(amount) if amount is not None else None,
                note=str(body.get("note") or ""),
            )
        except MarketplaceError as exc:
            raise self._commerce_error(exc) from exc
        except PaymentError as exc:
            raise GatewayError(402, "payment_declined", str(exc)) from exc
        except (TypeError, ValueError) as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        return json_response(200, self._commerce_order_view(order))

    def _commerce_recurring_list(self, request, session, params) -> Response:
        records = self._commerce_recurring.list_for(
            tenant=session.tenant_id, principal=session.principal_id
        )
        return json_response(
            200, {"items": [r.model_dump(mode="json") for r in records]}
        )

    def _commerce_recurring_create(self, request, session, params) -> Response:
        body = request.body or {}
        intent_id = str(body.get("intent_id") or "")
        if not intent_id:
            raise GatewayError(400, "invalid_request", "intent_id is required")
        try:
            obligation = self._commerce_recurring.create_from_intent(
                self._commerce,
                intent_id,
                tenant=session.tenant_id,
                period_days=int(body.get("period_days") or 30),
                now=request.now or self._clock(),
            )
        except MarketplaceError as exc:
            raise self._commerce_error(exc) from exc
        except (TypeError, ValueError) as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        return json_response(201, obligation.model_dump(mode="json"))

    def _commerce_recurring_renew(self, request, session, params) -> Response:
        body = request.body or {}
        try:
            obligation = self._commerce_recurring.get(
                params["obligation_id"], tenant=session.tenant_id
            )
            if obligation is None:
                raise GatewayError(404, "not_found", "no such obligation")
            current = obligation.offer
            if body.get("current_offer") is not None:
                current = CommerceOffer.model_validate(body["current_offer"])
            renewal = self._commerce_recurring.renew(
                self._commerce,
                params["obligation_id"],
                tenant=session.tenant_id,
                current_offer=current,
                now=request.now or self._clock(),
            )
        except ValidationError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        except MarketplaceError as exc:
            raise self._commerce_error(exc) from exc
        return json_response(201, self._commerce_intent_view(renewal))

    def _commerce_recurring_cancel(self, request, session, params) -> Response:
        try:
            obligation = self._commerce_recurring.cancel(
                params["obligation_id"],
                tenant=session.tenant_id,
                principal=session.principal_id,
                now=request.now or self._clock(),
            )
        except MarketplaceError as exc:
            raise self._commerce_error(exc) from exc
        return json_response(200, obligation.model_dump(mode="json"))

    def _commerce_payout_list(self, request, session, params) -> Response:
        records = self._commerce_payout_changes.list_for(
            tenant=session.tenant_id
        )
        return json_response(
            200, {"items": [r.model_dump(mode="json") for r in records]}
        )

    def _commerce_payout_request(self, request, session, params) -> Response:
        body = request.body or {}
        new_destination = str(body.get("new_destination") or "")
        if not new_destination:
            raise GatewayError(
                400, "invalid_request", "new_destination is required"
            )
        record = self._commerce_payout_changes.request(
            tenant=session.tenant_id,
            principal=session.principal_id,
            current_destination=str(body.get("current_destination") or ""),
            new_destination=new_destination,
            now=request.now or self._clock(),
        )
        return json_response(201, record.model_dump(mode="json"))

    def _commerce_payout_approve(self, request, session, params) -> Response:
        try:
            record = self._commerce_payout_changes.approve(
                params["request_id"],
                tenant=session.tenant_id,
                approver=session.principal_id,
                assurance_level=session.assurance_level,
                now=request.now or self._clock(),
            )
        except MarketplaceError as exc:
            raise self._commerce_error(exc) from exc
        return json_response(200, record.model_dump(mode="json"))

    def _commerce_payout_apply(self, request, session, params) -> Response:
        try:
            record = self._commerce_payout_changes.apply(
                params["request_id"],
                tenant=session.tenant_id,
                now=request.now or self._clock(),
            )
        except MarketplaceError as exc:
            raise self._commerce_error(exc) from exc
        return json_response(200, record.model_dump(mode="json"))

    def _commerce_job_dispatch(self, request, session, params) -> Response:
        stored = self._commerce_party_order(session, params["order_id"])
        body = request.body or {}
        node_id = str(body.get("node_id") or "")
        if not node_id:
            raise GatewayError(400, "invalid_request", "node_id is required")
        parameters = body.get("parameters") or {}
        if not isinstance(parameters, dict):
            raise GatewayError(
                400, "invalid_request", "parameters must be an object"
            )
        try:
            job = self._commerce_jobs.dispatch(
                stored,
                node_id=node_id,
                parameters={str(k): str(v) for k, v in parameters.items()},
                now=request.now or self._clock(),
            )
        except MarketplaceError as exc:
            raise self._commerce_error(exc) from exc
        return json_response(201, job.model_dump(mode="json"))

    def _commerce_job_ack(self, request, session, params) -> Response:
        body = request.body or {}
        try:
            scheduled = datetime.fromisoformat(str(body.get("scheduled_at") or ""))
        except ValueError:
            raise GatewayError(
                400, "invalid_request", "scheduled_at must be an ISO timestamp"
            ) from None
        try:
            job = self._commerce_jobs.acknowledge(
                params["job_id"],
                tenant=session.tenant_id,
                price_micros=int(body.get("price_micros") or 0),
                scheduled_at=scheduled,
                now=request.now or self._clock(),
            )
        except MarketplaceError as exc:
            raise self._commerce_error(exc) from exc
        except (TypeError, ValueError) as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        return json_response(200, job.model_dump(mode="json"))

    def _commerce_job_complete(self, request, session, params) -> Response:
        try:
            job = self._commerce_jobs.complete(
                params["job_id"],
                tenant=session.tenant_id,
                evidence=str((request.body or {}).get("evidence") or ""),
                now=request.now or self._clock(),
            )
        except MarketplaceError as exc:
            raise self._commerce_error(exc) from exc
        return json_response(200, job.model_dump(mode="json"))

    # ------------------------------------------------------------------ #
    # M4: the open market — peers, imports, and the sourcing sweep.       #
    # ------------------------------------------------------------------ #
    def _commerce_peers_list(self, request, session, params) -> Response:
        return json_response(
            200,
            {
                "items": [
                    p.model_dump(mode="json")
                    for p in self._commerce_federation.peers()
                ]
            },
        )

    def _commerce_peer_register(self, request, session, params) -> Response:
        body = request.body or {}
        peer_id = str(body.get("peer_id") or "")
        if not peer_id:
            raise GatewayError(400, "invalid_request", "peer_id is required")
        peer = self._commerce_federation.register_peer(
            peer_id=peer_id,
            name=str(body.get("name") or peer_id),
            jurisdiction=str(body.get("jurisdiction") or ""),
            base_url=str(body.get("base_url") or ""),
            now=request.now or self._clock(),
        )
        return json_response(201, peer.model_dump(mode="json"))

    def _commerce_peer_state(self, request, session, params) -> Response:
        state = str((request.body or {}).get("state") or "")
        if state not in ("active", "suspended"):
            raise GatewayError(
                400, "invalid_request", "state must be active or suspended"
            )
        try:
            peer = self._commerce_federation.set_peer_state(
                params["peer_id"], state=state
            )
        except MarketplaceError as exc:
            raise self._commerce_error(exc) from exc
        return json_response(200, peer.model_dump(mode="json"))

    def _commerce_peer_import(self, request, session, params) -> Response:
        body = request.body or {}
        try:
            offer = CommerceOffer.model_validate(body.get("offer") or {})
        except ValidationError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        attributes = body.get("attributes") or {}
        if not isinstance(attributes, dict):
            raise GatewayError(
                400, "invalid_request", "attributes must be an object"
            )
        try:
            record = self._commerce_federation.import_offer(
                params["peer_id"],
                offer,
                attributes={str(k): str(v) for k, v in attributes.items()},
                seller_attested=bool(body.get("seller_attested", False)),
                now=request.now or self._clock(),
            )
        except MarketplaceError as exc:
            raise self._commerce_error(exc) from exc
        return json_response(201, record.model_dump(mode="json"))

    def _commerce_announcements(self, request, session, params) -> Response:
        """This host's public shelf, signed for the asking peer.

        The offers are already public via the catalog; the signature only
        adds provenance the peer can verify with the shared secret agreed
        at pairing. No identity configured = this host does not announce."""
        if not self._commerce_peer_identity:
            raise GatewayError(
                404, "not_found", "this host does not announce to peers"
            )
        caller = str(request.query.get("peer_id") or "")
        secret = self._commerce_peer_secrets.get(caller)
        if not secret:
            raise GatewayError(404, "not_found", "unknown peer")
        category = str(request.query.get("category") or "")
        from ..marketplace import sign_offer

        items = []
        now = request.now or self._clock()
        for listing in self._commerce_catalog.store.active():
            if category and listing.category != category:
                continue
            try:
                offer = self._commerce_catalog.offer_for(
                    listing.listing_id, quantity=1, now=now
                )
            except MarketplaceError:
                continue  # an empty shelf announces nothing
            items.append(
                {
                    "offer": sign_offer(
                        offer,
                        peer_id=self._commerce_peer_identity,
                        secret=secret,
                    ).model_dump(mode="json"),
                    "attributes": {"category": listing.category}
                    if listing.category
                    else {},
                    "seller_attested": True,  # our KYC gate published it
                }
            )
        from ..marketplace.protocol import PROTOCOL_VERSION

        return json_response(
            200,
            {
                "protocol": PROTOCOL_VERSION,
                "peer_id": self._commerce_peer_identity,
                "items": items,
            },
        )

    def _commerce_peer_fetch(self, request, session, params) -> Response:
        if self._commerce_peer_transport is None:
            raise GatewayError(
                409, "conflict", "no peer transport is configured on this host"
            )
        from ..marketplace.peerwire import fetch_from_peer

        try:
            imported, rejected = fetch_from_peer(
                self._commerce_federation,
                params["peer_id"],
                transport=self._commerce_peer_transport,
                self_identity=self._commerce_peer_identity,
                now=request.now or self._clock(),
                category=str((request.body or {}).get("category") or ""),
            )
        except MarketplaceError as exc:
            raise self._commerce_error(exc) from exc
        return json_response(
            200,
            {
                "imported": [r.model_dump(mode="json") for r in imported],
                "rejected": rejected,
            },
        )

    def _commerce_source(self, request, session, params) -> Response:
        try:
            specification = RfqSpecification(
                category=str(request.query.get("category") or ""),
                quantity=int(request.query.get("quantity") or 1),
            )
        except (ValidationError, TypeError, ValueError) as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        rows = self._commerce_federation.source(
            specification,
            catalog=self._commerce_catalog,
            now=request.now or self._clock(),
        )
        return json_response(
            200, {"items": [row.model_dump(mode="json") for row in rows]}
        )

    def _commerce_reconciliation_list(self, request, session, params) -> Response:
        reports = self._commerce_reconciliation.exceptions()
        return json_response(
            200, {"items": [r.model_dump(mode="json") for r in reports]}
        )

    def _commerce_reconciliation_sweep(self, request, session, params) -> Response:
        result = self._commerce_reconciliation.sweep(
            now=request.now or self._clock()
        )
        return json_response(200, result)

    def _commerce_orders_list(self, request, session, params) -> Response:
        # The lazy acceptance-timeout sweep rides list traffic, the same
        # way every clock in this codebase rides a request.
        self._commerce_orders.sweep_acceptance_timeouts(
            now=request.now or self._clock()
        )
        orders = self._commerce_orders.orders.list_for(
            tenant=session.tenant_id, principal=session.principal_id
        )
        return json_response(
            200, {"items": [self._commerce_order_view(o) for o in orders]}
        )

    def _commerce_order_place(self, request, session, params) -> Response:
        body = request.body or {}
        intent_id = str(body.get("intent_id") or "")
        if not intent_id:
            raise GatewayError(400, "invalid_request", "intent_id is required")
        current_offer = None
        if body.get("current_offer") is not None:
            try:
                current_offer = CommerceOffer.model_validate(body["current_offer"])
            except ValidationError as exc:
                raise GatewayError(400, "invalid_request", str(exc)) from exc
        pm_ref = str(body.get("payment_method_ref") or "")
        customer_ref = ""
        if self._payments is not None:
            profile = self._payments.profile(session.principal_id)
            customer_ref = profile.customer_ref
            pm_ref = pm_ref or (profile.default_pm or "")
        if not pm_ref or not customer_ref:
            if self._commerce_orders.psp_mode == "live":
                raise GatewayError(
                    402, "payment_required", "no payment method on file"
                )
            # Pre-launch: the fake provider accepts placeholder refs and
            # moves nothing.
            customer_ref = customer_ref or f"cus_local_{session.principal_id}"
            pm_ref = pm_ref or "pm_test_default"
        try:
            order, created = self._commerce_orders.place_order(
                intent_id=intent_id,
                tenant=session.tenant_id,
                now=request.now or self._clock(),
                customer_ref=customer_ref,
                payment_method_ref=pm_ref,
                current_offer=current_offer,
                seller_principal=str(body.get("seller_principal") or ""),
            )
        except MarketplaceError as exc:
            raise self._commerce_error(exc) from exc
        except PaymentError as exc:
            raise GatewayError(402, "payment_declined", str(exc)) from exc
        return json_response(
            201 if created else 200, self._commerce_order_view(order)
        )

    def _commerce_party_order(self, session, order_id: str):
        """The order, only for its parties — a stranger sees nothing."""
        stored = self._commerce_orders.orders.get(
            order_id, tenant=session.tenant_id
        )
        if stored is None or session.principal_id not in (
            stored.record.buyer_principal,
            stored.record.seller_principal,
        ):
            raise GatewayError(404, "not_found", "no such order")
        return stored

    def _commerce_order_get(self, request, session, params) -> Response:
        stored = self._commerce_party_order(session, params["order_id"])
        return json_response(200, self._commerce_order_view(stored))

    def _commerce_order_transition(self, request, session, params, method, **kwargs):
        try:
            stored = method(
                params["order_id"],
                tenant=session.tenant_id,
                actor=session.principal_id,
                now=request.now or self._clock(),
                **kwargs,
            )
        except MarketplaceError as exc:
            raise self._commerce_error(exc) from exc
        except PaymentError as exc:
            raise GatewayError(402, "payment_declined", str(exc)) from exc
        return json_response(200, self._commerce_order_view(stored))

    def _commerce_order_ship(self, request, session, params) -> Response:
        return self._commerce_order_transition(
            request,
            session,
            params,
            self._commerce_orders.mark_shipped,
            tracking=str((request.body or {}).get("tracking") or ""),
        )

    def _commerce_evidence_ref(self, params, body) -> str:
        """The evidence ref a transition records. Content, when supplied,
        lands in the artifact store content-addressed — the ref that rides
        the audit chain is then tamper-evident, not just a claim."""
        content = str(body.get("evidence_content") or "")
        if content:
            if self._commerce_evidence is None:
                raise GatewayError(
                    400,
                    "invalid_request",
                    "evidence storage is not configured on this host; "
                    "pass an evidence reference instead",
                )
            return self._commerce_evidence.put(
                f"order-evidence:{params['order_id']}",
                content.encode("utf-8"),
                media_type="text/plain",
            )
        return str(body.get("evidence") or "")

    def _commerce_order_deliver(self, request, session, params) -> Response:
        body = request.body or {}
        return self._commerce_order_transition(
            request,
            session,
            params,
            self._commerce_orders.mark_delivered,
            evidence=self._commerce_evidence_ref(params, body),
        )

    def _commerce_order_accept(self, request, session, params) -> Response:
        return self._commerce_order_transition(
            request, session, params, self._commerce_orders.accept
        )

    def _commerce_order_cancel(self, request, session, params) -> Response:
        return self._commerce_order_transition(
            request, session, params, self._commerce_orders.cancel
        )

    def _commerce_order_refund(self, request, session, params) -> Response:
        return self._commerce_order_transition(
            request,
            session,
            params,
            self._commerce_orders.refund,
            reason=str((request.body or {}).get("reason") or ""),
        )

    def _commerce_order_ledger(self, request, session, params) -> Response:
        stored = self._commerce_party_order(session, params["order_id"])
        transactions = self._commerce_ledger.transactions(
            order_id=stored.record.order_id
        )
        return json_response(
            200,
            {"items": [txn.model_dump(mode="json") for txn in transactions]},
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

    def _market_library(self, request, session, params) -> Response:
        """The system's routable surface, ENUMERATED — every contract the
        assembler can pick from, with what each consumes and produces,
        plus the slot vocabulary those contracts span. Everything here is
        user-created and semantically named, so no operator can be
        expected to already know it; a route preview that starts from a
        blank input is a memory test, and this endpoint is its answer.
        Read-only, same wall as /v1/market/candidates."""
        assembler, _book = self._require_market()
        items = []
        slots: dict[str, dict] = {}
        for entry in assembler.contracts(""):
            contract = entry.contract
            stats = contract.stats or NodeStats()
            items.append(
                {
                    "name": contract.name,
                    "summary": contract.description,
                    "consumes": [
                        {
                            "name": s.name,
                            "type": s.value_type,
                            "label": s.label,
                            "example": s.example,
                        }
                        for s in contract.consumes
                    ],
                    "produces": [
                        {"name": s.name, "type": s.value_type}
                        for s in contract.produces
                    ],
                    "verified_successes": stats.successes,
                    "verified_failures": stats.failures,
                    "success_mean": stats.success_mean,
                }
            )
            for slot in contract.produces:
                record = slots.setdefault(
                    slot.name,
                    {"name": slot.name, "type": slot.value_type, "producers": []},
                )
                if contract.name not in record["producers"]:
                    record["producers"].append(contract.name)
        return json_response(
            200,
            {
                "items": items,
                "slots": sorted(slots.values(), key=lambda s: s["name"]),
            },
        )

    def _nodes_overview(self, request, session, params) -> Response:
        """Every node in the CALLER's tenant, whoever built it — the
        operator's field of view, behind the same users:manage authority
        that already administers those accounts. Reuses the desk's own
        projection per owner (titles, status, health, the deleted-node
        tombstone filter), so this view can never disagree with what
        each owner sees on their own desk."""
        if self._nodeplace is None or self._desk is None:
            raise GatewayError(404, "not_found", "nodes are not enabled on this host")
        owners = sorted(
            {
                node.noder_principal
                for node in self._nodeplace.all_nodes()
                if node.tenant_id == session.tenant_id
            }
        )
        items: list[dict] = []
        seen: set[str] = set()
        for owner in owners:
            for entry in self._desk.overview(
                principal=owner, tenant=session.tenant_id
            ):
                if entry.node_id in seen:
                    continue
                seen.add(entry.node_id)
                health = entry.health
                items.append(
                    {
                        "node_id": entry.node_id,
                        "title": entry.title,
                        "owner": entry.account.responsible,
                        "status": entry.status,
                        "health_score": health.score,
                        "verified_runs": health.verified_successes
                        + health.verified_failures,
                    }
                )
        items.sort(key=lambda item: (item["owner"], item["title"]))
        return json_response(200, {"items": items})

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
                # The strict value check (B1): user-offered values pass
                # the ONE door every surface shares — unknown keys and
                # type-invalid values refuse in words, with the input's
                # own plain label — before anything binds.
                checked = validate_user_inputs(manifest, user_inputs)
                filled = patch_or_defaults(
                    self._value_patcher, goal=contract.name, manifest=manifest
                )
                patch_cost = filled.cost
                contract = bind_inputs(contract, {**filled.values, **checked})
        except ValueError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        if self._desk is not None:
            # The Supernode owners' SOP binds HERE, where every contract
            # passes on its way to execution: their execution order lands
            # as explicit sop edges the scheduler honors — work passed to
            # the next number, ties in parallel, unordered members free.
            contract = self._stamp_fleet_order(contract)
        children = (
            contract.body.nodes
            if isinstance(contract.body, SubgraphBody)
            else [contract]
        )
        try:
            # UNWIRED first: reserved contracts hold and later execute this
            # exact compile through the approval flow, which carries no
            # tenant stamp and no value pipe — a wired hold would refuse on
            # its own injected references (W0.1). The approval door's
            # wiring is a named follow-up; holds behave exactly as pre-W0.
            compiled = compile_contract(contract)
        except ValueError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc
        reserved = reserved_operations(compiled)
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

        # Unreserved and clear to run: NOW compile wired (W0). The canonical
        # producer key is the desk node id when the child is a registered
        # version — the SAME key the single-node path files under
        # (_file_run_values) — else the child contract id. ONE map feeds
        # both the compile-time injection and the settle-time filing, so
        # the two paths agree by construction.
        desk_ids = (
            self._desk.owning_nodes([c.id for c in children])
            if self._desk is not None
            else {}
        )
        producer_keys = {c.id: desk_ids.get(c.id, c.id) for c in children}
        try:
            compiled = compile_contract(
                contract, wire_dataflow=True, producer_keys=producer_keys
            )
        except ValueError as exc:
            raise GatewayError(400, "invalid_request", str(exc)) from exc

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
        # The binder's tenant wall rides every script action (W0) — stamped
        # at submission like the engine does for single-node runs, never at
        # construction: one runner serves every tenant.
        compiled = stamp_value_tenant(compiled, session.tenant_id)
        # action id -> canonical producer key, for mid-run value filing.
        # Glue actions the contract contributes directly stay unfiled, and
        # so does any child whose NAME another sibling shares (W0.1): the
        # owners map keys by name, so a colliding name would file one
        # child's answer under another's key. (The compiler refuses to
        # wire such producers for the same reason.)
        name_counts: dict[str, int] = {}
        for c in children:
            name_counts[c.name] = name_counts.get(c.name, 0) + 1
        by_name = {c.name: c for c in children if name_counts[c.name] == 1}
        producer_ids = {
            action_id: producer_keys[by_name[owner].id]
            for action_id, owner in compiled.owners.items()
            if owner in by_name
        }
        # Every port a wired binding depends on is an OBLIGATION (W0.1):
        # the producer's action carries it as a declared output port, so a
        # success that omits it DEMOTES — a consumer must never resolve
        # the previous run's value through a port this run skipped.
        from ..values import parse_output_ref

        obligations: dict[str, set[str]] = {}
        for item in compiled.blueprint.actions:
            if item.action.adapter != "script":
                continue
            for bound in (item.action.parameters.get("bindings") or {}).values():
                port = parse_output_ref(bound)
                if port is not None:
                    obligations.setdefault(port[0], set()).add(port[1])
        compiled = stamp_output_obligations(compiled, producer_ids, obligations)

        def value_pipe(action_id: str, outcome) -> None:
            """File one settled child's outputs mid-run — the same shape
            _file_run_values gives a completed single-node run, at the
            same canonical key, so both paths agree and a downstream
            output:// binding resolves to THIS run's answer. A filing
            failure on an OBLIGATED port fails the producer loudly
            (ValuePipeError) — stale data must never pass as fresh."""
            producer = producer_ids.get(action_id)
            if not producer or self._values is None:
                return
            obligated = obligations.get(producer, set())
            evidence = outcome.evidence or {}
            payload = evidence.get("result")
            if payload is None:
                if obligated:
                    raise ValuePipeError(
                        f"producer {producer} emitted no result payload for "
                        f"obligated port(s) {', '.join(sorted(obligated))}"
                    )
                return
            try:
                refs = self._values.snapshot_outputs(
                    session.tenant_id, payload, label=producer, producer=producer
                )
            except Exception as exc:
                if obligated:
                    raise ValuePipeError(
                        f"filing failed for obligated port(s) "
                        f"{', '.join(sorted(obligated))}: {exc}"
                    ) from exc
                raise
            missing = sorted(obligated - set(refs))
            if missing:
                raise ValuePipeError(
                    f"producer {producer} did not fill obligated port(s) "
                    f"{', '.join(missing)}"
                )
            inputs = [
                str(line.get("value_ref"))
                for line in evidence.get("value_provenance") or []
                if line.get("value_ref")
            ]
            if inputs and refs:
                self._values.record_lineage(
                    session.tenant_id, producer, inputs, list(refs.values())
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
                value_pipe=value_pipe if self._values is not None else None,
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

    def _hold_items(self, session) -> list[dict]:
        return [
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

    def _list_contract_holds(self, request, session, params) -> Response:
        """Reserved contracts held for approval — the caller's tenant only."""
        self._sweep_holds(request)
        return json_response(200, {"items": self._hold_items(session)})

    def _inbox_view(self, request, session, params) -> Response:
        """Everything waiting on a human, in ONE feed: contracts held
        for approval, workflows that FAILED, and node builds that
        refused. The triggers are the defined events — the engine's
        ``workflow.failed`` and the build door's ``node.build_failed``,
        both on the hash-chained audit log — and the feed itself is a
        projection of the stores those events came from, so an item
        leaves the inbox the moment its cause resolves (a retry
        succeeds, a rebuild publishes) with no flag anyone must clear.
        Failed workflows list the caller's own by default; stored
        users:manage authority widens them to the tenant and adds the
        build refusals (the build ledger is tenant-keyed, not
        per-person)."""
        self._sweep_holds(request)
        oversee = self._resolver.has_permission(session, "users:manage")
        failed_runs = []
        for s in self._durable.runs.list(limit=10_000):
            if s.contract.metadata.get("tenant_id") != session.tenant_id:
                continue
            if not oversee and s.contract.submitted_by != session.principal_id:
                continue
            awaiting = _PAUSE_VALUE[s.pause.kind] if s.pause else None
            if s.phase.value != "failed" and awaiting != "incident":
                continue
            failed_runs.append(
                {
                    "run_id": s.run_id,
                    "intent": s.intent,
                    "submitted_by": s.contract.submitted_by,
                    "phase": s.phase.value,
                    "awaiting": awaiting,
                    "failure_reason": s.failure_reason,
                    "updated_at": s.updated_at.isoformat(),
                }
            )
        failed_runs.sort(key=lambda r: r["updated_at"], reverse=True)
        ledger = self._build_ledger()
        failed_builds = (
            ledger.open_refusals(session.tenant_id)
            if oversee and ledger is not None
            else []
        )
        return json_response(
            200,
            {
                "holds": self._hold_items(session),
                "failed_runs": failed_runs[:25],
                "failed_builds": failed_builds,
                # B2: functions whose drawer copy is missing — standing
                # until the run-time heal (or any write) closes them.
                "src_issues": self._src_issues(session) if oversee else [],
                "scope": "tenant" if oversee else "mine",
            },
        )

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

        # ---- the conversational-building quality metrics (plan §5) ------ #
        def _standing_questions() -> list[str]:
            """Every question currently standing before a human on a
            clarification pause — the texts the B0 budget is judged on."""
            texts: list[str] = []
            for s in _runs():
                pause = s.pause
                if pause is None or pause.kind.value != "clarification":
                    continue
                for q in (pause.payload or {}).get("questions") or []:
                    text = str((q or {}).get("question") or "")
                    if text:
                        texts.append(text)
            return texts

        def _mechanism_questions_open() -> float:
            from ..plainlanguage import is_mechanism_ask

            return float(
                len([t for t in _standing_questions() if is_mechanism_ask(t)])
            )

        def _value_asks_open() -> float:
            from ..plainlanguage import is_mechanism_ask

            return float(
                len(
                    [
                        t
                        for t in _standing_questions()
                        if not is_mechanism_ask(t)
                    ]
                )
            )

        def _src_divergence() -> float:
            if self._files is None or self._nodeplace is None:
                raise LookupError("no drawers on this host")
            total = missing = 0
            for node in self._nodeplace.all_nodes():
                if node.revoked_at is not None:
                    continue
                version = self._nodeplace.latest_version(node.node_id)
                if version is None:
                    continue
                try:
                    skill = ReusableSkill.model_validate_json(
                        version.sanitized_skill_json
                    )
                except Exception:  # noqa: BLE001
                    continue
                if not self._skill_script(skill):
                    continue
                total += 1
                if not any(
                    f.folder == "src" and f.name == "main.py"
                    for f in self._files.list(
                        tenant=node.tenant_id, node_id=node.node_id
                    )
                ):
                    missing += 1
            if not total:
                raise LookupError("no function nodes yet — no basis")
            return missing / total * 100

        def _runs_with_stored_io() -> float:
            if self._files is None:
                raise LookupError("no drawers on this host")
            done = [
                s
                for s in _ai_terminal_30d()
                if s.phase.value == "completed"
                and isinstance(
                    s.contract.metadata.get("node_function"), dict
                )
                and (s.contract.metadata.get("node_function") or {}).get(
                    "node_id"
                )
            ]
            if not done:
                raise LookupError("no completed node-function runs in 30 days")
            stored = 0
            for s in done:
                node_id = str(s.contract.metadata["node_function"]["node_id"])
                tenant = str(s.contract.metadata.get("tenant_id", ""))
                names = {
                    f.name
                    for f in self._files.list(tenant=tenant, node_id=node_id)
                    if f.folder == f"runs/{s.run_id}"
                }
                if {"inputs.json", "outputs.json"} <= names:
                    stored += 1
            return stored / len(done) * 100

        def _handoff_inspectability() -> float:
            from ..values import parse_output_ref

            graph = self._temporal_graph()
            total = cited = 0
            for s in _ai_terminal_30d():
                if s.phase.value != "completed" or s.execution is None:
                    continue
                function = s.contract.metadata.get("node_function")
                consumer = (
                    str(function["node_id"])
                    if isinstance(function, dict) and function.get("node_id")
                    else None
                )
                edges = (
                    graph.neighbors(consumer, edge_types=("handoff",))
                    if graph is not None and consumer
                    else []
                )
                for outcome in s.execution.action_outcomes:
                    if outcome.status is not ExecutionStatus.SUCCEEDED:
                        continue
                    lines = (outcome.evidence or {}).get("value_provenance")
                    for line in lines or []:
                        source = parse_output_ref(line.get("port_source"))
                        if source is None:
                            continue
                        total += 1
                        if any(
                            e["source_id"] == source[0]
                            and (e.get("attributes") or {}).get("run_id")
                            == s.run_id
                            and (e.get("attributes") or {}).get("port")
                            == source[1]
                            for e in edges
                        ):
                            cited += 1
            if not total:
                raise LookupError("no hand-offs have moved yet")
            return cited / total * 100

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
            # ---- conversational building (plan §5) ------------------ #
            "build.mechanism_questions_open": _mechanism_questions_open,
            "build.value_asks_open": _value_asks_open,
            "build.src_divergence_pct": _src_divergence,
            "build.runs_with_stored_io_pct": _runs_with_stored_io,
            "build.handoff_citation_pct": _handoff_inspectability,
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
        service = self._require_metrics()
        # Every panel view keeps the trend series alive: a collection
        # runs at most once an hour, so no operator has to remember a
        # scheduler for the hour-scale chart to have points on it.
        try:
            service.collect_if_stale(now=request.now or self._clock())
        except Exception:  # noqa: BLE001 - the view outranks the tick
            pass
        return json_response(200, service.view())

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
        scale = request.query.get("scale")
        if scale is not None:
            # The investor scales: hour/day/week/month/year, each bucket
            # closing on its last recorded value — a stock chart's read.
            try:
                points = (
                    int(request.query["points"])
                    if "points" in request.query
                    else None
                )
                series = self._metrics_store.series(scale=scale, points=points)
            except ValueError as exc:
                raise GatewayError(400, "invalid_request", str(exc)) from exc
            return json_response(200, {"series": series, "scale": scale})
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
        """Every earnings row, labeled by source: ad-dividend events key
        on ``ad:<placement>``, everything else is the Nodeplace — one
        ledger, honest provenance per line."""
        billing = self._require_billing()
        items = []
        for entry in billing.entries(session.principal_id):
            item = entry.model_dump(mode="json")
            item["source"] = (
                "ads" if str(entry.event_id).startswith("ad:") else "nodeplace"
            )
            items.append(item)
        return json_response(200, {"items": items})

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
        if self._stripe_webhooks is None:
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
            oolu_order = str(metadata.get("oolu_order_id") or "")
            oolu_tenant = str(metadata.get("oolu_tenant") or "")
            if oolu_order and oolu_tenant:
                # Marketplace orders: the provider event replays into the
                # order machine's own idempotent transitions and postings.
                mapped = {
                    "payment_intent.succeeded": "payment.captured",
                    "payment_intent.amount_capturable_updated": None,  # ack
                    "charge.refunded": "payment.refunded",
                }
                order_kind = mapped.get(event_type, None)
                if order_kind is not None:
                    result["order"] = self._commerce_orders.process_psp_event(
                        {
                            "type": order_kind,
                            "tenant": oolu_tenant,
                            "order_id": oolu_order,
                        },
                        now=request.now or self._clock(),
                    )
                return result
            if event_type in ("charge.refunded", "charge.dispute.created"):
                oolu_event = metadata.get("oolu_event_id")
                if oolu_event and self._disputes is not None:
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
        # P1: the first sign-in seeds the starter shelf, exactly once.
        self._maybe_seed_starters(result.tenant_id, result.principal)
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
        # P1: a phone-born account gets its starter shelf too.
        self._maybe_seed_starters(result.tenant_id, result.principal)
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
        # P1: a freshly verified account gets its starter shelf.
        self._maybe_seed_starters(result.tenant_id, result.principal)
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
            finished = google.finish(state)
        except SignInError as exc:
            raise GatewayError(404, "not_found", str(exc)) from exc
        # P1: a completed Google sign-in seeds the starter shelf.
        if finished.get("status") == "complete":
            self._maybe_seed_starters(
                str(finished.get("tenant") or ""),
                str(finished.get("principal") or ""),
            )
        return json_response(200, finished)

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
        # B3: the node keeps its OWN record — the run's resolved inputs
        # and emitted outputs land in the node's drawer, scrubbed.
        self._land_run_io(state)
        # B4: every value this run moved along an output:// edge lands a
        # run-cited handoff edge on the temporal graph.
        self._land_handoffs(state)
        # P2: the node's emitted book lands back in its drawer, and a
        # reminder the run asked for is filed into the standing store.
        self._land_records(state)
        self._file_reminder(state)
        # P3: emitted sheets and charts land under the drawer's records.
        self._land_emitted_files(state)
        # P4: a rhythm the trigger node parsed becomes a standing
        # schedule — filed by this hand, never by the sandbox.
        self._file_schedule(state)
        # P3 remainder closed: an invoice the deterministic parse could
        # not read is offered to the reading seat, whose checked values
        # come back as bindings on one ordinary re-run.
        self._consult_invoice_reader(state)
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

    def _land_run_io(self, state: RunState) -> None:
        """B3 — the self-contained node: a COMPLETED node-function run
        lands what went in and what came out in the node's OWN drawer,
        under the run's id (``runs/<run_id>/inputs.json`` +
        ``outputs.json``), scrubbed by the corpus discipline. The drawer
        becomes the node's complete story — contract, function, and
        every verified run's io — and the newest outputs project as the
        node's standing result. Verified runs only (the corpus's own
        law), idempotent per run, best-effort: bookkeeping on a
        finished run is never a new way for it to fail."""
        if self._files is None or state.phase is not Phase.COMPLETED:
            return
        function = (state.contract.metadata or {}).get("node_function")
        if not isinstance(function, dict) or not function.get("node_id"):
            return
        execution = state.execution
        if execution is None:
            return
        tenant = str(state.contract.metadata.get("tenant_id", ""))
        node_id = str(function["node_id"])
        folder = f"runs/{state.run_id}"
        try:
            existing = {
                f.name
                for f in self._files.list(tenant=tenant, node_id=node_id)
                if f.folder == folder
            }
            if {"inputs.json", "outputs.json"} <= existing:
                return  # a retry files the same run once
            bindings: dict = {}
            result_payload = None
            for outcome in execution.action_outcomes:
                if outcome.status is not ExecutionStatus.SUCCEEDED:
                    continue
                evidence = outcome.evidence or {}
                if evidence.get("bindings") and not bindings:
                    bindings = dict(evidence["bindings"])
                if evidence.get("result") is not None:
                    result_payload = evidence["result"]
            from ..knowledge.scrubbing import scrub

            stamp = state.updated_at.isoformat()

            def _land(name: str, payload: dict) -> None:
                if name in existing:
                    return
                self._files.save(
                    UserFile(
                        tenant_id=tenant,
                        node_id=node_id,
                        folder=folder,
                        name=name,
                        media_type="application/json",
                        content=scrub(
                            json.dumps(
                                payload, ensure_ascii=False, default=str
                            )
                        ),
                    )
                )

            _land(
                "inputs.json",
                {"run_id": state.run_id, "at": stamp, "bindings": bindings},
            )
            _land(
                "outputs.json",
                {
                    "run_id": state.run_id,
                    "at": stamp,
                    "verified": True,
                    "result": result_payload,
                },
            )
        except Exception:  # noqa: BLE001 - the answer stands either way
            logging.getLogger("oolu.gateway").warning(
                "run io filing failed for run %s", state.run_id, exc_info=True
            )

    def _land_handoffs(self, state: RunState) -> None:
        """B4 — run-cited provenance on the M1 graph: every value a
        COMPLETED run moved along an ``output://`` edge lands one
        ``handoff`` edge, producer → consumer, cited with the run id,
        the port, and the exact value reference — so "which value moved
        along which edge, when" is one time-scoped query answered with
        run ids. Idempotent per (producer, run, port); advisory, never
        fatal."""
        if state.phase is not Phase.COMPLETED:
            return
        function = (state.contract.metadata or {}).get("node_function")
        if not isinstance(function, dict) or not function.get("node_id"):
            return
        execution = state.execution
        if execution is None:
            return
        graph = self._temporal_graph()
        if graph is None:
            return
        consumer = str(function["node_id"])
        try:
            from ..values import parse_output_ref

            seen = {
                (
                    e["source_id"],
                    (e.get("attributes") or {}).get("run_id"),
                    (e.get("attributes") or {}).get("port"),
                )
                for e in graph.neighbors(consumer, edge_types=("handoff",))
            }
            stamp = state.updated_at.isoformat()
            for outcome in execution.action_outcomes:
                if outcome.status is not ExecutionStatus.SUCCEEDED:
                    continue
                evidence = outcome.evidence or {}
                for line in evidence.get("value_provenance") or []:
                    source = parse_output_ref(line.get("port_source"))
                    if source is None:
                        continue
                    producer, port = source
                    if (producer, state.run_id, port) in seen:
                        continue
                    graph.connect(
                        "handoff",
                        producer,
                        consumer,
                        attributes={
                            "port": port,
                            "value_ref": str(line.get("value_ref") or ""),
                            "run_id": state.run_id,
                            "at": stamp,
                        },
                        provenance=(f"run:{state.run_id}",),
                        confidence=1.0,
                    )
                    seen.add((producer, state.run_id, port))
        except Exception:  # noqa: BLE001 - the run's answer stands either way
            logging.getLogger("oolu.gateway").warning(
                "handoff filing failed for run %s", state.run_id, exc_info=True
            )

    def _binding_attachments(self, session, function: dict) -> dict:
        """P3 — the delivered-document stage: for each binding whose
        value names a file in the node's ``messages/`` drawer folder,
        that file's content rides the run (staged by the runner under
        ``attachments/``). Bounded and literal: only exact name
        matches, only the message drawer, at most a handful."""
        if self._files is None:
            return {}
        bindings = function.get("bindings") or {}
        node_id = str(function.get("node_id") or "")
        wanted = {
            str(value)
            for value in bindings.values()
            if isinstance(value, str) and value.strip()
        }
        if not wanted or not node_id:
            return {}
        attachments: dict[str, str] = {}
        try:
            for file in self._files.list(
                tenant=session.tenant_id, node_id=node_id
            ):
                if file.folder != "messages" or file.blob_ref:
                    continue
                if file.name in wanted and len(attachments) < 5:
                    attachments[file.name] = file.content
        except Exception:  # noqa: BLE001 - staging is best-effort
            return {}
        return attachments

    def _land_emitted_files(self, state: RunState) -> None:
        """P3 — a run that emitted ``files`` ({name: content}) lands
        each in the node's drawer under ``records/`` — the cashflow
        chart, the invoice sheet: projections and sheets the function
        rebuilt from its own book, replaced whole, best-effort. Names
        are flattened to basenames; a handful per run, bounded size."""
        if self._files is None:
            return
        result = self._completed_result(state)
        if result is None or not isinstance(result.get("files"), dict):
            return
        function = state.contract.metadata["node_function"]
        tenant = str(state.contract.metadata.get("tenant_id", ""))
        node_id = str(function["node_id"])
        try:
            existing = {
                f.name: f
                for f in self._files.list(tenant=tenant, node_id=node_id)
                if f.folder == "records"
            }
            for raw_name, content in list(result["files"].items())[:5]:
                name = str(raw_name).replace("/", "_").replace("\\", "_")
                content = str(content)
                if not name or len(content) > 1_000_000:
                    continue
                current = existing.get(name)
                if current is not None:
                    if current.content != content:
                        self._files.save(
                            current.model_copy(update={"content": content})
                        )
                    continue
                media = (
                    "text/html"
                    if name.endswith(".html")
                    else "text/csv"
                    if name.endswith(".csv")
                    else "text/plain"
                )
                self._files.save(
                    UserFile(
                        tenant_id=tenant,
                        node_id=node_id,
                        folder="records",
                        name=name,
                        media_type=media,
                        content=content,
                    )
                )
        except Exception:  # noqa: BLE001 - bookkeeping never fails a run
            logging.getLogger("oolu.gateway").warning(
                "emitted-file landing failed for run %s",
                state.run_id,
                exc_info=True,
            )

    @staticmethod
    def _completed_result(state: RunState) -> dict | None:
        """The dict payload a COMPLETED node-function run emitted, or
        None — the one reader the P2 completion hooks share."""
        if state.phase is not Phase.COMPLETED or state.execution is None:
            return None
        function = (state.contract.metadata or {}).get("node_function")
        if not isinstance(function, dict) or not function.get("node_id"):
            return None
        for outcome in state.execution.action_outcomes:
            if outcome.status is not ExecutionStatus.SUCCEEDED:
                continue
            result = (outcome.evidence or {}).get("result")
            if isinstance(result, dict):
                return result
        return None

    def _land_records(self, state: RunState) -> None:
        """P2 — the record discipline's write-back: a run that emitted
        ``records`` (the node's updated book, a list) lands it in the
        drawer as ``records/rows.json`` — the file the next run stages
        as ``./records.json``. The drawer IS the personal content:
        written verbatim (it is the user's own book, not a shared
        corpus), replaced whole, best-effort."""
        if self._files is None:
            return
        result = self._completed_result(state)
        if result is None or not isinstance(result.get("records"), list):
            return
        function = state.contract.metadata["node_function"]
        tenant = str(state.contract.metadata.get("tenant_id", ""))
        node_id = str(function["node_id"])
        content = json.dumps(
            result["records"], ensure_ascii=False, default=str
        )
        if len(content) > 1_000_000:
            logging.getLogger("oolu.gateway").warning(
                "records write skipped for %s: the book outgrew the "
                "drawer write",
                node_id,
            )
            return
        try:
            current = next(
                (
                    f
                    for f in self._files.list(tenant=tenant, node_id=node_id)
                    if f.folder == "records" and f.name == "rows.json"
                ),
                None,
            )
            if current is not None:
                if current.content != content:
                    self._files.save(
                        current.model_copy(update={"content": content})
                    )
            else:
                self._files.save(
                    UserFile(
                        tenant_id=tenant,
                        node_id=node_id,
                        folder="records",
                        name="rows.json",
                        media_type="application/json",
                        content=content,
                    )
                )
        except Exception:  # noqa: BLE001 - bookkeeping never fails a run
            logging.getLogger("oolu.gateway").warning(
                "records write failed for run %s", state.run_id, exc_info=True
            )

    def _file_reminder(self, state: RunState) -> None:
        """P2 — the reminders node's hand: a run that emitted a
        ``reminder`` ({text, day, time}) has it filed into the standing
        ``ReminderStore`` AS THE OWNER — the sandbox never touches the
        host's stores; the emitted result is the ask, this hook is the
        hand. Filed once (the store's own delivery guarantees stand);
        a past time or a host without reminders is audited, never an
        error."""
        if self._reminders is None:
            return
        result = self._completed_result(state)
        if result is None:
            return
        ask = result.get("reminder")
        if not (isinstance(ask, dict) and str(ask.get("text") or "").strip()):
            return
        tenant = str(state.contract.metadata.get("tenant_id", ""))
        principal = str(state.contract.submitted_by or "")
        already = any(
            e.event_type == "reminder.filed"
            and e.payload.get("run_id") == state.run_id
            for e in self._durable.audit.records()
        )
        if already:
            return  # one run files its reminder exactly once
        try:
            due = datetime.fromisoformat(
                f"{ask.get('day')}T{ask.get('time') or '09:00'}"
            ).replace(tzinfo=UTC)
            row = self._reminders.add(
                tenant=tenant,
                principal=principal,
                text=str(ask["text"]),
                due_at=due,
            )
        except (TypeError, ValueError) as exc:
            self._durable.audit.append(
                "reminder.not_filed",
                {
                    "run_id": state.run_id,
                    "tenant": tenant,
                    "principal": principal,
                    "reason": str(exc),
                },
            )
            return
        self._durable.audit.append(
            "reminder.filed",
            {
                "run_id": state.run_id,
                "reminder_id": row.reminder_id,
                "tenant": tenant,
                "principal": principal,
                "due_at": row.due_at.isoformat(),
            },
        )

    def _file_schedule(self, state: RunState) -> None:
        """P4 — the trigger node's hand: a run that emitted a parsed
        ``schedule`` ({cadence, at_minute, goal, …}) has it filed into
        the standing ``PulseStore`` AS THE OWNER — the sandbox parses
        the words, this hook keeps the rhythm. Once per run, audited
        as ``pulse.created`` with the run named; an unusable spec is
        audited, never an error."""
        result = self._completed_result(state)
        if result is None:
            return
        ask = result.get("schedule")
        if not (isinstance(ask, dict) and str(ask.get("goal") or "").strip()):
            return
        already = any(
            e.event_type == "pulse.created"
            and e.payload.get("run_id") == state.run_id
            for e in self._durable.audit.records()
        )
        if already:
            return  # one run keeps its rhythm exactly once
        tenant = str(state.contract.metadata.get("tenant_id", ""))
        principal = str(state.contract.submitted_by or "")
        try:
            schedule = self._pulse.add(
                tenant,
                principal,
                cadence=str(ask.get("cadence") or ""),
                at_minute=int(ask.get("at_minute") or 0),
                goal=str(ask.get("goal") or ""),
                weekday=ask.get("weekday"),
                day_of_month=ask.get("day_of_month"),
                month=ask.get("month"),
                day=ask.get("day"),
                label=str(ask.get("words") or "")[:200],
            )
        except (TypeError, ValueError) as exc:
            self._durable.audit.append(
                "pulse.ask_failed",
                {
                    "run_id": state.run_id,
                    "tenant": tenant,
                    "principal": principal,
                    "reason": str(exc),
                },
            )
            return
        self._durable.audit.append(
            "pulse.created",
            {
                "schedule_id": schedule.schedule_id,
                "tenant": tenant,
                "principal": principal,
                "goal": schedule.goal,
                "cadence": schedule.cadence,
                "run_id": state.run_id,
            },
        )

    _INVOICE_READING_PROMPT = (
        "You are the reading seat for scanned invoices. From the invoice "
        "text below, extract EXACTLY these fields and reply with ONE "
        "JSON object and nothing else:\n"
        '  {"vendor": "<the issuing business>", '
        '"date": "<YYYY-MM-DD or empty>", '
        '"total": "<the invoice total as digits>"}\n'
        "Rules: read, never guess — if you cannot find a definite total, "
        "reply with the single word UNREADABLE instead of JSON. Never "
        "invent a number that is not in the text."
    )

    def _consult_invoice_reader(self, state: RunState) -> None:
        """P3's model door: a COMPLETED run that emitted ``needs_reading``
        (the deterministic parse found no total) is offered to the
        reading seat — the tenant's author model, seated and metered.
        The seat's extraction is STRICTLY CHECKED (a total that does not
        parse as a number is refused — never a guessed number) and comes
        back as bindings on ONE ordinary re-run of the same function, so
        the row still lands through a run and every record law (B3 io,
        the sheet, the entry hand-off) holds. No model configured: the
        run's worded refusal stands, audited. Once per run."""
        result = self._completed_result(state)
        if result is None:
            return
        ask = result.get("needs_reading")
        if not (isinstance(ask, dict) and str(ask.get("file") or "").strip()):
            return
        already = any(
            e.event_type in ("invoice.read_by_seat", "invoice.reading_failed")
            and e.payload.get("run_id") == state.run_id
            for e in self._durable.audit.records()
        )
        if already:
            return
        tenant = str(state.contract.metadata.get("tenant_id", ""))
        principal = str(state.contract.submitted_by or "")
        function = state.contract.metadata["node_function"]
        name = str(ask["file"])

        def _refuse(reason: str) -> None:
            self._durable.audit.append(
                "invoice.reading_failed",
                {
                    "run_id": state.run_id,
                    "file": name,
                    "tenant": tenant,
                    "principal": principal,
                    "reason": reason,
                },
            )

        author = None
        try:
            author = self._node_function_author(tenant)
        except Exception:  # noqa: BLE001 - a broken keyring reads as none
            author = None
        if author is None:
            _refuse(
                "no model is configured to read invoices — the worded "
                "refusal stands"
            )
            return
        text = ""
        if self._files is not None:
            text = next(
                (
                    f.content
                    for f in self._files.list(
                        tenant=tenant, node_id=str(function["node_id"])
                    )
                    if f.folder == "messages" and f.name == name
                ),
                "",
            )
        if not text:
            _refuse("the named file is no longer in the drawer")
            return
        seat = self._seat_actor(author, principal)
        try:
            raw = seat.reply(
                [
                    {
                        "role": "system",
                        "content": self._INVOICE_READING_PROMPT,
                    },
                    {"role": "user", "content": text[:6000]},
                ]
            )
        except Exception as exc:  # noqa: BLE001 - the seat may be down
            _refuse(f"the reading seat did not answer: {exc}")
            return
        if "UNREADABLE" in str(raw):
            _refuse("the reading seat found no definite total — nothing "
                    "was guessed")
            return
        from ..orchestrator.proposals import _json_candidates

        extracted = None
        for blob in _json_candidates(str(raw)):
            try:
                candidate = json.loads(blob)
            except (TypeError, ValueError):
                continue
            if isinstance(candidate, dict) and candidate.get("total"):
                extracted = candidate
                break
        if extracted is None:
            _refuse("the reading seat's answer was not a checked "
                    "extraction — nothing was guessed")
            return
        # The strict value check, before anything binds: a total that
        # does not parse as a positive number is refused in words.
        try:
            total = float(
                str(extracted["total"]).replace(",", "").replace("$", "")
            )
        except (TypeError, ValueError):
            total = 0.0
        if not total > 0:
            _refuse("the seat's total did not check as a number — "
                    "nothing was guessed")
            return
        day = str(extracted.get("date") or "").strip()
        if not re.match(r"^20\d\d-\d\d-\d\d$", day):
            day = ""
        vendor = str(extracted.get("vendor") or "").strip()[:60]
        from types import SimpleNamespace

        session = SimpleNamespace(tenant_id=tenant, principal_id=principal)
        try:
            reread = self._start_intent_run(
                session,
                str(function.get("goal") or state.contract.intent),
                extra_bindings={
                    "invoice_file": name,
                    "extracted_total": round(total, 2),
                    "extracted_vendor": vendor,
                    "extracted_date": day,
                },
            )
        except GatewayError as exc:
            _refuse(f"the re-run was refused: {exc.message}")
            return
        self._durable.audit.append(
            "invoice.read_by_seat",
            {
                "run_id": state.run_id,
                "reread_run_id": str(reread["run_id"]),
                "file": name,
                "total": round(total, 2),
                "tenant": tenant,
                "principal": principal,
            },
        )

    def _node_last_result(self, tenant: str, node_id: str) -> dict | None:
        """The node's standing result — a PROJECTION over the drawer's
        newest ``runs/*/outputs.json`` (M1's law: derived on every read,
        never stored as truth). None when the node has not verified a
        run yet, or keeps no drawer here."""
        if self._files is None:
            return None
        newest = None
        for file in self._files.list(tenant=tenant, node_id=node_id):
            if file.name != "outputs.json" or not file.folder.startswith("runs/"):
                continue
            if newest is None or file.created_at > newest.created_at:
                newest = file
        if newest is None:
            return None
        try:
            return json.loads(newest.content)
        except ValueError:
            return None

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
            "submitted_by": state.contract.submitted_by,
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
