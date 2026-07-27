"""R3 exit gates for simulation-only routing.

Each test names the Phase 6 exit gate or acceptance test it realizes
(docs/robotic-actuation-protocol-plan.md, stage R3).
"""

from __future__ import annotations

import pytest

from oolu.actuation import (
    ActionIntent,
    CapabilityFamily,
    RecipeVersion,
    ReviewSignals,
    ValidatedOperatingEnvelope,
    assign_tier,
)
from oolu.actuation.contracts import ActuationCapability, ParameterBound, Reversibility
from oolu.actuation_routing import (
    ActuationApprovalService,
    ActuationNode,
    RawCommandRejected,
    build_plan,
    qualify_simulation,
    ready_for_compilation,
    route,
)
from oolu.actuation_routing.planner import PlanAssemblyError
from oolu.actuation_routing.simulation import (
    SimulationCheck,
    SimulationRecord,
    SimulatorQualification,
)
from oolu.durable import DurableConnection
from oolu.registries import WorkcellRegistry

NOW = 1_000


def make_intent(**overrides) -> ActionIntent:
    fields = dict(
        intent_id="intent-1",
        goal_id="goal-1",
        requested_by="engineer-quinn",
        desired_state_predicate={"clamped": True, "location": "dyno-fixture"},
        input_workpiece_state_id="wps-0",
        allowed_capability_families=(CapabilityFamily.MANIPULATION,),
        quality_acceptance_criteria={"tolerance_um": 100},
    )
    fields.update(overrides)
    return ActionIntent(**fields)


def capability(**overrides) -> ActuationCapability:
    fields = dict(
        capability_id="cap-load",
        family=CapabilityFamily.MANIPULATION,
        accepted_workpiece_states={"kind": "damper"},
        produced_state_delta_schema="schema://state-delta/clamp/1",
        known_hazards=("pinch",),
        reversibility=Reversibility.REVERSIBLE,
        verification_requirement_ids=("verify-clamp",),
        validated_operating_envelope_ids=("env-2",),
    )
    fields.update(overrides)
    return ActuationCapability(**fields)


def make_node(**overrides) -> ActuationNode:
    fields = dict(
        node_version_id="node-robot-1",
        capability=capability(),
        workcell_version_id="cell-7",
        machine_instance_ids=("robot-1",),
        facility_id="plant-1",
        producible_properties=("clamped", "location"),
        accepted_input_properties={"kind": "damper"},
        disposition_paths=("rework", "quarantine"),
        safety_case_current=True,
        achievable_tolerance_um=20,
        workspace_and_payload_margin=0.5,
        verification_coverage=0.8,
        availability=0.9,
        historical_yield=0.95,
        process_drift_risk=0.1,
        cost=0.3,
        delay=0.1,
    )
    fields.update(overrides)
    return ActuationNode(**fields)


INPUT_STATE = {"kind": "damper"}


# --- the raw-command wall ----------------------------------------------


def test_raw_servo_vocabulary_is_rejected_at_the_boundary() -> None:
    # Acceptance test 19.
    intent = make_intent(
        desired_state_predicate={
            "joint_1_target": 90,
            "gcode": "G1 X10",
            "velocity": 200,
            "clamped": True,
        }
    )
    with pytest.raises(RawCommandRejected) as excinfo:
        route(intent, (make_node(),), input_state=INPUT_STATE)
    assert set(excinfo.value.keys) == {"joint_1_target", "gcode", "velocity"}


# --- routing -----------------------------------------------------------


def test_goal_routes_to_compatible_nodes_without_any_command() -> None:
    # Phase 6 gates 1 and 2: a robot cell and a CNC-style cell are the
    # same contract; the decision is ranked data with named exclusions.
    robot = make_node()
    press = make_node(
        node_version_id="node-press-9",
        capability=capability(
            capability_id="cap-press",
            family=CapabilityFamily.FORMING,
            reversibility=Reversibility.IRREVERSIBLE,
        ),
        workcell_version_id="cell-9",
        cost=0.1,
    )
    decision = route(
        make_intent(
            allowed_capability_families=(
                CapabilityFamily.MANIPULATION,
                CapabilityFamily.FORMING,
            )
        ),
        (robot, press),
        input_state=INPUT_STATE,
    )
    assert [c.node_version_id for c in decision.candidates] == [
        "node-robot-1",
        "node-press-9",
    ]
    # The irreversible press pays its named penalty in the breakdown.
    press_score = decision.candidates[1].breakdown
    assert press_score.irreversibility_risk == 1.0
    # Nothing in the decision is executable: it is ids, scores, reasons.
    assert set(decision.model_dump()) == {"intent_id", "candidates", "excluded"}


