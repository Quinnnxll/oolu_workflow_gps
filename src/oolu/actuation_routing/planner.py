"""Plan assembly and the release gate — the last stop before R4.

``build_plan`` binds a routed candidate to exact resources under the
protocol's containment rules: parameters inside the validated envelope,
the recipe validated against that same envelope, disposition paths
mandatory for irreversible effects, and a qualified workcell. The plan
digest (domain ``oolu-actuation-plan/v1``) binds every one of those
choices; it is what simulations simulate, reviews review, and approvals
approve — the future R0 ``ActuationJob.plan_version_id`` names it.

``ready_for_compilation`` is the R3 terminal gate: plan, qualified
simulation, review decision, and a consumable approval bundle, checked
together, refusing by name. What it returns is a verdict — the artifact
eligible for the deterministic compiler. It does not compile, arm, or
command anything; those doors do not exist in this package.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from ..actuation.contracts import (
    ActionIntent,
    RecipeVersion,
    Reversibility,
    ValidatedOperatingEnvelope,
)
from ..actuation.digests import canonical_json, sha256_hex
from ..actuation.review import ReviewDecision
from ..registries.workcells import WorkcellRow
from .approvals import ActuationApprovalService
from .nodes import ActuationNode
from .simulation import SimulationRecord, SimulatorQualification, qualify_simulation

PLAN_DOMAIN = b"oolu-actuation-plan/v1\n"


class PlanAssemblyError(ValueError):
    def __init__(self, reasons: tuple[str, ...]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


class ActuationPlan(BaseModel):
    """A fully bound, digest-carrying plan. Data — never a command."""

    model_config = ConfigDict(frozen=True)

    plan_id: str
    intent_id: str
    node_version_id: str
    workcell_version_id: str
    tool_instance_ids: tuple[str, ...]
    fixture_instance_ids: tuple[str, ...]
    coordinate_frame_graph_digest: str
    recipe_version_id: str
    process_parameters: dict[str, int]
    envelope_id: str
    expected_state_delta: dict[str, str | int | bool]
    disposition_paths: tuple[str, ...]

    def digest(self) -> str:
        return sha256_hex(
            PLAN_DOMAIN
            + canonical_json(
                {
                    "plan_id": self.plan_id,
                    "intent_id": self.intent_id,
                    "node_version_id": self.node_version_id,
                    "workcell_version_id": self.workcell_version_id,
                    "tool_instance_ids": sorted(self.tool_instance_ids),
                    "fixture_instance_ids": sorted(self.fixture_instance_ids),
                    "coordinate_frame_graph_digest": (
                        self.coordinate_frame_graph_digest
                    ),
                    "recipe_version_id": self.recipe_version_id,
                    "process_parameters": dict(self.process_parameters),
                    "envelope_id": self.envelope_id,
                    "expected_state_delta": dict(self.expected_state_delta),
                    "disposition_paths": sorted(self.disposition_paths),
                }
            )
        )


def build_plan(
    intent: ActionIntent,
    node: ActuationNode,
    *,
    plan_id: str,
    workcell: WorkcellRow,
    frame_graph_digest: str,
    tool_instance_ids: tuple[str, ...],
    fixture_instance_ids: tuple[str, ...],
    recipe: RecipeVersion,
    envelope: ValidatedOperatingEnvelope,
    expected_state_delta: dict[str, str | int | bool],
) -> ActuationPlan:
    """Bind a routed candidate to exact resources, or refuse by name."""

    reasons: list[str] = []
    if workcell.workcell_version_id != node.workcell_version_id:
        reasons.append("workcell_mismatch_with_node")
    if workcell.commissioning_status != "qualified":
        reasons.append(f"workcell_not_qualified:{workcell.commissioning_status}")
    if recipe.envelope_id != envelope.envelope_id:
        reasons.append(f"recipe_validated_against_other_envelope:{recipe.envelope_id}")
    reasons.extend(envelope.violations(recipe.parameters))
    if (
        node.capability.reversibility is Reversibility.IRREVERSIBLE
        and not node.disposition_paths
    ):
        reasons.append("irreversible_without_disposition_path")
    if reasons:
        raise PlanAssemblyError(tuple(reasons))
    return ActuationPlan(
        plan_id=plan_id,
        intent_id=intent.intent_id,
        node_version_id=node.node_version_id,
        workcell_version_id=workcell.workcell_version_id,
        tool_instance_ids=tool_instance_ids,
        fixture_instance_ids=fixture_instance_ids,
        coordinate_frame_graph_digest=frame_graph_digest,
        recipe_version_id=recipe.recipe_version_id,
        process_parameters=dict(recipe.parameters),
        envelope_id=envelope.envelope_id,
        expected_state_delta=expected_state_delta,
        disposition_paths=node.disposition_paths,
    )


class ReleaseVerdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    plan_digest: str
    eligible_for_compilation: bool
    refusals: tuple[str, ...] = ()


def ready_for_compilation(
    plan: ActuationPlan,
    *,
    simulation: SimulationRecord,
    simulator: SimulatorQualification,
    review: ReviewDecision,
    approvals: ActuationApprovalService,
    now: int,
) -> ReleaseVerdict:
    """The R3 terminal gate: everything checked together, refusing by name.

    A ``True`` verdict consumes the approval bundle exactly once — a
    second release attempt for the same digest refuses, the same way a
    spent execution lease does.
    """

    digest = plan.digest()
    refusals: list[str] = []
    if not review.physical_execution_permitted:
        refusals.append(f"physical_execution_not_permitted:{review.tier.value}")
    if simulation.plan_digest != digest:
        refusals.append("simulation_for_different_plan")
    else:
        refusals.extend(qualify_simulation(simulation, simulator))
        if not simulation.all_checks_passed:
            refusals.extend(
                f"simulation_check_failed:{check.family}"
                for check in simulation.checks
                if not check.passed
            )
        if simulation.coordinate_frame_graph_digest != (
            plan.coordinate_frame_graph_digest
        ):
            refusals.append("simulation_frame_graph_stale")
    if refusals:
        return ReleaseVerdict(
            plan_digest=digest,
            eligible_for_compilation=False,
            refusals=tuple(refusals),
        )
    consume_refusals = approvals.consume(digest, now=now)
    if consume_refusals:
        return ReleaseVerdict(
            plan_digest=digest,
            eligible_for_compilation=False,
            refusals=consume_refusals,
        )
    return ReleaseVerdict(plan_digest=digest, eligible_for_compilation=True)
