from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import Settings
from .durable.audit import DurableAuditLog
from .durable.connection import DurableConnection
from .durable.service import DurableWorkflowService, OrchestratorFactory
from .identity.service import IdentityApprovalAuthority
from .orchestrator import (
    ActionExecutorRouteRunner,
    BoundedRetryRecovery,
    CapabilityGrounder,
    CollectingFeedbackSink,
    LeastCostRouteOptimizer,
    ModelBackedIntaker,
    RegistryGrounder,
    RiskBasedHumanControl,
    SkillRegistryPlanner,
    StatusOutcomeMonitor,
    WorkflowOrchestrator,
)
from .orchestrator.intake import IntakeModel
from .orchestrator.state import Blueprint, RoutePlan, SemanticEdge, SemanticGrounding
from .skills.models import ReusableSkill
from .skills.ports import ActionExecutor
from .skills.requirements import RequirementBrief

if TYPE_CHECKING:
    from .skills.registry import SkillRegistry

_NO_ROUTE_REASON = "no executable route is configured for this deployment yet"


class PassthroughGrounder:
    def ground(self, brief: RequirementBrief) -> SemanticGrounding:
        terms = [param.name for param in brief.parameters]
        return SemanticGrounding(
            edges=[SemanticEdge(source=term, target=term) for term in terms],
            resolved_capabilities=frozenset(terms),
            unresolved_terms=[],
        )


class PlanningOnlyOptimizer:
    def optimize(
        self, brief: RequirementBrief, grounding: SemanticGrounding
    ) -> RoutePlan:
        return RoutePlan(
            chosen=Blueprint(
                name="unconfigured",
                excluded=True,
                exclusion_reason=_NO_ROUTE_REASON,
            ),
            alternatives=[],
        )


def build_cli_executor(
    *,
    workspace: str | Path,
    allowed_executables: list[str],
    timeout_s: float = 30.0,
) -> dict[str, ActionExecutor]:
    from .skills.cli_adapter import CliActionExecutor, CliExecutionPolicy

    policy = CliExecutionPolicy.create(
        workspace=workspace,
        allowed_executables=allowed_executables,
        timeout_s=timeout_s,
    )
    executor = CliActionExecutor(policy)
    return {executor.name: executor}


def build_commerce_executors(
    *,
    amazon_client: Any = None,
    site_driver: Any = None,
    is_authorized: Callable[[str], bool] | None = None,
    orders_enabled: Callable[[], bool] | None = None,
    resolve_authorization: Callable[[Any], str | None] | None = None,
) -> dict[str, ActionExecutor]:
    """The order-placing hands: a general site driver and per-site adapters.

    Each is registered only when its real driver is provided — the general
    ``web`` executor needs a browser (Playwright, the ``browser`` extra),
    the ``amazon`` executor needs an Amazon client. The route optimizer
    then scores whichever roads are actually drivable here and picks the
    cheapest.

    Two money gates ride on every order, both defensive:
    ``is_authorized`` ties each order to the user's payment-consent + 2FA
    release; ``orders_enabled`` is the operator's master switch above it, so
    that even an authorized order does not go through until the deployment
    has turned autonomous ordering on. Omit ``orders_enabled`` for no
    operator gate (the historical behaviour).
    """
    from .skills.commerce import AmazonExecutor, SiteDriverExecutor

    executors: dict[str, ActionExecutor] = {}
    if site_driver is not None:
        web = SiteDriverExecutor(
            site_driver,
            is_authorized=is_authorized,
            orders_enabled=orders_enabled,
            resolve_authorization=resolve_authorization,
        )
        executors[web.name] = web
    if amazon_client is not None:
        amazon = AmazonExecutor(
            amazon_client,
            is_authorized=is_authorized,
            orders_enabled=orders_enabled,
            resolve_authorization=resolve_authorization,
        )
        executors[amazon.name] = amazon
    return executors


def build_http_executor(
    *,
    allow_hosts: list[str] | tuple[str, ...] = (),
    timeout_s: float = 20.0,
    max_bytes: int = 1_000_000,
    allow_private: bool = False,
) -> dict[str, ActionExecutor]:
    """The engine's first hands: GET-only HTTP behind the SSRF guard.

    ``allow_hosts`` narrows to named hosts (and their subdomains);
    ``allow_private`` disables the public-address guard for offline
    tests/demos only — never ship it on.
    """
    from .skills.http_adapter import HttpActionExecutor, HttpExecutionPolicy

    executor = HttpActionExecutor(
        HttpExecutionPolicy(
            allow_hosts=frozenset(h.strip().lower() for h in allow_hosts if h.strip()),
            timeout_s=timeout_s,
            max_bytes=max_bytes,
            allow_private=allow_private,
        )
    )
    return {executor.name: executor}


def build_discovered_cli_executor(
    *,
    workspace: str | Path,
    extra_allow: list[str] | None = None,
    timeout_s: float = 30.0,
) -> dict[str, ActionExecutor]:
    from .skills.discovery import discover_tools

    allowed = [tool.path for tool in discover_tools()] + list(extra_allow or [])
    return build_cli_executor(
        workspace=workspace, allowed_executables=allowed, timeout_s=timeout_s
    )