def test_exclusions_are_named_not_silent() -> None:
    wrong_family = make_node(
        node_version_id="node-welder",
        capability=capability(
            capability_id="cap-weld", family=CapabilityFamily.JOINING
        ),
    )
    stale_safety = make_node(node_version_id="node-stale", safety_case_current=False)
    cannot_reach = make_node(
        node_version_id="node-mover", producible_properties=("location",)
    )
    bare_irreversible = make_node(
        node_version_id="node-laser",
        capability=capability(reversibility=Reversibility.IRREVERSIBLE),
        disposition_paths=(),
    )
    decision = route(
        make_intent(),
        (wrong_family, stale_safety, cannot_reach, bare_irreversible),
        input_state=INPUT_STATE,
    )
    assert decision.candidates == ()
    reasons = {e.node_version_id: e.reasons for e in decision.excluded}
    assert "capability_family_not_allowed:joining" in reasons["node-welder"]
    assert "safety_case_not_current" in reasons["node-stale"]
    assert "desired_state_unreachable:clamped" in reasons["node-mover"]
    assert "irreversible_without_disposition_path" in reasons["node-laser"]


def test_workcell_qualification_seam_excludes_by_name(tmp_path) -> None:
    conn = DurableConnection(tmp_path / "cells.db")
    cells = WorkcellRegistry(conn)
    cells.register_workcell(
        workcell_version_id="cell-7",
        facility_id="plant-1",
        machine_instance_ids=("robot-1",),
        controller_instance_ids=("ctrl-1",),
        safety_controller_instance_ids=("safety-1",),
        safety_configuration_digest="d" * 64,
        risk_assessment_id="risk-3",
        now=NOW,
    )  # left in draft — never qualified

    def qualified(cell_id: str) -> bool:
        row = cells.workcell(cell_id)
        return row is not None and row.commissioning_status == "qualified"

    decision = route(
        make_intent(),
        (make_node(),),
        input_state=INPUT_STATE,
        workcell_qualified=qualified,
    )
    assert decision.excluded[0].reasons == ("workcell_not_qualified:cell-7",)
    conn.close()


def test_score_prefers_capable_cheap_and_reversible() -> None:
    tight = make_node(node_version_id="node-tight", achievable_tolerance_um=10)
    loose = make_node(node_version_id="node-loose", achievable_tolerance_um=90)
    decision = route(make_intent(), (loose, tight), input_state=INPUT_STATE)
    assert decision.candidates[0].node_version_id == "node-tight"
    breakdown = decision.candidates[0].breakdown
    assert breakdown.tolerance_and_process_capability == pytest.approx(0.9)


# --- simulation qualification ------------------------------------------


def simulator(**overrides) -> SimulatorQualification:
    fields = dict(
        simulator_id="sim-geom",
        simulator_version="3.1",
        validated_check_families=("reachability", "collision", "state_delta"),
        validation_evidence_refs=("val-1",),
    )
    fields.update(overrides)
    return SimulatorQualification(**fields)


def simulation(**overrides) -> SimulationRecord:
    fields = dict(
        simulation_id="sim-run-1",
        simulator_id="sim-geom",
        simulator_version="3.1",
        plan_digest="p" * 64,
        workcell_version_id="cell-7",
        tool_instance_ids=("gripper-40mm",),
        fixture_instance_ids=("fixture-3",),
        coordinate_frame_graph_digest="f" * 64,
        recipe_version_id="recipe-5",
        process_parameters={"clamp_force_mn": 12_000},
        envelope_id="env-2",
        checks=(
            SimulationCheck(family="reachability", passed=True),
            SimulationCheck(family="collision", passed=True),
        ),
    )
    fields.update(overrides)
    return SimulationRecord(**fields)


def test_simulation_must_name_exact_versions() -> None:
    # Phase 6 gate 3.
    incomplete = simulation(tool_instance_ids=(), recipe_version_id="", envelope_id="")
    failures = qualify_simulation(incomplete, simulator())
    assert "missing_binding:tool_instance_ids" in failures
    assert "missing_binding:recipe_version_id" in failures
    assert "missing_binding:envelope_id" in failures


