"""The actuation-job binder — an R3 release becomes an R0 job.

One function with one rule: every field of the ``ActuationJob`` that
the aggregate digest binds comes from the released plan and the
compiled program, never from the caller's imagination. The caller adds
only what the plan cannot know — the concrete workpieces on the floor,
the execution window, the nonce, and the workcell's controller and
safety identities from the registry.
"""

from __future__ import annotations

from ..actuation.contracts import ActuationJob
from ..actuation_routing.planner import ActuationPlan
from ..registries.workcells import WorkcellRow
from .compiler import MachineProgram


class JobBindingError(ValueError):
    def __init__(self, reasons: tuple[str, ...]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def bind_actuation_job(
    plan: ActuationPlan,
    program: MachineProgram,
    workcell: WorkcellRow,
    *,
    job_id: str,
    workpiece_instance_ids: tuple[str, ...],
    input_state_digest: str,
    approval_bundle_id: str,
    execution_window_start: str,
    execution_window_end: str,
    nonce: str,
) -> ActuationJob:
    reasons: list[str] = []
    if program.plan_digest != plan.digest():
        reasons.append("program_compiled_for_different_plan")
    if workcell.workcell_version_id != plan.workcell_version_id:
        reasons.append("workcell_mismatch_with_plan")
    if workcell.commissioning_status != "qualified":
        reasons.append(f"workcell_not_qualified:{workcell.commissioning_status}")
    if reasons:
        raise JobBindingError(tuple(reasons))
    return ActuationJob(
        job_id=job_id,
        intent_id=plan.intent_id,
        plan_version_id=plan.digest(),
        workcell_version_id=plan.workcell_version_id,
        workpiece_instance_ids=workpiece_instance_ids,
        input_state_digest=input_state_digest,
        machine_instance_ids=workcell.machine_instance_ids,
        controller_instance_ids=workcell.controller_instance_ids,
        tool_instance_ids=plan.tool_instance_ids,
        fixture_instance_ids=plan.fixture_instance_ids,
        coordinate_frame_graph_digest=plan.coordinate_frame_graph_digest,
        process_recipe_version_id=plan.recipe_version_id,
        process_parameters=dict(plan.process_parameters),
        machine_program_digest=program.digest,
        validated_operating_envelope_id=plan.envelope_id,
        local_safety_configuration_digest=workcell.safety_configuration_digest,
        expected_state_delta=dict(plan.expected_state_delta),
        approval_bundle_id=approval_bundle_id,
        execution_window_start=execution_window_start,
        execution_window_end=execution_window_end,
        nonce=nonce,
    )
