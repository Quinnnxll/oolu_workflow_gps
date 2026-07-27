"""R4 exit gates for the edge, compiler, and qualified-workcell stage.

Each test names the Phase 7 exit gate or acceptance test it realizes
(docs/robotic-actuation-protocol-plan.md, stage R4). The pilot shapes
are §32's damper loop.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from oolu.actuation import (
    ActionIntent,
    CapabilityFamily,
    Disposition,
    RecipeVersion,
    ReviewSignals,
    ValidatedOperatingEnvelope,
    assign_tier,
    run_preflight,
)
from oolu.actuation.contracts import ActuationCapability, ParameterBound, Reversibility
from oolu.actuation.digests import canonical_json
from oolu.actuation.lease import LeaseRefusal
from oolu.actuation.lifecycle import ActuationState
from oolu.actuation_edge import (
    ActuationGateway,
    BatchGate,
    ChannelIdentities,
    CompileRefused,
    DeviationMonitor,
    DurableLeaseService,
    ExecutionMode,
    IROp,
    IRStep,
    LocalExecutor,
    MachineProgram,
    PilotDialect,
    ProgramIR,
    SafetyControllerSim,
    bind_actuation_job,
    compile_program,
)
from oolu.actuation_edge.execution import CloudLink, ExecutionRefused
from oolu.actuation_edge.jobs import JobBindingError
from oolu.actuation_edge.program_ir import RawParameterRejected
from oolu.actuation_routing import (
    ActuationApprovalService,
    ActuationNode,
    build_plan,
    ready_for_compilation,
)
from oolu.actuation_routing.planner import ActuationPlan, ReleaseVerdict
from oolu.actuation_routing.simulation import (
    SimulationCheck,
    SimulationRecord,
    SimulatorQualification,
)
from oolu.durable import DurableConnection
from oolu.evidence import HmacDeviceKeys
from oolu.registries import WorkcellRegistry

NOW = 1_000
GOLDEN = Path(__file__).parent / "goldens" / "pilot_damper_load.txt"


def pilot_plan() -> ActuationPlan:
    """The §32 pilot plan with literal bindings — the golden's anchor."""

    return ActuationPlan(
        plan_id="plan-pilot-1",
        intent_id="intent-pilot-1",
        node_version_id="node-robot-1",
        workcell_version_id="cell-7",
        tool_instance_ids=("gripper-40mm",),
        fixture_instance_ids=("fixture-3",),
        coordinate_frame_graph_digest="f" * 64,
        recipe_version_id="recipe-5",
        process_parameters={"clamp_force_mn": 12_000, "profile_cycles": 20},
        envelope_id="env-2",
        expected_state_delta={"tested": True},
        disposition_paths=("rework", "quarantine"),
    )


def pilot_ir() -> ProgramIR:
    return ProgramIR(
        ir_id="ir-pilot-1",
        steps=(
            IRStep(op=IROp.VERIFY_IDENTITY, parameters={"workpiece": "damper-42"}),
            IRStep(op=IROp.MOUNT_TOOL, parameters={"tool": "gripper-40mm"}),
            IRStep(op=IROp.ACQUIRE_WORKPIECE, parameters={"workpiece": "damper-42"}),
            IRStep(op=IROp.PLACE_IN_FIXTURE, parameters={"fixture": "fixture-3"}),
            IRStep(op=IROp.CLAMP, parameters={"clamp_force_mn": 12_000}),
            IRStep(
                op=IROp.EXECUTE_PROFILE,
                parameters={"profile": "sine-2hz", "profile_cycles": 20},
            ),
            IRStep(op=IROp.UNCLAMP, parameters={}),
            IRStep(op=IROp.TRANSFER_TO_INSPECTION, parameters={"station": "cmm-1"}),
            IRStep(op=IROp.PARK, parameters={}),
        ),
    )


def compiler_keys() -> HmacDeviceKeys:
    keys = HmacDeviceKeys()
    keys.register_key("compiler-1", b"compiler-signing-key")
    keys.register_key("edge-command-1", b"command-key")
    keys.register_key("edge-evidence-1", b"evidence-key")
    return keys


def eligible_release(plan: ActuationPlan) -> ReleaseVerdict:
    return ReleaseVerdict(plan_digest=plan.digest(), eligible_for_compilation=True)