def test_unqualified_simulator_cannot_support_review() -> None:
    # Plan review gap 2.2.3: the simulator is equipment.
    narrow = simulator(validated_check_families=("reachability",))
    failures = qualify_simulation(simulation(), narrow)
    assert "simulator_not_qualified_for:collision" in failures
    revoked = simulator(status="revoked")
    assert "simulator_revoked" in qualify_simulation(simulation(), revoked)
    assert "no_checks_run" in qualify_simulation(simulation(checks=()), simulator())


# --- plan assembly -----------------------------------------------------


def envelope() -> ValidatedOperatingEnvelope:
    return ValidatedOperatingEnvelope(
        envelope_id="env-2",
        workcell_version_id="cell-7",
        bounds={
            "clamp_force_mn": ParameterBound(unit="mN", minimum=8_000, maximum=15_000)
        },
    )


def recipe(**overrides) -> RecipeVersion:
    fields = dict(
        recipe_version_id="recipe-5",
        capability_id="cap-load",
        envelope_id="env-2",
        parameters={"clamp_force_mn": 12_000},
        status="approved",
        approval_required=False,
    )
    fields.update(overrides)
    return RecipeVersion(**fields)


def qualified_cell(tmp_path):
    conn = DurableConnection(tmp_path / "plan-cells.db")
    cells = WorkcellRegistry(conn)
    cells.register_workcell(
        workcell_version_id="cell-7",
        facility_id="plant-1",
        machine_instance_ids=("robot-1",),
        controller_instance_ids=("ctrl-1",),
        safety_controller_instance_ids=("safety-1",),
        safety_configuration_digest="d" * 64,
        risk_assessment_id="risk-3",
        now=NOW,
    )
    cells.set_commissioning_status("cell-7", "commissioning")
    cells.set_commissioning_status("cell-7", "qualified")
    cells.calibrate_frame(
        workcell_version_id="cell-7",
        frame_id="base",
        calibration_digest="2" * 64,
        now=NOW,
    )
    return conn, cells


def plan_for(cells) -> "object":
    return build_plan(
        make_intent(),
        make_node(),
        plan_id="plan-1",
        workcell=cells.workcell("cell-7"),
        frame_graph_digest=cells.frame_graph_digest("cell-7"),
        tool_instance_ids=("gripper-40mm",),
        fixture_instance_ids=("fixture-3",),
        recipe=recipe(),
        envelope=envelope(),
        expected_state_delta={"clamped": True},
    )


def test_plan_assembly_refuses_by_name(tmp_path) -> None:
    conn, cells = qualified_cell(tmp_path)
    hot = recipe(parameters={"clamp_force_mn": 20_000})
    with pytest.raises(PlanAssemblyError) as excinfo:
        build_plan(
            make_intent(),
            make_node(),
            plan_id="plan-1",
            workcell=cells.workcell("cell-7"),
            frame_graph_digest=cells.frame_graph_digest("cell-7"),
            tool_instance_ids=("gripper-40mm",),
            fixture_instance_ids=("fixture-3",),
            recipe=hot,
            envelope=envelope(),
            expected_state_delta={"clamped": True},
        )
    assert any("clamp_force_mn" in r for r in excinfo.value.reasons)
    conn.close()


def test_plan_digest_binds_every_choice(tmp_path) -> None:
    conn, cells = qualified_cell(tmp_path)
    base = plan_for(cells)
    rebound = base.model_copy(update={"tool_instance_ids": ("gripper-60mm",)})
    assert base.digest() != rebound.digest()
    conn.close()


# --- approvals (acceptance test 18) ------------------------------------


def a4_review():
    return assign_tier(
        ReviewSignals(
            physical_execution_requested=True,
            within_certified_cell=True,
            within_validated_envelope=True,
            human_robot_shared_space=True,
        )
    )


def test_high_consequence_waits_for_dual_control(tmp_path) -> None:
    conn = DurableConnection(tmp_path / "approvals.db")
    service = ActuationApprovalService(conn)
    digest = "a" * 64
    service.request(digest, a4_review(), now=NOW, ttl_seconds=600)
    service.approve(digest, role="process_owner", approver="pat", now=NOW + 1)
    waiting = service.status(digest, now=NOW + 2)
    assert not waiting.ready
    assert "safety_or_quality_owner" in waiting.missing_roles
    # One person signing both roles is one signature, not dual control.
    service.approve(digest, role="safety_or_quality_owner", approver="pat", now=NOW + 3)
    still = service.status(digest, now=NOW + 4)
    assert not still.ready
    assert "dual_control_requires_two_distinct_approvers" in still.refusals
    service.approve(digest, role="safety_or_quality_owner", approver="sam", now=NOW + 5)
    assert service.status(digest, now=NOW + 6).ready
    assert service.consume(digest, now=NOW + 7) == ()
    assert service.consume(digest, now=NOW + 8) == ("bundle_already_consumed",)
    conn.close()


