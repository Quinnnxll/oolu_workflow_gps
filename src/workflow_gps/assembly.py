from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

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


def build_intake_model(
    settings: Settings | None = None,
    *,
    registry: "SkillRegistry | None" = None,
) -> IntakeModel:
    settings = settings or Settings()
    from .orchestrator.intake import LiteLLMIntakeModel

    context_provider = None
    if registry is not None:
        from .skills.context import SkillContextBuilder

        context_provider = SkillContextBuilder(
            registry, max_tools=settings.skills.max_context_tools
        ).manifest

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
) -> DesktopRuntime:
    settings = settings or Settings()
    conn = DurableConnection(db_path)
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
    )
    return DesktopRuntime(desktop=desktop, durable=durable, conn=conn)
