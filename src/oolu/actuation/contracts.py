"""Typed contracts for approval-gated physical actuation.

These are the protocol's nouns: what a physical goal, a capability, a
validated envelope, a workcell, and an authorized job *are*, before any
service exists to move them. Two structural rules run through every
model here:

* **Frozen where authorization depends on it.** An ``ActuationJob`` is
  immutable; a material change is a new job with a new digest and a new
  approval cycle, never a mutation.
* **Physical parameters are scaled integers.** Digest-bound material
  refuses floats (see ``digests``), so every process parameter is an
  integer in the unit its envelope declares (micrometres, millinewtons,
  milli-degrees). The unit lives with the bound, once — not with every
  value.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from .digests import aggregate_digest


class CapabilityFamily(str, Enum):
    """The §24.2 actuation families — physical effects, not vendor verbs."""

    MANIPULATION = "manipulation"
    ASSEMBLY = "assembly"
    MATERIAL_REMOVAL = "material_removal"
    MATERIAL_ADDITION = "material_addition"
    JOINING = "joining"
    FORMING = "forming"
    TREATMENT = "treatment"
    TRANSPORT = "transport"
    TEST_ACTUATION = "test_actuation"
    CALIBRATION = "calibration"


class Reversibility(str, Enum):
    """Declared per capability; the router must price irreversibility."""

    REVERSIBLE = "reversible"
    COMPENSATABLE = "compensatable"
    REWORKABLE = "reworkable"
    IRREVERSIBLE = "irreversible"


class Disposition(str, Enum):
    """Terminal outcomes for a workpiece. There is no "rolled back"."""

    ACCEPTED = "accepted"
    REWORKED = "reworked"
    QUARANTINED = "quarantined"
    SCRAPPED = "scrapped"
    CONTAINED = "contained"


class CommissioningStatus(str, Enum):
    DRAFT = "draft"
    COMMISSIONING = "commissioning"
    QUALIFIED = "qualified"
    SUSPENDED = "suspended"
    RETIRED = "retired"


class ActionIntent(BaseModel):
    """A desired physical state, typed — never a motion command.

    The intent is the only thing a model plane may author. Everything
    downstream (plan, program, lease) is derived by deterministic,
    reviewable machinery.
    """

    model_config = ConfigDict(frozen=True)

    intent_id: str
    goal_id: str
    requested_by: str
    desired_state_predicate: dict[str, str | int | bool]
    input_workpiece_state_id: str
    allowed_capability_families: tuple[CapabilityFamily, ...]
    quality_acceptance_criteria: dict[str, str | int | bool] = Field(
        default_factory=dict
    )
    budget_minor_units: int | None = None
    deadline: str | None = None
    risk_tolerance_policy_id: str | None = None
    evidence_requirements: dict[str, str | int | bool] = Field(default_factory=dict)
    prior_artifact_refs: tuple[str, ...] = ()


class ParameterBound(BaseModel):
    """One validated bound: a closed integer range in a declared unit."""

    model_config = ConfigDict(frozen=True)

    unit: str
    minimum: int
    maximum: int

    def contains(self, value: int) -> bool:
        return self.minimum <= value <= self.maximum


class ValidatedOperatingEnvelope(BaseModel):
    """The box learned behavior may move inside and never widen.

    Invariant 27: learned optimization cannot silently expand a safety,
    process, material, or quality envelope. The envelope is frozen; a
    wider envelope is a *new* envelope id that re-earns validation.
    """

    model_config = ConfigDict(frozen=True)

    envelope_id: str
    workcell_version_id: str
    bounds: dict[str, ParameterBound]
    validation_evidence_refs: tuple[str, ...] = ()

    def violations(self, parameters: dict[str, int]) -> list[str]:
        """Named reasons ``parameters`` fall outside this envelope."""

        problems: list[str] = []
        for name, value in parameters.items():
            bound = self.bounds.get(name)
            if bound is None:
                problems.append(f"parameter {name!r} has no validated bound")
            elif not bound.contains(value):
                problems.append(
                    f"parameter {name!r}={value} outside validated "
                    f"[{bound.minimum}, {bound.maximum}] {bound.unit}"
                )
        for name in self.bounds:
            if name not in parameters:
                problems.append(f"bounded parameter {name!r} missing")
        return problems


class ToolInstance(BaseModel):
    model_config = ConfigDict(frozen=True)

    tool_instance_id: str
    tool_type: str
    serial_number: str
    geometry_digest: str
    calibration_record_ids: tuple[str, ...] = ()
    status: str = "available"  # available | due_inspection | quarantined | retired


class WorkcellVersion(BaseModel):
    """The qualified physical context a job binds to, as one version."""

    model_config = ConfigDict(frozen=True)

    workcell_version_id: str
    facility_id: str
    machine_instance_ids: tuple[str, ...]
    controller_instance_ids: tuple[str, ...]
    safety_controller_instance_ids: tuple[str, ...]
    coordinate_frame_graph_digest: str
    safety_configuration_digest: str
    validated_operating_envelope_ids: tuple[str, ...]
    risk_assessment_id: str
    commissioning_status: CommissioningStatus = CommissioningStatus.DRAFT


class ActuationJob(BaseModel):
    """The §24.4 authorized actuation job — every binding, one digest.

    ``aggregate()`` recomputes the digest from the bound fields; any
    mismatch between a stored digest and a recomputation means some
    bound identity changed after approval, and the job cannot arm.
    """

    model_config = ConfigDict(frozen=True)

    job_id: str
    intent_id: str
    plan_version_id: str
    workcell_version_id: str
    workpiece_instance_ids: tuple[str, ...]
    input_state_digest: str
    machine_instance_ids: tuple[str, ...]
    controller_instance_ids: tuple[str, ...]
    tool_instance_ids: tuple[str, ...]
    fixture_instance_ids: tuple[str, ...]
    coordinate_frame_graph_digest: str
    process_recipe_version_id: str
    process_parameters: dict[str, int]
    machine_program_digest: str
    validated_operating_envelope_id: str
    local_safety_configuration_digest: str
    expected_state_delta: dict[str, str | int | bool]
    approval_bundle_id: str
    execution_window_start: str
    execution_window_end: str
    nonce: str

    def aggregate(self) -> str:
        return aggregate_digest(
            {
                "intent_id": self.intent_id,
                "plan_version_id": self.plan_version_id,
                "workcell_version_id": self.workcell_version_id,
                "workpiece_instance_ids": list(self.workpiece_instance_ids),
                "input_state_digest": self.input_state_digest,
                "machine_instance_ids": list(self.machine_instance_ids),
                "controller_instance_ids": list(self.controller_instance_ids),
                "tool_instance_ids": list(self.tool_instance_ids),
                "fixture_instance_ids": list(self.fixture_instance_ids),
                "coordinate_frame_graph_digest": self.coordinate_frame_graph_digest,
                "process_recipe_version_id": self.process_recipe_version_id,
                "process_parameters": dict(self.process_parameters),
                "machine_program_digest": self.machine_program_digest,
                "validated_operating_envelope_id": self.validated_operating_envelope_id,
                "local_safety_configuration_digest": (
                    self.local_safety_configuration_digest
                ),
                "expected_state_delta": dict(self.expected_state_delta),
                "approval_bundle_id": self.approval_bundle_id,
                "execution_window_start": self.execution_window_start,
                "execution_window_end": self.execution_window_end,
                "nonce": self.nonce,
            }
        )


class ActuationCapability(BaseModel):
    """A declared physical effect with its hazards and verification needs."""

    model_config = ConfigDict(frozen=True)

    capability_id: str
    family: CapabilityFamily
    accepted_workpiece_states: dict[str, str | int | bool]
    produced_state_delta_schema: str
    known_hazards: tuple[str, ...]
    reversibility: Reversibility
    verification_requirement_ids: tuple[str, ...]
    validated_operating_envelope_ids: tuple[str, ...] = ()


class RecipeVersion(BaseModel):
    """A versioned parameter set for a capability inside one envelope."""

    model_config = ConfigDict(frozen=True)

    recipe_version_id: str
    capability_id: str
    envelope_id: str
    parameters: dict[str, int]
    parent_version_id: str | None = None
    status: str = "draft"  # draft | approved | superseded
    approval_required: bool = True


class EnvelopeViolation(ValueError):
    """A parameter proposal fell outside its validated envelope."""

    def __init__(self, violations: list[str]) -> None:
        super().__init__("; ".join(violations))
        self.violations = violations


class ParameterProposal(BaseModel):
    """A learned parameter change, always envelope-checked at birth.

    ``apply`` never mutates a recipe and never yields an approved one:
    an in-envelope proposal becomes a *draft* child version that still
    owes its approval gates (acceptance test 33); an out-of-envelope
    proposal raises (acceptance test 32).
    """

    model_config = ConfigDict(frozen=True)

    proposal_id: str
    recipe_version_id: str
    parameters: dict[str, int]
    rationale_evidence_refs: tuple[str, ...] = ()

    def apply(
        self,
        recipe: RecipeVersion,
        envelope: ValidatedOperatingEnvelope,
        *,
        new_version_id: str,
    ) -> RecipeVersion:
        if recipe.recipe_version_id != self.recipe_version_id:
            raise EnvelopeViolation(
                [f"proposal targets {self.recipe_version_id!r}, not this recipe"]
            )
        if recipe.envelope_id != envelope.envelope_id:
            raise EnvelopeViolation(
                [f"recipe is validated against envelope {recipe.envelope_id!r}"]
            )
        problems = envelope.violations(self.parameters)
        if problems:
            raise EnvelopeViolation(problems)
        return RecipeVersion(
            recipe_version_id=new_version_id,
            capability_id=recipe.capability_id,
            envelope_id=recipe.envelope_id,
            parameters=dict(self.parameters),
            parent_version_id=recipe.recipe_version_id,
            status="draft",
            approval_required=True,
        )


class ExecutionEvidenceBundle(BaseModel):
    """The §24.10 record of what was commanded, what happened, and the
    disposition — including the negative outcomes that must remain
    searchable."""

    model_config = ConfigDict(frozen=True)

    job_digest: str
    program_digest: str
    started_at: str
    ended_at: str
    commanded_trace_ref: str
    actual_trace_ref: str
    interlock_and_alarm_events: tuple[dict[str, str | int | bool], ...] = ()
    operator_intervention_events: tuple[dict[str, str | int | bool], ...] = ()
    deviation_events: tuple[dict[str, str | int | bool], ...] = ()
    pre_workpiece_state_id: str = ""
    post_workpiece_state_id: str = ""
    inspection_observation_ids: tuple[str, ...] = ()
    disposition: Disposition = Disposition.CONTAINED
    edge_signature: str = ""