def compiled(keys: HmacDeviceKeys | None = None) -> MachineProgram:
    plan = pilot_plan()
    return compile_program(
        eligible_release(plan),
        plan,
        pilot_ir(),
        program_id="prog-1",
        dialect=PilotDialect(),
        signer=keys or compiler_keys(),
        compiler_identity="compiler-1",
    )


# --- the compiler ------------------------------------------------------


def test_compiler_is_deterministic_and_matches_the_golden() -> None:
    # Gap 2.2.4: byte determinism plus the golden regression suite.
    first, second = compiled(), compiled()
    assert first.text == second.text
    assert first.digest == second.digest
    assert first.text == GOLDEN.read_text()


def test_compiler_cannot_be_bypassed() -> None:
    # Phase 7 gate 1: no approval, no program.
    plan = pilot_plan()
    ineligible = ReleaseVerdict(
        plan_digest=plan.digest(),
        eligible_for_compilation=False,
        refusals=("missing_approval_role:qualified_operator",),
    )
    with pytest.raises(CompileRefused) as excinfo:
        compile_program(
            ineligible,
            plan,
            pilot_ir(),
            program_id="prog-x",
            dialect=PilotDialect(),
            signer=compiler_keys(),
            compiler_identity="compiler-1",
        )
    assert "release_not_eligible" in excinfo.value.reasons
    foreign = ReleaseVerdict(plan_digest="0" * 64, eligible_for_compilation=True)
    with pytest.raises(CompileRefused) as excinfo:
        compile_program(
            foreign,
            plan,
            pilot_ir(),
            program_id="prog-x",
            dialect=PilotDialect(),
            signer=compiler_keys(),
            compiler_identity="compiler-1",
        )
    assert "release_for_different_plan" in excinfo.value.reasons


def test_raw_vocabulary_cannot_enter_the_ir() -> None:
    # Acceptance test 19, compiler layer: servo words die at IR birth.
    with pytest.raises(RawParameterRejected):
        IRStep(op=IROp.EXECUTE_PROFILE, parameters={"gcode": "G1 X10"})
    with pytest.raises(RawParameterRejected):
        IRStep(op=IROp.CLAMP, parameters={"joint_3_target": 90})


# --- artifact verification at the gateway ------------------------------


def make_gateway(tmp_path, keys: HmacDeviceKeys) -> ActuationGateway:
    conn = DurableConnection(tmp_path / "leases.db")
    return ActuationGateway(
        identities=ChannelIdentities(
            command_identity="edge-command-1", evidence_identity="edge-evidence-1"
        ),
        keys=keys,
        leases=DurableLeaseService(conn),
    )


def qualified_cell(tmp_path):
    conn = DurableConnection(tmp_path / "cells.db")
    cells = WorkcellRegistry(conn)
    cells.register_workcell(
        workcell_version_id="cell-7",
        facility_id="plant-1",
        machine_instance_ids=("robot-1", "dyno-1"),
        controller_instance_ids=("ctrl-1",),
        safety_controller_instance_ids=("safety-1",),
        safety_configuration_digest="d" * 64,
        risk_assessment_id="risk-3",
        now=NOW,
    )
    cells.set_commissioning_status("cell-7", "commissioning")
    cells.set_commissioning_status("cell-7", "qualified")
    return conn, cells


def bound_job(cells, program: MachineProgram):
    plan = pilot_plan()
    return bind_actuation_job(
        plan,
        program,
        cells.workcell("cell-7"),
        job_id="job-1",
        workpiece_instance_ids=("damper-42",),
        input_state_digest="a" * 64,
        approval_bundle_id="approvals-9",
        execution_window_start="2026-07-27T10:00:00Z",
        execution_window_end="2026-07-27T11:00:00Z",
        nonce="nonce-1",
    )


def test_modified_program_fails_the_signed_digest_check(tmp_path) -> None:
    # Acceptance test 21.
    keys = compiler_keys()
    gateway = make_gateway(tmp_path, keys)
    conn, cells = qualified_cell(tmp_path)
    program = compiled(keys)
    job = bound_job(cells, program)
    tampered = program.model_copy(
        update={
            "text": program.text.replace("clamp_force_mn=12000", "clamp_force_mn=19000")
        }
    )
    assert "program_text_digest_mismatch" in gateway.verify_artifact(tampered, job)
    forged = program.model_copy(update={"signature": "0" * 64})
    assert "program_signature_invalid" in gateway.verify_artifact(forged, job)
    other = job.model_copy(update={"machine_program_digest": "9" * 64})
    assert "program_digest_not_bound_by_job" in gateway.verify_artifact(program, other)
    assert gateway.verify_artifact(program, job) == ()
    conn.close()