def build_worker_executor(
    worker_executor: Any,
    *,
    secret: str = "local-worker-secret",
    worker_id: str = "worker-1",
    capabilities: frozenset[str] = frozenset({"run"}),
    tenant_id: str = "local",
    trust_level: str = "untrusted_synthesized",
    timeout_seconds: float = 30.0,
) -> dict[str, ActionExecutor]:
    from .skills.remote import InProcessWorkerTransport, RemoteWorkerActionExecutor
    from .worker.control_plane import ControlPlane, WorkerInfo
    from .worker.leases import LeaseSigner, LeaseVerifier, TrustLevel
    from .worker.ledger import InMemoryLeaseLedger
    from .worker.worker import Worker

    ledger = InMemoryLeaseLedger()
    signer = LeaseSigner(secret)
    control_plane = ControlPlane(signer, ledger=ledger)
    control_plane.register_worker(
        WorkerInfo(
            worker_id=worker_id,
            capabilities=capabilities,
            backend_kind=worker_executor.backend_kind,
        )
    )
    verifier = LeaseVerifier(secret, audience=worker_id, ledger=ledger)
    worker = Worker(worker_id, verifier, worker_executor)
    transport = InProcessWorkerTransport({worker_id: worker})
    executor = RemoteWorkerActionExecutor(
        control_plane,
        transport,
        tenant_id=tenant_id,
        trust_level=TrustLevel(trust_level),
        capabilities=capabilities,
        timeout_seconds=timeout_seconds,
    )
    return {executor.name: executor}


def build_docker_worker_executor(
    settings: Settings | None = None,
    *,
    image: str | None = None,
    backend_kind: str = "docker",
) -> Any:
    """The Docker-sandboxed WorkerExecutor a worker host owns. Requires the docker
    SDK and a reachable daemon (raises BackendUnavailable otherwise)."""
    settings = settings or Settings()
    from .runtime.isolation import LocalDockerBackend
    from .worker.execution import BackendWorkerExecutor

    backend = LocalDockerBackend(
        image=image or settings.backend.image,
        network_name=settings.backend.network_name,
        uv_cache_dir=settings.backend.uv_cache_dir,
        default_index_url=settings.backend.pinned_index_url,
        run_as_user=settings.backend.run_as_user,
    )
    return BackendWorkerExecutor(backend, backend_kind=backend_kind)


def build_remote_worker_executor(
    *,
    http: Any,
    worker_urls: dict[str, str],
    secret: str = "local-worker-secret",
    capabilities: frozenset[str] = frozenset({"run"}),
    backend_kind: str = "docker",
    tenant_id: str = "local",
    trust_level: str = "untrusted_synthesized",
    timeout_seconds: float = 30.0,
) -> dict[str, ActionExecutor]:
    from .skills.remote import RemoteWorkerActionExecutor
    from .worker.control_plane import ControlPlane, WorkerInfo
    from .worker.http import HttpWorkerTransport
    from .worker.leases import LeaseSigner, TrustLevel
    from .worker.ledger import InMemoryLeaseLedger

    control_plane = ControlPlane(LeaseSigner(secret), ledger=InMemoryLeaseLedger())
    for worker_id in worker_urls:
        control_plane.register_worker(
            WorkerInfo(
                worker_id=worker_id,
                capabilities=capabilities,
                backend_kind=backend_kind,
            )
        )
    transport = HttpWorkerTransport(
        http, worker_urls=worker_urls, timeout=timeout_seconds
    )
    executor = RemoteWorkerActionExecutor(
        control_plane,
        transport,
        tenant_id=tenant_id,
        trust_level=TrustLevel(trust_level),
        capabilities=capabilities,
        timeout_seconds=timeout_seconds,
    )
    return {executor.name: executor}


def build_browser_executor(
    *,
    headless: bool = True,
    allow_hosts: list[str] | None = None,
    executable_path: str | None = None,
) -> dict[str, ActionExecutor]:
    from .skills.browser import BrowserActionExecutor, BrowserPolicy

    executor = BrowserActionExecutor(
        policy=BrowserPolicy(
            headless=headless,
            allow_hosts=frozenset(allow_hosts or []),
            executable_path=executable_path,
        )
    )
    return {executor.name: executor}


def build_planning_context(
    settings: Settings | None = None,
    *,
    registry: "SkillRegistry | None" = None,
    tools: list[Any] | None = None,
    discover: bool = False,
) -> Callable[[str], str] | None:
    settings = settings or Settings()
    resolved_tools = list(tools or [])
    if discover and not resolved_tools:
        from .skills.discovery import discover_tools

        resolved_tools = discover_tools()
    if registry is None and not resolved_tools:
        return None
    from .skills.context import PlanningContextBuilder

    return PlanningContextBuilder(
        registry,
        tools=resolved_tools,
        max_skills=settings.skills.max_context_tools,
        max_tools=settings.skills.max_context_tools,
    ).manifest


def build_intake_model(
    settings: Settings | None = None,
    *,
    registry: "SkillRegistry | None" = None,
    tools: list[Any] | None = None,
    discover: bool = False,
) -> IntakeModel:
    settings = settings or Settings()
    from .orchestrator.intake import LiteLLMIntakeModel

    context_provider = build_planning_context(
        settings, registry=registry, tools=tools, discover=discover
    )
    return LiteLLMIntakeModel(
        settings.routing.fast.model,
        timeout=settings.request_timeout_s,
        context_provider=context_provider,
    )


