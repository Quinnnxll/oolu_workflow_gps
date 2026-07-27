"""The router — typed intents in, ranked capability nodes out, no commands.

Three walls stand before any scoring:

* **The raw-command wall** (acceptance test 19): an intent whose
  desired-state predicate speaks servo-or-drive vocabulary — joint
  targets, axis velocities, G-code, PWM — is rejected at this boundary
  by name. Models describe desired *workpiece states*; machines are
  addressed only by the deterministic compiler in R4, never from here.
* **Hard exclusions with named reasons**: wrong capability family,
  unproducible desired property, incompatible input state, an
  irreversible effect with no declared disposition path, a stale
  safety case, an unqualified workcell. An excluded node is reported,
  not silently dropped — the exclusion list is how an operator learns
  why the web refused.
* **The score is a breakdown, not a number**: §18.1's components are
  returned per candidate so a routing choice is auditable term by term.
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel, ConfigDict

from ..actuation.contracts import ActionIntent, Reversibility
from .nodes import ActuationNode

# Vocabulary that addresses a machine rather than describing a
# workpiece state. Prefix or exact-key match, case-insensitive.
RAW_COMMAND_PREFIXES = (
    "joint_",
    "servo_",
    "axis_",
    "drive_",
    "motor_",
    "pwm_",
    "gcode",
    "spindle_",
)
RAW_COMMAND_KEYS = ("velocity", "torque", "trajectory", "waypoints", "pose_stream")

_IRREVERSIBILITY_RISK = {
    Reversibility.REVERSIBLE: 0.0,
    Reversibility.COMPENSATABLE: 0.2,
    Reversibility.REWORKABLE: 0.4,
    Reversibility.IRREVERSIBLE: 1.0,
}


class RawCommandRejected(ValueError):
    """The intent tried to address a machine instead of a state."""

    def __init__(self, keys: tuple[str, ...]) -> None:
        super().__init__(
            "raw machine-command vocabulary rejected at the routing "
            f"boundary: {', '.join(keys)}"
        )
        self.keys = keys


def reject_raw_commands(intent: ActionIntent) -> None:
    offending = tuple(
        key
        for key in intent.desired_state_predicate
        if key.lower() in RAW_COMMAND_KEYS
        or any(key.lower().startswith(prefix) for prefix in RAW_COMMAND_PREFIXES)
    )
    if offending:
        raise RawCommandRejected(offending)


class ScoreBreakdown(BaseModel):
    """§18.1's actuation node score, term by term."""

    model_config = ConfigDict(frozen=True)

    input_state_compatibility: float
    desired_state_reachability: float
    workspace_and_payload_margin: float
    tolerance_and_process_capability: float
    tool_fixture_material_compatibility: float
    validated_envelope_match: float
    safety_case_match: float
    verification_coverage: float
    availability: float
    historical_yield: float
    cost: float
    delay: float
    irreversibility_risk: float
    process_drift_risk: float

    def total(self) -> float:
        return (
            self.input_state_compatibility
            + self.desired_state_reachability
            + self.workspace_and_payload_margin
            + self.tolerance_and_process_capability
            + self.tool_fixture_material_compatibility
            + self.validated_envelope_match
            + self.safety_case_match
            + self.verification_coverage
            + self.availability
            + self.historical_yield
            - self.cost
            - self.delay
            - self.irreversibility_risk
            - self.process_drift_risk
        )


class ScoredCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    node_version_id: str
    breakdown: ScoreBreakdown

    @property
    def score(self) -> float:
        return self.breakdown.total()


class ExcludedNode(BaseModel):
    model_config = ConfigDict(frozen=True)

    node_version_id: str
    reasons: tuple[str, ...]


class RoutingDecision(BaseModel):
    """Ranked candidates and named exclusions. Data, never commands."""

    model_config = ConfigDict(frozen=True)

    intent_id: str
    candidates: tuple[ScoredCandidate, ...]
    excluded: tuple[ExcludedNode, ...]