def test_binder_refuses_foreign_programs(tmp_path) -> None:
    conn, cells = qualified_cell(tmp_path)
    program = compiled().model_copy(update={"plan_digest": "0" * 64})
    with pytest.raises(JobBindingError) as excinfo:
        bound_job(cells, program)
    assert "program_compiled_for_different_plan" in excinfo.value.reasons
    conn.close()


# --- durable leases ----------------------------------------------------


def test_spent_lease_stays_spent_across_restart(tmp_path) -> None:
    # Acceptance test 22, with a process restart in the middle.
    conn = DurableConnection(tmp_path / "lease-restart.db")
    service = DurableLeaseService(conn)
    lease = service.issue(job_digest="a" * 64, nonce="n-1", now=NOW, ttl_seconds=60)
    assert (
        service.consume(
            lease_id=lease.lease_id, job_digest="a" * 64, nonce="n-1", now=NOW + 1
        )
        is None
    )
    reborn = DurableLeaseService(conn)  # the process comes back
    assert (
        reborn.consume(
            lease_id=lease.lease_id, job_digest="a" * 64, nonce="n-1", now=NOW + 2
        )
        is LeaseRefusal.ALREADY_CONSUMED
    )
    stale = reborn.issue(job_digest="a" * 64, nonce="n-2", now=NOW, ttl_seconds=10)
    assert (
        reborn.consume(
            lease_id=stale.lease_id, job_digest="a" * 64, nonce="n-2", now=NOW + 60
        )
        is LeaseRefusal.EXPIRED
    )
    conn.close()


# --- the full walk: release → compile → bind → arm ---------------------


def full_walk(tmp_path):
    """Intent → route-shaped plan → release → compile → bind → arm."""

    keys = compiler_keys()
    conn, cells = qualified_cell(tmp_path)
    cells.calibrate_frame(
        workcell_version_id="cell-7",
        frame_id="base",
        calibration_digest="2" * 64,
        now=NOW,
    )
    intent = ActionIntent(
        intent_id="intent-pilot-1",
        goal_id="goal-1",
        requested_by="engineer-quinn",
        desired_state_predicate={"tested": True},
        input_workpiece_state_id="wps-0",
        allowed_capability_families=(CapabilityFamily.TEST_ACTUATION,),
    )
    node = ActuationNode(
        node_version_id="node-robot-1",
        capability=ActuationCapability(
            capability_id="cap-dyno",
            family=CapabilityFamily.TEST_ACTUATION,
            accepted_workpiece_states={"kind": "damper"},
            produced_state_delta_schema="schema://state-delta/test/1",
            known_hazards=("stored_energy",),
            reversibility=Reversibility.REVERSIBLE,
            verification_requirement_ids=("verify-scan",),
            validated_operating_envelope_ids=("env-2",),
        ),
        workcell_version_id="cell-7",
        machine_instance_ids=("robot-1", "dyno-1"),
        facility_id="plant-1",
        producible_properties=("tested",),
        safety_case_current=True,
    )
    envelope = ValidatedOperatingEnvelope(
        envelope_id="env-2",
        workcell_version_id="cell-7",
        bounds={
            "clamp_force_mn": ParameterBound(unit="mN", minimum=8_000, maximum=15_000),
            "profile_cycles": ParameterBound(unit="cycles", minimum=1, maximum=100),
        },
    )
    recipe = RecipeVersion(
        recipe_version_id="recipe-5",
        capability_id="cap-dyno",
        envelope_id="env-2",
        parameters={"clamp_force_mn": 12_000, "profile_cycles": 20},
        status="approved",
        approval_required=False,
    )
    plan = build_plan(
        intent,
        node,
        plan_id="plan-pilot-1",
        workcell=cells.workcell("cell-7"),
        frame_graph_digest=cells.frame_graph_digest("cell-7"),
        tool_instance_ids=("gripper-40mm",),
        fixture_instance_ids=("fixture-3",),
        recipe=recipe,
        envelope=envelope,
        expected_state_delta={"tested": True},
    )
    review = assign_tier(
        ReviewSignals(
            physical_execution_requested=True,
            within_certified_cell=True,
            within_validated_envelope=True,
        )
    )
    approvals = ActuationApprovalService(conn)
    approvals.request(plan.digest(), review, now=NOW, ttl_seconds=600)
    approvals.approve(
        plan.digest(), role="qualified_operator", approver="operator-lee", now=NOW + 1
    )
    release = ready_for_compilation(
        plan,
        simulation=SimulationRecord(
            simulation_id="sim-run-1",
            simulator_id="sim-geom",
            simulator_version="3.1",
            plan_digest=plan.digest(),
            workcell_version_id="cell-7",
            tool_instance_ids=("gripper-40mm",),
            fixture_instance_ids=("fixture-3",),
            coordinate_frame_graph_digest=plan.coordinate_frame_graph_digest,
            recipe_version_id="recipe-5",
            envelope_id="env-2",
            checks=(SimulationCheck(family="reachability", passed=True),),
        ),
        simulator=SimulatorQualification(
            simulator_id="sim-geom",
            simulator_version="3.1",
            validated_check_families=("reachability",),
            validation_evidence_refs=("val-1",),
        ),
        review=review,
        approvals=approvals,
        now=NOW + 2,
    )
    assert release.eligible_for_compilation
    program = compile_program(
        release,
        plan,
        pilot_ir(),
        program_id="prog-1",
        dialect=PilotDialect(),
        signer=keys,
        compiler_identity="compiler-1",
    )
    job = bind_actuation_job(
        plan,
        program,
        cells.workcell("cell-7"),
        job_id="job-1",
        workpiece_instance_ids=("damper-42",),
        input_state_digest="a" * 64,
        approval_bundle_id="approvals-9",
        execution_window_start="2026-07-27T10:00:00Z",
        execution_window_end="2026-07-27T11:00:00Z",
        nonce="nonce-1",
    )
    gateway = ActuationGateway(
        identities=ChannelIdentities(
            command_identity="edge-command-1", evidence_identity="edge-evidence-1"
        ),
        keys=keys,
        leases=DurableLeaseService(conn),
    )
    live = cells.live_state(
        "cell-7",
        tool_instance_ids=job.tool_instance_ids,
        fixture_instance_ids=job.fixture_instance_ids,
        workpiece_instance_ids=job.workpiece_instance_ids,
        machine_program_digest=program.digest,
        guards_closed=True,
        emergency_stop_clear=True,
        safety_controller_healthy=True,
        energy_state_nominal=True,
        clock_within_tolerance=True,
    )
    return conn, cells, keys, gateway, plan, program, job, live


