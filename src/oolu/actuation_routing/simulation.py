"""Simulation records and simulator qualification.

Two of the plan's review gaps close here:

* **A simulator is equipment** (plan §2.2 gap 3): it has a version, a
  validated scope (which check families it is qualified to run), its
  own validation evidence, and a revocation status. A collision check
  from a simulator qualified only for reachability is refused by name
  — simulation success from an unqualified simulator satisfies
  nothing.
* **A simulation names exactly what it simulated** (Phase 6's third
  exit gate): the record binds workcell version, tools, fixtures, the
  frame-graph digest, recipe version, parameters, and envelope. A
  record with a binding missing reports it by name and cannot support
  a release.

Per invariant 19, a passing simulation is *supporting evidence* for
review — never a substitute for the local safety functions validated in
the physical cell.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SimulatorQualification(BaseModel):
    model_config = ConfigDict(frozen=True)

    simulator_id: str
    simulator_version: str
    validated_check_families: tuple[str, ...]
    validation_evidence_refs: tuple[str, ...]
    known_divergences: tuple[str, ...] = ()
    status: str = "qualified"  # qualified | expired | revoked


class SimulationCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    family: str  # reachability | collision | load | timing | state_delta | process
    passed: bool
    findings: tuple[str, ...] = ()


class SimulationRecord(BaseModel):
    """One simulation run over one exact configuration."""

    model_config = ConfigDict(frozen=True)

    simulation_id: str
    simulator_id: str
    simulator_version: str
    plan_digest: str
    workcell_version_id: str
    tool_instance_ids: tuple[str, ...]
    fixture_instance_ids: tuple[str, ...]
    coordinate_frame_graph_digest: str
    recipe_version_id: str
    process_parameters: dict[str, int] = Field(default_factory=dict)
    envelope_id: str = ""
    checks: tuple[SimulationCheck, ...] = ()

    def missing_bindings(self) -> tuple[str, ...]:
        """Named configuration bindings this record failed to identify."""

        missing: list[str] = []
        if not self.plan_digest:
            missing.append("plan_digest")
        if not self.workcell_version_id:
            missing.append("workcell_version_id")
        if not self.tool_instance_ids:
            missing.append("tool_instance_ids")
        if not self.coordinate_frame_graph_digest:
            missing.append("coordinate_frame_graph_digest")
        if not self.recipe_version_id:
            missing.append("recipe_version_id")
        if not self.envelope_id:
            missing.append("envelope_id")
        return tuple(missing)

    @property
    def all_checks_passed(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)


def qualify_simulation(
    record: SimulationRecord, qualification: SimulatorQualification
) -> tuple[str, ...]:
    """Named reasons this record cannot support a review decision."""

    failures: list[str] = []
    if (
        record.simulator_id != qualification.simulator_id
        or record.simulator_version != qualification.simulator_version
    ):
        failures.append("simulator_qualification_mismatch")
    if qualification.status != "qualified":
        failures.append(f"simulator_{qualification.status}")
    for check in record.checks:
        if check.family not in qualification.validated_check_families:
            failures.append(f"simulator_not_qualified_for:{check.family}")
    failures.extend(
        f"missing_binding:{binding}" for binding in record.missing_bindings()
    )
    if not record.checks:
        failures.append("no_checks_run")
    return tuple(failures)