def blob_store_from_env(data_dir: str | Path, env: dict | None = None):
    """Where the blob layer lives: Cloudflare R2 / S3 when the bucket is
    named in the environment, the local filesystem otherwise. One
    selector for every artifact site (the file drawer's blobs and the
    CAD hand's exports), so a hosted install moves its bytes to object
    storage with four variables and no code:

        OOLU_BLOB_S3_BUCKET             the bucket name (turns S3 on)
        OOLU_BLOB_S3_ENDPOINT           https://<account>.r2.cloudflarestorage.com
        OOLU_BLOB_S3_ACCESS_KEY_ID      the R2/S3 access key
        OOLU_BLOB_S3_SECRET_ACCESS_KEY  its secret
        OOLU_BLOB_S3_PREFIX             optional key prefix (multi-install)
        OOLU_BLOB_S3_REGION             optional (default "auto", right for R2)
    """
    import os as _os

    from .durable.artifacts import FilesystemArtifactStore

    env = env if env is not None else dict(_os.environ)
    bucket = env.get("OOLU_BLOB_S3_BUCKET", "").strip()
    if not bucket:
        return FilesystemArtifactStore(Path(data_dir) / "file-blobs")
    from .durable.artifacts_s3 import S3ArtifactStore

    return S3ArtifactStore(
        bucket=bucket,
        endpoint_url=env.get("OOLU_BLOB_S3_ENDPOINT", "").strip(),
        access_key_id=env.get("OOLU_BLOB_S3_ACCESS_KEY_ID", "").strip(),
        secret_access_key=env.get("OOLU_BLOB_S3_SECRET_ACCESS_KEY", "").strip(),
        region=env.get("OOLU_BLOB_S3_REGION", "auto").strip() or "auto",
        prefix=env.get("OOLU_BLOB_S3_PREFIX", "").strip(),
    )


def build_desktop_hands(
    *,
    data_dir: str | Path,
    environ: dict | None = None,
) -> dict[str, ActionExecutor]:
    """Every hand `wfgps desktop` gives the engine on THIS machine.

    - HTTP (GET-only, SSRF-guarded) — always on.
    - CLI: the discovered local tools (ffmpeg, pandoc, …), workspace-
      confined under the data directory — on by default because commanding
      the local device is what the desktop engine is FOR; disable with
      OOLU_CLI_TOOLS=off, widen with OOLU_CLI_ALLOWLIST (comma-separated
      executable paths).
    (The script hand is added by ``build_host_runtime``.)
    """
    import os as _os

    env = environ if environ is not None else _os.environ
    hands = build_http_executor(
        allow_hosts=tuple(
            h for h in env.get("OOLU_HTTP_ALLOWLIST", "").split(",") if h
        ),
        allow_private=env.get("OOLU_HTTP_ALLOW_PRIVATE") == "1",
    )
    if env.get("OOLU_CLI_TOOLS", "").strip().lower() not in {"off", "0", "false"}:
        workspace = Path(data_dir) / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        hands.update(
            build_discovered_cli_executor(
                workspace=workspace,
                extra_allow=[
                    p
                    for p in env.get("OOLU_CLI_ALLOWLIST", "").split(",")
                    if p.strip()
                ],
            )
        )
    if env.get("OOLU_CAD", "").strip().lower() not in {"off", "0", "false"}:
        # The engineering hand: on wherever its geometry kernel is
        # installed (the 'cad' extra), silent where it isn't. Exports
        # land in the SAME content-addressed store the file drawer's
        # blobs live in, so surfacing a part later is a row, not a copy.
        try:
            import cadquery  # noqa: F401, PLC0415 - availability probe

            from .skills.cad_adapter import CadActionExecutor

            executor = CadActionExecutor(
                artifacts=blob_store_from_env(data_dir, env)
            )
            hands[executor.name] = executor
        except ImportError:
            pass
    return hands


def build_script_executor(
    settings: Settings | None = None,
    *,
    cache_path: str | Path = ":memory:",
    synthesizer=None,  # runtime.ScriptSynthesizer, optional
) -> dict[str, ActionExecutor]:
    """The script hand: run planner-provided or synthesized code through the
    configured isolation backend (Docker when configured; the subprocess
    dev fallback otherwise), memoized per node and always verified by
    execution before anything is trusted."""
    settings = settings or Settings()
    from .cache.store import LocalScriptCache
    from .config import _build_backend
    from .runtime.script_node import NodeScriptRunner

    runner = NodeScriptRunner(
        _build_backend(settings.backend),
        LocalScriptCache(cache_path),
        synthesizer=synthesizer,
        pinned_index_url=settings.backend.pinned_index_url,
        backend_kind=settings.backend.kind,
        backend_image=(
            settings.backend.image if settings.backend.kind == "docker" else None
        ),
    )
    return {runner.name: runner}