def test_the_pilot_walk_arms_and_wrong_workpiece_blocks(tmp_path) -> None:
    # Phase 7 gate 2 / acceptance test 20.
    conn, cells, keys, gateway, plan, program, job, live = full_walk(tmp_path)
    lease = gateway._leases.issue(
        job_digest=job.aggregate(), nonce=job.nonce, now=NOW + 3, ttl_seconds=60
    )
    decision = gateway.arm(
        job,
        approved_digest=job.aggregate(),
        program=program,
        preflight=run_preflight(job, live),
        lease_id=lease.lease_id,
        now=NOW + 4,
    )
    assert decision.armed
    wrong_piece = live.model_copy(update={"workpiece_instance_ids": ("damper-OTHER",)})
    retry = gateway._leases.issue(
        job_digest=job.aggregate(), nonce=job.nonce, now=NOW + 5, ttl_seconds=60
    )
    blocked = gateway.arm(
        job,
        approved_digest=job.aggregate(),
        program=program,
        preflight=run_preflight(job, wrong_piece),
        lease_id=retry.lease_id,
        now=NOW + 6,
    )
    assert not blocked.armed
    assert "preflight:workpiece_identity" in blocked.refusals
    conn.close()


# --- local execution semantics -----------------------------------------


def executor_for(tmp_path, *, limits=None):
    conn, cells, keys, gateway, plan, program, job, live = full_walk(tmp_path)
    return _executor_from(conn, keys, gateway, program, job, limits)


def _executor_from(conn, keys, gateway, program, job, limits):
    safety = SafetyControllerSim()
    cloud = CloudLink()
    executor = LocalExecutor(
        job=job,
        program=program,
        ir=pilot_ir(),
        mode=ExecutionMode.FIRST_PIECE,
        monitor=DeviationMonitor(limits or {"clamp_force_mn": 500}),
        safety=safety,
        cloud=cloud,
        gateway=gateway,
    )
    return conn, keys, executor, safety, cloud, job


def faithful_plant(step: IRStep) -> dict[str, int]:
    return {
        key: value
        for key, value in step.parameters.items()
        if isinstance(value, int) and not isinstance(value, bool)
    }


