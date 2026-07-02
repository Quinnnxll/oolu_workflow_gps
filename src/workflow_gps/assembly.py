from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

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
    RiskBasedHumanControl,
    StatusOutcomeMonitor,
    WorkflowOrchestrator,
)
from .orchestrator.intake import IntakeModel
from .orchestrator.state import Blueprint, RoutePlan, SemanticEdge, SemanticGrounding
from .providers.vault import SecretVault
from .skills.ports import ActionExecutor
from .skills.requirements import RequirementBrief
from .worker.policy import IsolationPolicy

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


def build_intake_model(settings: Settings | None = None) -> IntakeModel:
    settings = settings or Settings()
    from .orchestrator.intake import LiteLLMIntakeModel

    return LiteLLMIntakeModel(
        settings.routing.fast.model, timeout=settings.request_timeout_s
    )


def build_orchestrator_factory(
    settings: Settings | None = None,
    *,
    intake_model: IntakeModel | None = None,
    blueprints: list[Blueprint] | None = None,
    grounding_map: dict[str, str] | None = None,
    executors: dict[str, ActionExecutor] | None = None,
) -> OrchestratorFactory:
    intaker = ModelBackedIntaker(intake_model)
    executor = ActionExecutorRouteRunner(dict(executors or {}))

    if blueprints:
        grounder: object = CapabilityGrounder(
            dict(grounding_map or {}), always_resolved=executor.capabilities()
        )
        optimizer: object = LeastCostRouteOptimizer(list(blueprints))
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