def build_orchestrator_factory(
    settings: Settings | None = None,
    *,
    intake_model: IntakeModel | None = None,
    skills: list[ReusableSkill] | None = None,
    blueprints: list[Blueprint] | None = None,
    grounding_map: dict[str, str] | None = None,
    executors: dict[str, ActionExecutor] | None = None,
    route_model=None,  # chat.ChatModel: semantic route choice, optional
    rebuilder=None,  # orchestrator.RouteRebuilder: the post-retry LLM rebuild
    # Turn a shopping ask into commerce routes at plan time: wraps whatever
    # optimizer the branches below choose with a CommerceRouteOptimizer that
    # parses a purchase brief and builds the order roads. Off by default;
    # non-purchase briefs pass straight through unchanged.
    commerce_planning: bool = False,
) -> OrchestratorFactory:
    from .orchestrator.adapters import CommerceRouteOptimizer, ModelRouteOptimizer

    intaker = ModelBackedIntaker(intake_model)
    executor = ActionExecutorRouteRunner(dict(executors or {}))

    if skills and not blueprints:
        planner = SkillRegistryPlanner(skills)
        grounder: object = RegistryGrounder(planner.capabilities())
        optimizer: object = ModelRouteOptimizer(
            LeastCostRouteOptimizer(planner.blueprints()), model=route_model
        )
    elif blueprints:
        grounder = CapabilityGrounder(
            dict(grounding_map or {}), always_resolved=executor.capabilities()
        )
        optimizer = LeastCostRouteOptimizer(list(blueprints))
    else:
        grounder = PassthroughGrounder()
        optimizer = PlanningOnlyOptimizer()

    if commerce_planning:
        # The commerce optimizer self-grounds its routes against the installed
        # executors' capabilities, so it works over any base grounder above.
        optimizer = CommerceRouteOptimizer(
            optimizer, capabilities=executor.capabilities()
        )

    def factory(audit: DurableAuditLog) -> WorkflowOrchestrator:
        return WorkflowOrchestrator(
            intaker=intaker,
            grounder=grounder,  # type: ignore[arg-type]
            optimizer=optimizer,  # type: ignore[arg-type]
            human_control=RiskBasedHumanControl(),
            executor=executor,
            monitor=StatusOutcomeMonitor(),
            recovery=BoundedRetryRecovery(),
            feedback=CollectingFeedbackSink(),
            events=audit,  # type: ignore[arg-type]
            rebuilder=rebuilder,
        )

    return factory


LOCAL_ISSUER = "oolu-local"
LOCAL_AUDIENCE = "oolu"


@dataclass
class HostRuntime:
    """Everything `oolu host` serves, over one data directory."""

    gateway: Any  # gateway.GatewayApp
    asgi: Any  # gateway.GatewayASGI — what uvicorn runs
    accounts: Any  # identity.LocalAccountService
    identity: Any  # identity.IdentityStore
    conn: DurableConnection
    _closers: tuple[Any, ...] = ()

    def close(self) -> None:
        for closer in self._closers:
            closer.close()
        self.conn.close()