def _exclusions(
    intent: ActionIntent,
    node: ActuationNode,
    input_state: dict[str, str | int | bool],
    workcell_qualified: Callable[[str], bool] | None,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if node.capability.family not in intent.allowed_capability_families:
        reasons.append(f"capability_family_not_allowed:{node.capability.family.value}")
    for prop in intent.desired_state_predicate:
        if prop not in node.producible_properties:
            reasons.append(f"desired_state_unreachable:{prop}")
    for prop, required in node.accepted_input_properties.items():
        if input_state.get(prop) != required:
            reasons.append(f"input_state_incompatible:{prop}")
    if (
        node.capability.reversibility is Reversibility.IRREVERSIBLE
        and not node.disposition_paths
    ):
        reasons.append("irreversible_without_disposition_path")
    if not node.safety_case_current:
        reasons.append("safety_case_not_current")
    if workcell_qualified is not None and not workcell_qualified(
        node.workcell_version_id
    ):
        reasons.append(f"workcell_not_qualified:{node.workcell_version_id}")
    return tuple(reasons)


def _score(
    intent: ActionIntent,
    node: ActuationNode,
    input_state: dict[str, str | int | bool],
) -> ScoreBreakdown:
    matched_inputs = sum(
        1
        for prop, required in node.accepted_input_properties.items()
        if input_state.get(prop) == required
    )
    input_compat = (
        matched_inputs / len(node.accepted_input_properties)
        if node.accepted_input_properties
        else 1.0
    )
    tolerance = 0.0
    required_um = intent.quality_acceptance_criteria.get("tolerance_um")
    if isinstance(required_um, int) and node.achievable_tolerance_um is not None:
        if node.achievable_tolerance_um <= required_um:
            # Margin ratio: achieving 2 µm against a 10 µm ask scores
            # higher than barely achieving 10 µm.
            tolerance = 1.0 - (node.achievable_tolerance_um / required_um)
    required_tools = set(node.capability.required_tool_types)
    tool_compat = (
        len(required_tools & set(node.available_tool_types)) / len(required_tools)
        if required_tools
        else 1.0
    )
    return ScoreBreakdown(
        input_state_compatibility=input_compat,
        desired_state_reachability=1.0,  # unreachable nodes were excluded
        workspace_and_payload_margin=node.workspace_and_payload_margin,
        tolerance_and_process_capability=tolerance,
        tool_fixture_material_compatibility=tool_compat,
        validated_envelope_match=(
            1.0 if node.capability.validated_operating_envelope_ids else 0.0
        ),
        safety_case_match=1.0,  # stale safety cases were excluded
        verification_coverage=node.verification_coverage,
        availability=node.availability,
        historical_yield=node.historical_yield,
        cost=node.cost,
        delay=node.delay,
        irreversibility_risk=_IRREVERSIBILITY_RISK[node.capability.reversibility],
        process_drift_risk=node.process_drift_risk,
    )


def route(
    intent: ActionIntent,
    nodes: tuple[ActuationNode, ...],
    *,
    input_state: dict[str, str | int | bool],
    workcell_qualified: Callable[[str], bool] | None = None,
) -> RoutingDecision:
    """Rank compatible nodes for a typed physical goal.

    ``workcell_qualified`` is the seam to R2's workcell registry
    (``lambda cell: registry.workcell(cell).commissioning_status ==
    "qualified"``); without it, commissioning status is not checked
    here and the release gate still catches it later.
    """

    reject_raw_commands(intent)
    candidates: list[ScoredCandidate] = []
    excluded: list[ExcludedNode] = []
    for node in nodes:
        reasons = _exclusions(intent, node, input_state, workcell_qualified)
        if reasons:
            excluded.append(
                ExcludedNode(node_version_id=node.node_version_id, reasons=reasons)
            )
            continue
        candidates.append(
            ScoredCandidate(
                node_version_id=node.node_version_id,
                breakdown=_score(intent, node, input_state),
            )
        )
    candidates.sort(key=lambda candidate: candidate.score, reverse=True)
    return RoutingDecision(
        intent_id=intent.intent_id,
        candidates=tuple(candidates),
        excluded=tuple(excluded),
    )