def test_cloud_loss_changes_nothing_locally(tmp_path) -> None:
    # Acceptance test 24 / Phase 7 gates 3–4.
    conn, keys, executor, safety, cloud, job = executor_for(tmp_path)
    steps_seen = []

    def plant(step: IRStep) -> dict[str, int]:
        steps_seen.append(step.op)
        if len(steps_seen) == 3:
            cloud.disconnect()  # the cloud dies mid-cycle
        return faithful_plant(step)

    executor.execute(plant, started_at="2026-07-27T10:00:00Z")
    assert executor.state is ActuationState.VERIFYING
    assert executor.cloud_loss_noted  # recorded, never acted on
    # And with everything dark, the safety controller still trips.
    conn2, keys2, executor2, safety2, cloud2, job2 = executor_for(tmp_path / "b")
    cloud2.disconnect()

    def tripping_plant(step: IRStep) -> dict[str, int]:
        if step.op is IROp.CLAMP:
            safety2.trip("guard_opened")
        return faithful_plant(step)

    executor2.execute(tripping_plant, started_at="2026-07-27T10:00:00Z")
    assert executor2.state is ActuationState.SAFE_STOPPED
    assert "guard_opened" in executor2.stop_reason
    conn.close()
    conn2.close()


def test_lease_expiry_mid_cycle_is_not_a_stop(tmp_path) -> None:
    # Acceptance test 25.
    conn, keys, executor, safety, cloud, job = executor_for(tmp_path)

    def plant(step: IRStep) -> dict[str, int]:
        if step.op is IROp.EXECUTE_PROFILE:
            assert executor.on_lease_expired() == "controller_validated_completion"
        return faithful_plant(step)

    executor.execute(plant, started_at="2026-07-27T10:00:00Z")
    assert executor.state is ActuationState.VERIFYING  # the cycle finished
    assert executor.new_start_blocked
    conn.close()


def test_deviation_holds_locally_with_immutable_evidence(tmp_path) -> None:
    # Acceptance tests 26 and 31.
    conn, keys, executor, safety, cloud, job = executor_for(tmp_path)

    def drifting_plant(step: IRStep) -> dict[str, int]:
        actual = faithful_plant(step)
        if step.op is IROp.CLAMP:
            actual["clamp_force_mn"] = 13_000  # 1000 over a 500 bound
        return actual

    executor.execute(drifting_plant, started_at="2026-07-27T10:00:00Z")
    assert executor.state is ActuationState.SAFE_STOPPED
    assert executor.stop_reason == "commanded_actual_deviation_beyond_bound"
    event = executor.deviation_events[0]
    assert (event.commanded, event.actual, event.limit) == (12_000, 13_000, 500)
    with pytest.raises(ExecutionRefused, match="automatic_restart_prohibited"):
        executor.restart()
    bundle = executor.finish(
        disposition=Disposition.QUARANTINED, ended_at="2026-07-27T10:05:00Z"
    )
    assert bundle.deviation_events[0]["actual"] == 13_000
    assert bundle.disposition is Disposition.QUARANTINED
    # The bundle is signed by the evidence identity, not the command key.
    assert keys.verify(
        device_id="edge-evidence-1",
        payload=canonical_json(
            bundle.model_copy(update={"edge_signature": ""}).model_dump(mode="json")
        ),
        signature=bundle.edge_signature,
    )
    conn.close()


def test_first_piece_gates_batch_execution(tmp_path) -> None:
    # Acceptance test 29 / Phase 7 gate 5.
    conn, keys, executor, safety, cloud, job = executor_for(tmp_path)
    executor.execute(faithful_plant, started_at="2026-07-27T10:00:00Z")
    gate = BatchGate()
    digest = job.aggregate()
    assert gate.authorize_batch(digest) == ("first_piece_required",)
    gate.record_first_piece(digest, disposition=Disposition.ACCEPTED)
    assert gate.authorize_batch(digest) == ("first_piece_inspection_pending",)
    gate.record_inspection(digest, passed=False)
    assert gate.authorize_batch(digest) == ("first_piece_inspection_failed",)
    gate.record_inspection(digest, passed=True)
    assert gate.authorize_batch(digest) == ()
    conn.close()


def test_channel_identities_are_structurally_distinct() -> None:
    with pytest.raises(ValueError, match="distinct identities"):
        ChannelIdentities(command_identity="edge-1", evidence_identity="edge-1")
