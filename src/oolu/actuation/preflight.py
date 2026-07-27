"""Live preflight — the §24.7 checks as a typed verdict.

Preflight compares the job's *approved bindings* against the *live
identities and states* of the cell at arming time. Any failure blocks
arming with a named reason; there is no severity ladder and no
override parameter, because §24.4 admits none: a mismatch in
workpiece, tool, fixture, frame, program, safety configuration, or
safety state means the approved job and physical reality disagree.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .contracts import ActuationJob


class LiveWorkcellState(BaseModel):
    """What the edge observes at the cell, immediately before arming."""

    model_config = ConfigDict(frozen=True)

    workcell_version_id: str
    machine_instance_ids: tuple[str, ...]
    controller_instance_ids: tuple[str, ...]
    tool_instance_ids: tuple[str, ...]
    fixture_instance_ids: tuple[str, ...]
    workpiece_instance_ids: tuple[str, ...]
    coordinate_frame_graph_digest: str
    machine_program_digest: str
    local_safety_configuration_digest: str
    guards_closed: bool
    emergency_stop_clear: bool
    safety_controller_healthy: bool
    energy_state_nominal: bool
    clock_within_tolerance: bool


class PreflightResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures


def run_preflight(job: ActuationJob, live: LiveWorkcellState) -> PreflightResult:
    failures: list[str] = []

    def check(name: str, ok: bool) -> None:
        if not ok:
            failures.append(name)

    check("workcell_version", live.workcell_version_id == job.workcell_version_id)
    check(
        "machine_identity",
        set(job.machine_instance_ids) <= set(live.machine_instance_ids),
    )
    check(
        "controller_identity",
        set(job.controller_instance_ids) <= set(live.controller_instance_ids),
    )
    check(
        "tool_identity",
        set(job.tool_instance_ids) == set(live.tool_instance_ids),
    )
    check(
        "fixture_identity",
        set(job.fixture_instance_ids) == set(live.fixture_instance_ids),
    )
    check(
        "workpiece_identity",
        set(job.workpiece_instance_ids) == set(live.workpiece_instance_ids),
    )
    check(
        "coordinate_frame_calibration",
        live.coordinate_frame_graph_digest == job.coordinate_frame_graph_digest,
    )
    check(
        "controller_program_digest",
        live.machine_program_digest == job.machine_program_digest,
    )
    check(
        "safety_configuration",
        live.local_safety_configuration_digest == job.local_safety_configuration_digest,
    )
    check("guards_closed", live.guards_closed)
    check("emergency_stop_clear", live.emergency_stop_clear)
    check("safety_controller_health", live.safety_controller_healthy)
    check("energy_state", live.energy_state_nominal)
    check("local_clock", live.clock_within_tolerance)

    return PreflightResult(failures=tuple(failures))
