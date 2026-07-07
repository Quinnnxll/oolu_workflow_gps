from __future__ import annotations

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


def build_orchestrator_factory(
    settings: Settings | None = None,
    *,
    intake_model: IntakeModel | None = None,
    skills: list[ReusableSkill] | None = None,
    blueprints: list[Blueprint] | None = None,
    grounding_map: dict[str, str] | None = None,
    executors: dict[str, ActionExecutor] | None = None,
    route_model=None,  # chat.ChatModel: semantic route choice, optional
) -> OrchestratorFactory:
    from .orchestrator.adapters import ModelRouteOptimizer

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
    google_client_id: str | None = None,
    google_client_secret: str = "",
    google_default_tenant: str = "main",
    seed_handiwork_for: str | None = None,
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
    from .settings_node import SettingsNode, SettingsStore

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
    home_tenant = (
        getattr(config, "registration_tenant", None) if config else None
    ) or "main"

    def _model_setting(key: str, fallback):
        return settings_node.effective(home_tenant).get(key, fallback)

    def _planning_router(purpose: str):
        from .providers.chatmodel import ChatModelRouter

        return ChatModelRouter(
            model_keys,
            home_tenant,
            meter=model_meter,
            budget=lambda: float(_model_setting("budget.model_cap", 0.0) or 0.0),
            preference=lambda: str(_model_setting("model.provider", "auto")),
            tier=lambda: str(_model_setting("model.tier", "fast")),
            source=lambda: str(_model_setting("model.source", "subscription")),
            local_url=lambda: str(_model_setting("model.local_url", "")),
            local_model=lambda: str(_model_setting("model.local_model", "")),
            purpose=purpose,
        )

    # Milestone A's key, bridged into planning: intake structures briefs and
    # route choice turns semantic. Both degrade to the deterministic floor
    # (heuristic intake, least-cost routes) the moment no key answers.
    if intake_model is None:
        from .providers.chatmodel import RouterIntakeModel

        intake_model = RouterIntakeModel(_planning_router("plan.intake"))

    factory = build_orchestrator_factory(
        settings,
        intake_model=intake_model,
        skills=skills,
        blueprints=blueprints,
        grounding_map=grounding_map,
        executors=executors,
        route_model=_planning_router("plan.route"),
    )
    durable = DurableWorkflowService(conn, factory)

    identity = IdentityStore(data / "identity.db")
    users = LocalUserStore(data / "users.db")
    signer = Hs256Signer(secret=secret, issuer=LOCAL_ISSUER, audience=LOCAL_AUDIENCE)
    validator = OidcValidator(
        [
            ProviderConfig(
                issuer=LOCAL_ISSUER,
                audiences=frozenset({LOCAL_AUDIENCE}),
                verifier=Hs256Verifier(secret),
            )
        ]
    )
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
    market = CandidateAssembler(registry=registry, stats=stats, ratings=ratings)
    # The Work environment's operator view: node accounts + earnings +
    # verified health + per-node execution feeds, over the same stores.
    desk = WorkDesk(
        registry=registry,
        accounts=NodeAccountStore(conn),
        metering=metering,
        stats=stats,
        attribution=attribution,
        audit=durable.audit,
    )
    price_book = PriceBook(data / "prices.db")
    traces = TraceStore(data / "traces.db")

    nodeplace_service = NodeplaceService(registry)
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
        trace_store=traces,
        contract_executors=executors,
        accounts=accounts,
        desk=desk,
        files=UserFileStore(conn),
        settings_node=settings_node,
        # The brain behind chat: the same keyring/meter planning uses —
        # pasted keys survive restarts encrypted, every consultation is
        # metered, and one spending cap covers chat AND planning.
        model_keys=model_keys,
        model_meter=model_meter,
        google_signin=google,
        identity_links=identity_links,
        # Pre-launch: the test card vault and a closed launch guard — the
        # real transaction port stays shut until an operator opens it.
        payments=PaymentMethodsService(PaymentProfileStore(conn), FakeCardVault()),
        launch_guard=LaunchGuard(transactions_enabled=False),
        # The plan lifecycle behind the account console; it mirrors its
        # state into the (managed, display-only) subscription settings.
        subscriptions=SubscriptionService(conn, settings=settings_node),
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
            connect_src=(paired,) if paired else (),
        ),
        accounts=accounts,
        identity=identity,
        conn=conn,
        _closers=(users, identity, price_book, traces),
    )