def test_approvals_bind_digests_and_expire(tmp_path) -> None:
    conn = DurableConnection(tmp_path / "approvals2.db")
    service = ActuationApprovalService(conn)
    service.request("a" * 64, a4_review(), now=NOW, ttl_seconds=10)
    # Signing digest B never helps digest A.
    with pytest.raises(KeyError, match="no_approval_bundle_for_digest"):
        service.approve("b" * 64, role="process_owner", approver="pat", now=NOW)
    service.approve("a" * 64, role="process_owner", approver="pat", now=NOW + 1)
    service.approve(
        "a" * 64, role="safety_or_quality_owner", approver="sam", now=NOW + 2
    )
    expired = service.status("a" * 64, now=NOW + 30)
    assert not expired.ready and "approval_expired" in expired.refusals
    conn.close()


def test_prohibited_tiers_cannot_open_a_bundle(tmp_path) -> None:
    conn = DurableConnection(tmp_path / "approvals3.db")
    service = ActuationApprovalService(conn)
    a5 = assign_tier(
        ReviewSignals(
            physical_execution_requested=True,
            within_certified_cell=True,
            within_validated_envelope=False,
        )
    )
    with pytest.raises(PermissionError, match="physical_execution_prohibited:A5"):
        service.request("c" * 64, a5, now=NOW, ttl_seconds=600)
    conn.close()


# --- the release gate, end to end --------------------------------------


def a1_review():
    return assign_tier(
        ReviewSignals(
            physical_execution_requested=True,
            within_certified_cell=True,
            within_validated_envelope=True,
        )
    )


def test_release_gate_end_to_end(tmp_path) -> None:
    conn, cells = qualified_cell(tmp_path)
    service = ActuationApprovalService(conn)
    plan = plan_for(cells)
    digest = plan.digest()
    review = a1_review()
    service.request(digest, review, now=NOW, ttl_seconds=600)
    service.approve(
        digest, role="qualified_operator", approver="operator-lee", now=NOW + 1
    )
    good = simulation(
        plan_digest=digest,
        coordinate_frame_graph_digest=plan.coordinate_frame_graph_digest,
    )
    verdict = ready_for_compilation(
        plan,
        simulation=good,
        simulator=simulator(),
        review=review,
        approvals=service,
        now=NOW + 2,
    )
    assert verdict.eligible_for_compilation
    # The bundle spent: a second release for the same digest refuses.
    again = ready_for_compilation(
        plan,
        simulation=good,
        simulator=simulator(),
        review=review,
        approvals=service,
        now=NOW + 3,
    )
    assert not again.eligible_for_compilation
    assert "bundle_already_consumed" in again.refusals
    conn.close()


def test_release_refuses_failed_or_foreign_simulation(tmp_path) -> None:
    conn, cells = qualified_cell(tmp_path)
    service = ActuationApprovalService(conn)
    plan = plan_for(cells)
    digest = plan.digest()
    review = a1_review()
    service.request(digest, review, now=NOW, ttl_seconds=600)
    service.approve(
        digest, role="qualified_operator", approver="operator-lee", now=NOW + 1
    )
    collided = simulation(
        plan_digest=digest,
        coordinate_frame_graph_digest=plan.coordinate_frame_graph_digest,
        checks=(
            SimulationCheck(
                family="collision", passed=False, findings=("gripper vs fixture",)
            ),
        ),
    )
    verdict = ready_for_compilation(
        plan,
        simulation=collided,
        simulator=simulator(),
        review=review,
        approvals=service,
        now=NOW + 2,
    )
    assert not verdict.eligible_for_compilation
    assert "simulation_check_failed:collision" in verdict.refusals
    foreign = simulation(plan_digest="0" * 64)
    verdict = ready_for_compilation(
        plan,
        simulation=foreign,
        simulator=simulator(),
        review=review,
        approvals=service,
        now=NOW + 3,
    )
    assert "simulation_for_different_plan" in verdict.refusals
    # Refused releases never spent the bundle.
    assert service.status(digest, now=NOW + 4).ready
    conn.close()
