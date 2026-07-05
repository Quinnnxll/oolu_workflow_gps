from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .billing import EarningsLedger, PayoutStore
from .config import Settings
from .desktop.service import DesktopService
from .durable.artifacts import FilesystemArtifactStore
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
from .providers.vault import SecretVault
from .skills.models import ReusableSkill
from .skills.ports import ActionExecutor
from .skills.requirements import RequirementBrief
from .worker.policy import IsolationPolicy

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


@dataclass
class DesktopRuntime:
    desktop: DesktopService
    durable: DurableWorkflowService
    conn: DurableConnection
    # The same ledger objects the shell reads — hand THESE to a settlement
    # job so the earnings screen and the money pipeline share one truth.
    earnings: EarningsLedger | None = None
    payouts: PayoutStore | None = None

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "DesktopRuntime":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


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
) -> OrchestratorFactory:
    intaker = ModelBackedIntaker(intake_model)
    executor = ActionExecutorRouteRunner(dict(executors or {}))

    if skills and not blueprints:
        planner = SkillRegistryPlanner(skills)
        grounder: object = RegistryGrounder(planner.capabilities())
        optimizer: object = LeastCostRouteOptimizer(planner.blueprints())
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


LOCAL_ISSUER = "wfgps-local"
LOCAL_AUDIENCE = "wfgps"


@dataclass
class HostRuntime:
    """Everything `wfgps host` serves, over one data directory."""

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
) -> HostRuntime:
    """The multi-user web host: the full multi-tenant gateway over one
    data directory, with LOCAL accounts as the identity provider.

    Identity semantics are unchanged from a real-IdP deployment — bearer
    tokens through ``OidcValidator``, authority from stored grants — the
    only local part is who signs the tokens: this install's own HMAC
    secret (the self-host trade; ``assert_production_identity`` still
    refuses this shape for production-money deployments). Everything
    lives under ``data_dir``: one folder to back up.
    """
    if len(secret) < 32:
        raise ValueError("the host secret must be at least 32 characters")
    settings = settings or Settings()
    data = Path(data_dir)
    data.mkdir(parents=True, exist_ok=True)

    # Imported lazily so shells without the gateway never pay for it.
    from .gateway import GatewayApp
    from .gateway.asgi import GatewayASGI
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
    from .knowledge import TraceStore
    from .metering import AttributionStore, MeteringLedger
    from .nodeplace import (
        CandidateAssembler,
        LiveVersionStats,
        NodeplaceService,
        PriceBook,
        RatingService,
        RatingStore,
        RegistryStore,
    )

    conn = DurableConnection(data / "host.db")
    factory = build_orchestrator_factory(
        settings,
        intake_model=intake_model,
        skills=skills,
        blueprints=blueprints,
        grounding_map=grounding_map,
        executors=executors,
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
    market = CandidateAssembler(
        registry=registry,
        stats=LiveVersionStats(
            metering=metering, audit=durable.audit, attribution=attribution
        ),
        ratings=ratings,
    )
    price_book = PriceBook(data / "prices.db")
    traces = TraceStore(data / "traces.db")

    gateway = GatewayApp(
        durable,
        validator=validator,
        resolver=resolver,
        approval_authority=IdentityApprovalAuthority(resolver),
        config=config,
        nodeplace=NodeplaceService(registry),
        ratings=ratings,
        market=market,
        price_book=price_book,
        attribution=attribution,
        trace_store=traces,
        contract_executors=executors,
        accounts=accounts,
    )
    return HostRuntime(
        gateway=gateway,
        asgi=GatewayASGI(gateway),
        accounts=accounts,
        identity=identity,
        conn=conn,
        _closers=(users, identity, price_book, traces),
    )


def build_desktop_runtime(
    settings: Settings | None = None,
    *,
    db_path: str | Path,
    intake_model: IntakeModel | None = None,
    skills: list[ReusableSkill] | None = None,
    blueprints: list[Blueprint] | None = None,
    grounding_map: dict[str, str] | None = None,
    executors: dict[str, ActionExecutor] | None = None,
    approval_authority: IdentityApprovalAuthority | None = None,
    vault: SecretVault | None = None,
    isolation: IsolationPolicy | None = None,
    docker_available: bool = True,
    artifacts_dir: str | Path | None = None,
    noder_principal: str | None = "local-noder",
    payout_adapter: Any = None,  # billing.PayoutAdapter: onboarding + KYC refresh
    proposal_model: Any = None,  # orchestrator.ProposalModel: advice as a prior
) -> DesktopRuntime:
    settings = settings or Settings()
    conn = DurableConnection(db_path)
    # Earnings are wired by default: the screen shows honest zeros until
    # the user's contributions earn. Pass noder_principal=None to disable.
    earnings = EarningsLedger(conn) if noder_principal is not None else None
    payouts = PayoutStore(conn) if noder_principal is not None else None
    artifacts = (
        FilesystemArtifactStore(artifacts_dir) if artifacts_dir is not None else None
    )
    factory = build_orchestrator_factory(
        settings,
        intake_model=intake_model,
        skills=skills,
        blueprints=blueprints,
        grounding_map=grounding_map,
        executors=executors,
    )
    durable = DurableWorkflowService(conn, factory, artifacts=artifacts)
    desktop = DesktopService(
        durable,
        approval_authority=approval_authority,
        vault=vault,
        isolation=isolation,
        docker_available=docker_available,
        earnings_ledger=earnings,
        payout_store=payouts,
        payout_adapter=payout_adapter,
        noder_principal=noder_principal,
        proposal_model=proposal_model,
    )
    return DesktopRuntime(
        desktop=desktop,
        durable=durable,
        conn=conn,
        earnings=earnings,
        payouts=payouts,
    )