def build_host_runtime(
    settings: Settings | None = None,
    *,
    data_dir: str | Path,
    secret: str,
    token_ttl_seconds: int = 8 * 3600,
    intake_model: IntakeModel | None = None,
    skills: list[ReusableSkill] | None = None,
    blueprints: list[Blueprint] | None = None,
    grounding_map: dict[str, str] | None = None,
    executors: dict[str, ActionExecutor] | None = None,
    config: Any = None,  # gateway.GatewayConfig
    database_url: str | None = None,
    frontend: str = "host",
    # True on a public multi-user host serving the shell: the shell is
    # told it faces a remote server (sign-in required, same-origin auth)
    # instead of assuming the loopback desktop. `oolu host` sets this;
    # `oolu desktop` never does.
    shell_remote: bool = False,
    # Hostnames whose requests get the operator's admin page instead of
    # ``frontend`` — how one public deployment serves the product shell at
    # app.example.com and the admin console at admin.example.com.
    admin_hosts: tuple[str, ...] = (),
    google_client_id: str | None = None,
    google_client_secret: str = "",
    google_default_tenant: str = "main",
    seed_handiwork_for: str | None = None,
    mail=None,  # mail.MailSender: e-mail verification + password reset
    sms=None,  # sms.SmsSender: "continue with phone" codes + passwords
    # A PUBLIC host must never run synthesized code unsandboxed: with
    # require_isolation the script hand is wired only when the backend is
    # real isolation (docker), never the subprocess dev fallback.
    require_isolation: bool = False,
    # The hosted plan's brain: operator keys per provider (e.g.
    # {"anthropic": "sk-ant-..."}). Stored encrypted in the keyring under
    # the reserved platform tenant; tenants on model.source="subscription"
    # are served through them inside their plan's monthly allowance.
    platform_model_keys: dict[str, str] | None = None,
    # The launch guard's first gate: the deliberate operator switch that
    # lets real cards be charged (prices and verification still gate per
    # class of work). Off is the pre-launch default.
    transactions_enabled: bool = False,
    # Real money adapters: with a Stripe secret key the card vault and the
    # payout adapter talk to Stripe; without one the test doubles stay in
    # place. The webhook secret (whsec_...) opens /v1/webhooks/stripe.
    stripe_secret_key: str | None = None,
    stripe_webhook_secret: str | None = None,
    # The desktop's own disk for the chat's local file search. ONLY the
    # loopback desktop sets this; a public host must never touch it.
    local_files_root: str | Path | None = None,
    # The general "buy this on any site" road: a browser-backed SiteDriver
    # (skills/site_driver.py — a persistent, headed profile with a
    # human-control login/2FA pause). When provided, the ``web`` commerce
    # executor is registered and tied to the payment-consent + 2FA gate below.
    # None (the default) leaves ordering unwired, exactly as before — a
    # server host has no display to sign a storefront in, so only the desktop
    # shell passes this. Enabling a driver does NOT open the money port; the
    # LaunchGuard (``transactions_enabled``) and the checkout authorization
    # are unchanged.
    site_driver: Any = None,
    amazon_client: Any = None,
    # The operator's master switch for autonomous order placement, above the
    # per-order consent + 2FA gate. Off (the default) means the order-placing
    # hands are wired and can browse, but the money step of any order is
    # BLOCKED until the operator turns real ordering on for this deployment —
    # the "explicit opt-in before anything spends" the checkout road needs.
    # This spends the USER's money at a retailer through their released
    # authorization; it is independent of ``transactions_enabled``, which is
    # OoLu charging its OWN prices through the LaunchGuard.
    ordering_enabled: bool = False,
) -> HostRuntime:
    """The multi-user web host: the full multi-tenant gateway over one
    data directory, with LOCAL accounts as the identity provider.

    Identity semantics are unchanged from a real-IdP deployment — bearer
    tokens through ``OidcValidator``, authority from stored grants — the
    only local part is who signs the tokens: this install's own HMAC
    secret (the self-host trade; ``assert_production_identity`` still
    refuses this shape for production-money deployments).

    ``database_url`` selects the durable backend for the workflow runtime:
    a PostgreSQL DSN puts runs/metering/registry/ratings on an online
    database (so several app clients share one server), while the default
    keeps them in ``data_dir/host.db`` (SQLite). Identity/account stores
    and a few auxiliary stores still live under ``data_dir`` either way —
    fine for a single hosted node; porting those to Postgres is what a
    multi-node deployment would add next.
    """
    if len(secret) < 32:
        raise ValueError("the host secret must be at least 32 characters")
    settings = settings or Settings()
    data = Path(data_dir)
    data.mkdir(parents=True, exist_ok=True)

    # Imported lazily so shells without the gateway never pay for it.
    import os as _os

    from .billing import (
        FakeCardVault,
        LaunchGuard,
        ModelCallMeter,
        PaymentMethodsService,
        PaymentProfileStore,
        SubscriptionService,
    )
    from .durable.files import UserFileStore
    from .gateway import GatewayApp
    from .gateway.asgi import GatewayASGI
    from .gateway.notify import RunEventNotifier, WebhookEndpointStore
    from .identity import (
        AuthorityResolver,
        Hs256Signer,
        Hs256Verifier,
        IdentityStore,
        LocalAccountService,
        LocalUserStore,
        OidcValidator,
        ProviderConfig,
    )
    from .identity.apikeys import ApiKeyService
    from .knowledge import TraceStore
    from .lessons import LessonStore
    from .mail import MailCodeStore
    from .metering import AttributionStore, MeteringLedger
    from .nodeplace import (
        CandidateAssembler,
        LiveVersionStats,
        NodeAccountStore,
        NodeplaceService,
        PriceBook,
        RatingService,
        RatingStore,
        RegistryStore,
        WorkDesk,
    )
    from .providers.keyring import ModelKeyring
    from .reminders import ReminderStore
    from .representative import (
        RepresentativeEngine,
        RepresentativeStore,
        StoreAdapterServer,
        VllmAdapterServer,
    )
    from .settings_node import SettingsNode, SettingsStore
    from .social import (
        AssistantHistoryStore,
        DirectMessageStore,
        FriendshipStore,
    )

    if database_url:
        from .durable.postgres import PostgresDurableConnection

        conn: Any = PostgresDurableConnection(database_url)
    else:
        conn = DurableConnection(data / "host.db")
    # The model plumbing is shared: the keyring holds the pasted keys, the
    # meter books every consultation, and the settings node carries the
    # provider/tier choice and the one spending cap that covers everything.
    model_keys = ModelKeyring(conn, key_path=data / "machine.key")
    model_meter = ModelCallMeter()
    settings_node = SettingsNode(SettingsStore(conn))
    _mail_codes = MailCodeStore(conn)
    # The payment second factor and the order-consent gate: OoLu may spend
    # money only when the account has TOTP enrolled and re-confirms the
    # exact amount with a fresh code (docs — Issue 6).
    import time as _time

    from .billing import PaymentAuthorizationStore
    from .identity import TotpStore

    # The consent scope is "tenant:principal"; TOTP is keyed by principal.
    def _principal_of(scope: str) -> str:
        return scope.split(":", 1)[-1]

    _totp = TotpStore(conn, key_path=data / "machine.key")
    _payment_auth = PaymentAuthorizationStore(
        conn,
        verify_second_factor=lambda scope, code: _totp.verify(
            _principal_of(scope), code, now=_time.time()
        ),
        second_factor_enrolled=lambda scope: _totp.is_enrolled(
            _principal_of(scope)
        ),
    )
    home_tenant = (
        getattr(config, "registration_tenant", None) if config else None
    ) or "main"

    # The hosted plan's brain: platform keys follow the environment on
    # every boot (set → stored encrypted, unset → removed, so rotation and
    # revocation are a restart, not a migration). The usage store books
    # every tenant's consultations durably; the subscription brain serves
    # model.source="subscription" inside the plan's monthly allowance.
    from .billing import PLATFORM_TENANT, ModelUsageStore, SubscriptionBrain
    from .providers.keyring import PROVIDERS

    if platform_model_keys is not None:
        for provider in PROVIDERS:
            platform_secret = (platform_model_keys.get(provider) or "").strip()
            if platform_secret:
                model_keys.store(PLATFORM_TENANT, provider, platform_secret)
            else:
                model_keys.remove(PLATFORM_TENANT, provider)
    model_usage = ModelUsageStore(conn)
    subscription_brain = SubscriptionBrain(
        model_keys,
        model_usage,
        plan_for=lambda tenant: str(
            settings_node.effective(tenant).get("subscription.plan", "free")
        ),
    )

    def _model_setting(key: str, fallback):
        return settings_node.effective(home_tenant).get(key, fallback)

    def _planning_router(purpose: str):
        from .providers.chatmodel import ChatModelRouter

        return ChatModelRouter(
            model_keys,
            home_tenant,
            meter=model_meter,
            subscription=subscription_brain,
            budget=lambda: float(_model_setting("budget.model_cap", 0.0) or 0.0),
            currency=lambda: str(_model_setting("account.currency", "USD")),
            preference=lambda: str(_model_setting("model.provider", "auto")),
            tier=lambda: str(_model_setting("model.tier", "fast")),
            source=lambda: str(_model_setting("model.source", "subscription")),
            local_url=lambda: str(_model_setting("model.local_url", "")),
            local_model=lambda: str(_model_setting("model.local_model", "")),
            web_search=lambda: bool(_model_setting("model.web_search", True)),
            purpose=purpose,
        )

    # Milestone A's key, bridged into planning: intake structures briefs and
    # route choice turns semantic. Both degrade to the deterministic floor
    # (heuristic intake, least-cost routes) the moment no key answers.
    if intake_model is None:
        from .providers.chatmodel import RouterIntakeModel

        intake_model = RouterIntakeModel(_planning_router("plan.intake"))

    # Execution retry's last resort: after the user's retries run out the
    # engine calls the model out to plan the steps and write the code —
    # gated per tenant on the "Auto-build nodes on my paths" consent — and
    # runs it through the script hand below, which verifies by execution
    # before trusting anything. A machine without a script runtime keeps
    # working; the rebuild then refuses with the reason instead of firing.
    from .orchestrator.rebuild import (
        AUTOBUILD_CONSENT_KEY,
        REBUILD_PURPOSE,
        LLMRouteRebuilder,
    )

    def _autobuild_consent(tenant: str, principal: str = "") -> bool:
        # Personal-first: the submitting account's own consent, with the
        # tenant layer as the shared default.
        return bool(
            settings_node.effective(tenant or home_tenant, principal or None).get(
                AUTOBUILD_CONSENT_KEY, False
            )
        )

    rebuilder = LLMRouteRebuilder(
        _planning_router(REBUILD_PURPOSE), consent=_autobuild_consent
    )
    run_executors = dict(executors or {})
    # The order-placing hands, tied to the payment gate: a released
    # authorization_id (consent + fresh 2FA + exact-amount re-confirmation)
    # is what ``_order_authorized`` checks before any money step runs. No
    # driver → no road registered → the optimizer simply excludes ordering.
    if site_driver is not None or amazon_client is not None:
        from .billing import PaymentAuthorizationResolver

        # The mint-and-attach seam: an order action that declares its intent
        # (payee, exact amount, run, scope) files the consent request and
        # proceeds the instant the user authorizes it — the released auth_id
        # is resolved live, not hand-wired onto the plan.
        _order_resolver = PaymentAuthorizationResolver(_payment_auth)
        run_executors.update(
            build_commerce_executors(
                amazon_client=amazon_client,
                site_driver=site_driver,
                is_authorized=_payment_auth.is_authorized,
                orders_enabled=lambda: ordering_enabled,
                resolve_authorization=_order_resolver.resolve,
            )
        )
    if require_isolation and settings.backend.kind != "docker":
        logging.getLogger(__name__).warning(
            "public host without an isolation backend (backend.kind=%s): "
            "the script hand stays OFF — synthesized code never runs "
            "unsandboxed on a public host",
            settings.backend.kind,
        )
    elif "script" not in run_executors:
        try:
            from .runtime.script_node import ChatModelSynthesizer

            run_executors.update(
                build_script_executor(
                    settings,
                    cache_path=data / "scripts.db",
                    synthesizer=ChatModelSynthesizer(
                        _planning_router("plan.synthesize")
                    ),
                )
            )
        except Exception as exc:  # noqa: BLE001 - hosts without a script
            # runtime still serve; the rebuild simply refuses with a reason.
            logging.getLogger(__name__).warning(
                "script runtime unavailable (%s); LLM rebuild disabled", exc
            )

    factory = build_orchestrator_factory(
        settings,
        intake_model=intake_model,
        skills=skills,
        blueprints=blueprints,
        grounding_map=grounding_map,
        executors=run_executors,
        route_model=_planning_router("plan.route"),
        rebuilder=rebuilder,
        # Turn shopping asks into commerce routes only where the order-placing
        # hands are actually wired — the same opt-in that registered them.
        commerce_planning=site_driver is not None or amazon_client is not None,
    )
    durable = DurableWorkflowService(conn, factory)

    identity = IdentityStore(data / "identity.db")
    users = LocalUserStore(data / "users.db")
    signer = Hs256Signer(secret=secret, issuer=LOCAL_ISSUER, audience=LOCAL_AUDIENCE)
    identity_providers = [
        ProviderConfig(
            issuer=LOCAL_ISSUER,
            audiences=frozenset({LOCAL_AUDIENCE}),
            verifier=Hs256Verifier(secret),
        )
    ]
    validator = OidcValidator(identity_providers)
    resolver = AuthorityResolver(identity)
    accounts = LocalAccountService(
        users, identity, signer, token_ttl_seconds=token_ttl_seconds
    )

    registry = RegistryStore(conn)
    metering = MeteringLedger(conn)
    attribution = AttributionStore(conn)
    ratings = RatingService(RatingStore(conn), verified_run=metering.verified_run)
    stats = LiveVersionStats(
        metering=metering, audit=durable.audit, attribution=attribution
    )
    node_accounts = NodeAccountStore(conn)
    # Supernode KYC: verified legal entities carry a global trust-ranking
    # multiplier. The reviewing work rides on the paying plan's fee, and
    # applications are screened by company-mail domain before any human
    # looks (OOLU_KYC_TRUSTED_DOMAINS fast-tracks verified domains).
    from .nodeplace import KycService, KycStore

    kyc = KycService(
        KycStore(conn),
        accounts=node_accounts,
        plan_for=lambda tenant: settings_node.effective(tenant).get(
            "subscription.plan", "free"
        ),
    )
    # Node hygiene: clone/fraud/zombie detection with the restriction the
    # Node Policy (agreed at creation) authorizes. Restricted nodes leave
    # ranking here and refuse new runs at the gateway.
    from .nodeplace import NodeHygieneService

    hygiene = NodeHygieneService(
        registry=registry,
        accounts=node_accounts,
        stats=stats,
        attribution=attribution,
    )
    market = CandidateAssembler(
        registry=registry,
        stats=stats,
        ratings=ratings,
        trust=kyc.trust_multiplier,
        restricted=hygiene.is_restricted,
    )
    # The Work environment's operator view: node accounts + earnings +
    # verified health + per-node execution feeds, over the same stores.
    desk = WorkDesk(
        registry=registry,
        accounts=node_accounts,
        metering=metering,
        stats=stats,
        attribution=attribution,
        audit=durable.audit,
    )
    price_book = PriceBook(data / "prices.db")
    traces = TraceStore(data / "traces.db")
    # One representative store serves both the gateway (drafting, status)
    # and the trainer worker's registry writes — same file, same schema.
    _representative_store = RepresentativeStore(data / "representative.db")

    # Publish is gated on proof: at least one verified run (local runs
    # through the node's own function count) before a listing can go
    # active on the global nodeplace — a name is not a capability.
    nodeplace_service = NodeplaceService(
        registry,
        verified=lambda version_id: stats.version_stats(version_id).successes > 0,
    )
    if seed_handiwork_for:
        # The desktop's prebuilt hands, packaged as ONE visible node the
        # local user answers for. Idempotent — created on first launch.
        from .nodeplace.handiwork import seed_handiwork_node

        seed_handiwork_node(
            nodeplace_service,
            desk,
            registry,
            tenant=home_tenant,
            principal=seed_handiwork_for,
            skills=skills,
        )

    # E-mail/IdP identities -> local accounts, shared by registration and
    # Google sign-in so "already registered" means the same thing everywhere.
    from .identity.google_signin import IdentityLinkStore

    identity_links = IdentityLinkStore(conn)

    # The money stack. Always wired so earnings/payout/dispute surfaces
    # answer; the ADAPTERS decide test vs live: a Stripe secret key swaps
    # the fake card vault and payout adapter for the real ones, and the
    # webhook secret opens the Stripe event door. Real charging is still
    # triple-gated by the launch guard (operator switch, settled prices,
    # verified successes) and by require_production_money.
    from .billing import (
        BillingService,
        DisputeService,
        DisputeStore,
        EarningsLedger,
        FakePayoutAdapter,
        PayoutStore,
        StripeCardVault,
        StripeConnectAdapter,
    )
    from .providers.vault import SecretVault

    earnings_ledger = EarningsLedger(conn)
    payout_store = PayoutStore(conn)
    if stripe_secret_key:
        from .providers.transport import HttpxTransport

        stripe_vault = SecretVault()
        stripe_key_ref = stripe_vault.put(stripe_secret_key, kind="stripe")
        stripe_transport = HttpxTransport()
        card_vault: Any = StripeCardVault(
            vault=stripe_vault,
            transport=stripe_transport,
            api_key_ref=stripe_key_ref,
        )
        payout_adapter: Any = StripeConnectAdapter(
            vault=stripe_vault,
            transport=stripe_transport,
            api_key_ref=stripe_key_ref,
        )
    else:
        card_vault = FakeCardVault()
        payout_adapter = FakePayoutAdapter()
    dispute_service = DisputeService(
        ledger=earnings_ledger,
        disputes=DisputeStore(conn),
        durable=conn,
        providers=identity_providers,
        idempotency=durable.idempotency,
    )
    stripe_webhooks = None
    if stripe_webhook_secret:
        from .gateway.webhooks import StripeWebhookVerifier

        stripe_webhooks = StripeWebhookVerifier(stripe_webhook_secret)

    # "Continue with Google": only when an OAuth client is configured. The
    # id_token verifier is Google's JWKS (RS256) — requires the oidc extra;
    # the import error below says exactly how to get it.
    google = None
    if google_client_id:
        from .identity.google_signin import GoogleSignIn, GoogleSignInConfig
        from .identity.jwks import JwksVerifier
        from .providers.transport import HttpxTransport

        google_transport = HttpxTransport()
        google = GoogleSignIn(
            accounts,
            identity_links,
            GoogleSignInConfig(
                client_id=google_client_id, client_secret=google_client_secret
            ),
            verifier=JwksVerifier(
                fetch=lambda: google_transport.request(
                    "GET", "https://www.googleapis.com/oauth2/v3/certs"
                ).json
            ),
            transport=google_transport,
            default_tenant=google_default_tenant,
            # A first-arrival Google account is born with a USABLE
            # password, mailed to the proven address — Settings then
            # only ever needs "change password", never "set".
            notify_password=(
                (
                    lambda email, username, password: mail.send(
                        to=email,
                        subject="Your OoLu account password",
                        body=(
                            f"Welcome to OoLu! Your account is {username} "
                            f"and your password is {password} — change it "
                            "in Settings whenever you like."
                        ),
                    )
                )
                if mail is not None
                else None
            ),
        )

    # The representative honours the same measurement-units preference the
    # chat assistant does, resolved from the SAME stored signal — the account's
    # spending currency — so "auto" gives the identical answer on both
    # surfaces. Scope is "tenant:principal"; units and currency are per-tenant.
    from .chat import units_directive

    def _representative_units_note(scope: str) -> str | None:
        # Scope is "tenant:principal": the units the draft speaks are the
        # ACCOUNT's own (personal layer first, tenant as the shared base) —
        # the same resolution the chat assistant uses.
        tenant, _, principal = scope.partition(":")
        effective = settings_node.effective(tenant, principal or None)
        return units_directive(
            effective.get("account.units", "auto"),
            currency=effective.get("account.currency", "USD"),
        )

    gateway = GatewayApp(
        durable,
        validator=validator,
        resolver=resolver,
        approval_authority=IdentityApprovalAuthority(resolver),
        config=config,
        nodeplace=nodeplace_service,
        ratings=ratings,
        market=market,
        price_book=price_book,
        attribution=attribution,
        # Verified-run evidence: a run through a node's own function lands
        # here, so built nodes verify from LOCAL use — the door out of
        # needs_verification that marketplace bindings alone never open.
        metering=metering,
        trace_store=traces,
        # Contract runs get the same hands as the orchestrator — the
        # script executor included, so a node's OWN function (its script
        # action) executes and routes locally instead of falling back to
        # the global machinery.
        contract_executors=run_executors,
        accounts=accounts,
        desk=desk,
        kyc=kyc,
        hygiene=hygiene,
        # The drawer with its blob door: inline documents in the database,
        # real binaries (PDF/DOCX/MP4/...) as content-addressed files on
        # disk — the database never swallows a video.
        files=UserFileStore(conn, artifacts=blob_store_from_env(data)),
        settings_node=settings_node,
        # The brain behind chat: the same keyring/meter planning uses —
        # pasted keys survive restarts encrypted, every consultation is
        # metered, and one spending cap covers chat AND planning.
        model_keys=model_keys,
        model_meter=model_meter,
        # The hosted plan's brain and the per-tenant usage books behind it.
        subscription=subscription_brain,
        model_usage=model_usage,
        google_signin=google,
        identity_links=identity_links,
        # The mail door: verification-first registration and password
        # reset switch on the moment a sender exists; the code store is
        # always there so verified marks survive sender changes.
        mail=mail,
        mail_codes=_mail_codes,
        sms=sms,
        totp=_totp,
        payment_authorizations=_payment_auth,
        local_files_root=Path(local_files_root) if local_files_root else None,
        # People talking to people, and one OoLu thread per account that
        # every signed-in device shares.
        direct_messages=DirectMessageStore(conn),
        friendships=FriendshipStore(conn),
        assistant_history=AssistantHistoryStore(conn),
        # Reminders: rows with a clock — the deterministic route for
        # "remind me", surfaced by the client's poll.
        reminders=ReminderStore(conn),
        # Imitate: guided demonstrations recorded in a node's window —
        # the training data logs that build capable nodes.
        lessons=LessonStore(conn),
        # The representative: replies drafted in each account's own voice
        # (docs/representative-plan.md). Local SQLite like every learned
        # store; the per-tenant chat router is handed in per call by the
        # gateway. When OOLU_REPRESENTATIVE_VLLM names the multi-LoRA
        # server (api_base, /v1 included), accounts with a trained adapter
        # draft through their OWN voice — the trainer worker (Phase 1)
        # writes the registry this reads.
        representative=RepresentativeEngine(
            _representative_store,
            adapters=(
                VllmAdapterServer(
                    _representative_store,
                    api_base=_os.environ["OOLU_REPRESENTATIVE_VLLM"],
                )
                if _os.environ.get("OOLU_REPRESENTATIVE_VLLM")
                else StoreAdapterServer(_representative_store)
            ),
            units_note_for=_representative_units_note,
        ),
        # The operator's legal documents; marked templates answer until
        # terms.md / privacy.md exist here.
        legal_dir=data / "legal",
        # The card vault is Stripe when a secret key exists, the test
        # double otherwise; the launch guard's transaction port opens only
        # by the operator's explicit switch (prices and verification still
        # gate per class of work).
        payments=PaymentMethodsService(PaymentProfileStore(conn), card_vault),
        launch_guard=LaunchGuard(transactions_enabled=transactions_enabled),
        # The plan lifecycle behind the account console; it mirrors its
        # state into the (managed, display-only) subscription settings and
        # tells the truth about whether choosing a plan actually charges.
        subscriptions=SubscriptionService(
            conn,
            settings=settings_node,
            charging_open=lambda: transactions_enabled,
        ),
        # Earnings/payout/dispute surfaces over the same durable books the
        # charge and settlement services write.
        billing=BillingService(earnings_ledger),
        payout_store=payout_store,
        payout_adapter=payout_adapter,
        disputes=dispute_service,
        stripe_webhooks=stripe_webhooks,
        # The public execution API: machine keys + signed run webhooks.
        api_keys=ApiKeyService(conn),
        webhook_endpoints=(endpoints := WebhookEndpointStore(conn)),
        notifier=RunEventNotifier(
            audit=durable.audit, durable=durable, endpoints=endpoints, conn=conn
        ),
    )
    # The shell may call exactly one origin beyond itself: the online
    # server this install pairs with. The CSP widens to it and nothing else.
    paired = getattr(config, "server_url", None) if config else None
    return HostRuntime(
        gateway=gateway,
        asgi=GatewayASGI(
            gateway,
            frontend=frontend,
            shell_remote=shell_remote,
            admin_hosts=admin_hosts,
            connect_src=(paired,) if paired else (),
        ),
        accounts=accounts,
        identity=identity,
        conn=conn,
        _closers=(users, identity, price_book, traces),
    )
