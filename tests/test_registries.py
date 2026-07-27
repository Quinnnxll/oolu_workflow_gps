"""R2 exit gates for the protocol registries over the durable layer.

Each test names the acceptance test(s) from the build plan it realizes
(docs/robotic-actuation-protocol-plan.md, stage R2).
"""

from __future__ import annotations

import pytest

from oolu.actuation import ActuationJob, LeaseRegistry, arm, run_preflight
from oolu.durable import DurableConnection
from oolu.evidence import (
    CalibrationRecord,
    CalibrationRegistry,
    EvidenceRights,
    MeasurandCalibration,
    RightsError,
)
from oolu.registries import (
    AuthorizedTestJobRegistry,
    MachineRegistry,
    MethodRegistry,
    MethodStatus,
    RightsStore,
    StandardsRegistry,
    WorkcellRegistry,
    authorization_digest,
)

NOW = 1_000


@pytest.fixture
def conn(tmp_path):
    connection = DurableConnection(tmp_path / "registries.db")
    yield connection
    connection.close()


# --- standards ---------------------------------------------------------


def test_standards_currency_is_a_query_not_prose(conn) -> None:
    registry = StandardsRegistry(conn)
    registry.register(
        standard_id="ISO 10218-1",
        edition="2011",
        title="Robots and robotic devices — safety requirements",
        now=NOW,
    )
    registry.register(
        standard_id="ISO 10218-1",
        edition="2025",
        title="Robotics — safety requirements for robots in industry",
        now=NOW + 10,
    )
    assert registry.current("ISO 10218-1").edition == "2025"
    old = registry.get("ISO 10218-1", "2011")
    assert old.status == "superseded" and old.superseded_by == "2025"
    registry.withdraw(standard_id="ISO 10218-1", edition="2011")
    assert registry.get("ISO 10218-1", "2011").status == "withdrawn"
    assert registry.current("ISO 9999") is None
    with pytest.raises(ValueError, match="immutable"):
        registry.register(
            standard_id="ISO 10218-1", edition="2025", title="dup", now=NOW + 20
        )


# --- machines and instrument selection ---------------------------------


def _force_rig(machines: MachineRegistry, *, machine_id, sensor_id, unc, cal=None):
    machines.register_machine(
        machine_instance_id=machine_id,
        machine_definition="dyno",
        serial_number=f"sn-{machine_id}",
        facility_id="plant-1",
        now=NOW,
        status="active",
    )
    machines.register_sensor(
        sensor_instance_id=sensor_id,
        machine_instance_id=machine_id,
        quantity_kind="force",
        unit="N",
        range_minimum=-10_000.0,
        range_maximum=10_000.0,
        achievable_standard_uncertainty=unc,
        calibration_record_id=cal,
        now=NOW,
    )


def test_instruments_selected_by_range_and_uncertainty_not_name(conn) -> None:
    # Acceptance test 17 at registry level.
    machines = MachineRegistry(conn)
    _force_rig(machines, machine_id="famous-big-rig", sensor_id="force-big", unc=8.0)
    _force_rig(machines, machine_id="modest-rig", sensor_id="force-modest", unc=1.5)
    found = machines.find_instruments(
        quantity_kind="force",
        unit="N",
        range_minimum=-5_000.0,
        range_maximum=5_000.0,
        max_standard_uncertainty=2.0,
    )
    assert [sensor.sensor_instance_id for sensor in found] == ["force-modest"]
    # Nothing matched on any name; the famous rig fails only on capability.
    wide = machines.find_instruments(
        quantity_kind="force",
        unit="N",
        range_minimum=-5_000.0,
        range_maximum=5_000.0,
        max_standard_uncertainty=10.0,
    )
    assert [s.sensor_instance_id for s in wide] == ["force-modest", "force-big"]


def test_quarantine_and_calibration_wall_the_query(conn) -> None:
    machines = MachineRegistry(conn)
    calibrations = CalibrationRegistry()
    calibrations.register(
        CalibrationRecord(
            calibration_record_id="cal-expired",
            sensor_or_instrument_id="force-a",
            calibration_provider_id="lab-9",
            method_id="cal-m",
            calibration_date=100,
            valid_until=500,  # expired by NOW
            measurands=(
                MeasurandCalibration(
                    quantity_kind="force",
                    unit="N",
                    range_minimum=-10_000.0,
                    range_maximum=10_000.0,
                ),
            ),
        )
    )
    _force_rig(
        machines, machine_id="rig-a", sensor_id="force-a", unc=1.0, cal="cal-expired"
    )
    _force_rig(machines, machine_id="rig-b", sensor_id="force-b", unc=1.0)

    def calibration_valid(sensor) -> bool:
        if sensor.calibration_record_id is None:
            return True
        return calibrations.check(
            sensor.calibration_record_id,
            quantity_kind=sensor.quantity_kind,
            unit=sensor.unit,
            value=0.0,
            test_time=NOW,
        ).valid

    found = machines.find_instruments(
        quantity_kind="force",
        unit="N",
        range_minimum=-1_000.0,
        range_maximum=1_000.0,
        max_standard_uncertainty=2.0,
        calibration_valid=calibration_valid,
    )
    assert [s.sensor_instance_id for s in found] == ["force-b"]
    machines.set_machine_status("rig-b", "quarantined")
    assert (
        machines.find_instruments(
            quantity_kind="force",
            unit="N",
            range_minimum=-1_000.0,
            range_maximum=1_000.0,
            max_standard_uncertainty=2.0,
            calibration_valid=calibration_valid,
        )
        == ()
    )
    machines.set_machine_status("rig-b", "retired")
    with pytest.raises(ValueError, match="retired"):
        machines.set_machine_status("rig-b", "active")


# --- methods -----------------------------------------------------------


def test_method_lifecycle_is_a_whitelist(conn) -> None:
    methods = MethodRegistry(conn)
    methods.publish(
        method_id="method-fv", version="1.0.0", title="Force-velocity", now=NOW
    )
    with pytest.raises(ValueError, match="illegal method transition"):
        methods.set_status(
            method_id="method-fv",
            version="1.0.0",
            status=MethodStatus.APPROVED,
            now=NOW,
        )
    methods.set_status(
        method_id="method-fv", version="1.0.0", status=MethodStatus.VALIDATED, now=NOW
    )
    methods.set_status(
        method_id="method-fv", version="1.0.0", status=MethodStatus.APPROVED, now=NOW
    )
    assert methods.authorized("method-fv", "1.0.0")
    methods.set_status(
        method_id="method-fv",
        version="1.0.0",
        status=MethodStatus.SUPERSEDED,
        now=NOW,
    )
    assert not methods.authorized("method-fv", "1.0.0")
    with pytest.raises(ValueError, match="illegal method transition"):
        methods.set_status(
            method_id="method-fv",
            version="1.0.0",
            status=MethodStatus.APPROVED,
            now=NOW,
        )


# --- test jobs ---------------------------------------------------------


def _authorize(jobs: AuthorizedTestJobRegistry, **overrides):
    fields = dict(
        test_job_id="tj-1",
        specimen_instance_ids=("damper-42",),
        method_id="method-fv",
        method_version="1.0.0",
        machine_instance_id="dyno-1",
        sensor_instance_ids=("force-7", "disp-3"),
        controller_configuration_digest="c" * 64,
        target_conditions={"temperature_mk": 296_150, "profile": "sine-2hz"},
        data_rights_policy_id="rights-1",
        approval_policy_id="policy-a2",
        expires_at=NOW + 3_600,
        now=NOW,
    )
    fields.update(overrides)
    return jobs.authorize(**fields)


def test_unapproved_method_version_quarantines_the_job(conn) -> None:
    # Acceptance test 3.
    methods = MethodRegistry(conn)
    methods.publish(method_id="method-fv", version="1.0.0", title="FV", now=NOW)
    jobs = AuthorizedTestJobRegistry(conn, methods)
    job = _authorize(jobs)
    assert job.status == "quarantined"
    assert job.reason == "unauthorized_method_version:method-fv@1.0.0"
    assert job.authorization_digest is None
    assert not jobs.verify(job)
    # The quarantined attempt is stored — it remains evidence.
    assert jobs.get("tj-1").status == "quarantined"


def test_authorized_job_digest_binds_configuration(conn) -> None:
    # Acceptance test 4 for test jobs: a changed controller
    # configuration invalidates the digest.
    methods = MethodRegistry(conn)
    methods.publish(method_id="method-fv", version="1.0.0", title="FV", now=NOW)
    methods.set_status(
        method_id="method-fv", version="1.0.0", status=MethodStatus.VALIDATED, now=NOW
    )
    methods.set_status(
        method_id="method-fv", version="1.0.0", status=MethodStatus.APPROVED, now=NOW
    )
    jobs = AuthorizedTestJobRegistry(conn, methods)
    job = _authorize(jobs)
    assert job.status == "authorized"
    assert jobs.verify(job)
    drifted = job.model_copy(update={"controller_configuration_digest": "e" * 64})
    assert not jobs.verify(drifted)
    assert authorization_digest(drifted) != job.authorization_digest


# --- workcells, tools, and the frame graph -----------------------------


def _qualified_cell(conn) -> WorkcellRegistry:
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
    cells.register_tool(
        tool_instance_id="gripper-40mm",
        tool_type="gripper",
        serial_number="g-1",
        geometry_digest="1" * 64,
        wear_limit=100,
        now=NOW,
    )
    cells.calibrate_frame(
        workcell_version_id="cell-7",
        frame_id="base",
        calibration_digest="2" * 64,
        now=NOW,
    )
    cells.calibrate_frame(
        workcell_version_id="cell-7",
        frame_id="tcp",
        calibration_digest="3" * 64,
        now=NOW,
    )
    return cells


def _job_for(cells: WorkcellRegistry) -> ActuationJob:
    return ActuationJob(
        job_id="job-1",
        intent_id="intent-1",
        plan_version_id="plan-1",
        workcell_version_id="cell-7",
        workpiece_instance_ids=("wp-1",),
        input_state_digest="a" * 64,
        machine_instance_ids=("robot-1", "dyno-1"),
        controller_instance_ids=("ctrl-1",),
        tool_instance_ids=("gripper-40mm",),
        fixture_instance_ids=("fixture-3",),
        coordinate_frame_graph_digest=cells.frame_graph_digest("cell-7"),
        process_recipe_version_id="recipe-5",
        process_parameters={"clamp_force_mn": 12_000},
        machine_program_digest="c" * 64,
        validated_operating_envelope_id="env-2",
        local_safety_configuration_digest="d" * 64,
        expected_state_delta={"clamped": True},
        approval_bundle_id="approvals-9",
        execution_window_start="2026-07-27T10:00:00Z",
        execution_window_end="2026-07-27T11:00:00Z",
        nonce="nonce-1",
    )


def _live(cells: WorkcellRegistry):
    return cells.live_state(
        "cell-7",
        tool_instance_ids=("gripper-40mm",),
        fixture_instance_ids=("fixture-3",),
        workpiece_instance_ids=("wp-1",),
        machine_program_digest="c" * 64,
        guards_closed=True,
        emergency_stop_clear=True,
        safety_controller_healthy=True,
        energy_state_nominal=True,
        clock_within_tolerance=True,
    )


def test_worn_or_failed_tool_blocks_the_next_job(conn) -> None:
    # Acceptance test 28.
    cells = _qualified_cell(conn)
    job = _job_for(cells)
    assert cells.release_check(job, now=NOW) == ()
    cells.record_tool_wear("gripper-40mm", 100)
    assert "tool_wear_limit_reached:gripper-40mm" in cells.release_check(job, now=NOW)
    cells.register_tool(
        tool_instance_id="gripper-60mm",
        tool_type="gripper",
        serial_number="g-2",
        geometry_digest="4" * 64,
        wear_limit=100,
        now=NOW,
    )
    cells.record_tool_inspection("gripper-60mm", passed=False, now=NOW)
    failures = cells.tool_release("gripper-60mm", now=NOW)
    assert "tool_inspection_failed:gripper-60mm" in failures
    assert "tool_quarantined:gripper-60mm" in failures
    # A passed inspection with a due date goes overdue, and blocks again.
    cells.record_tool_inspection(
        "gripper-60mm", passed=True, now=NOW, next_due_at=NOW + 50
    )
    assert cells.tool_release("gripper-60mm", now=NOW + 10) == ()
    assert cells.tool_release("gripper-60mm", now=NOW + 60) == (
        "tool_inspection_overdue:gripper-60mm",
    )


def test_unqualified_workcell_blocks_release(conn) -> None:
    cells = _qualified_cell(conn)
    job = _job_for(cells)
    cells.set_commissioning_status("cell-7", "suspended")
    assert "workcell_not_qualified:suspended" in cells.release_check(job, now=NOW)


def test_frame_recalibration_invalidates_prior_authorization(conn) -> None:
    # Acceptance test 27, end to end: authorize against the current
    # frame graph, arm successfully, recalibrate one frame, and the
    # same job can never arm again.
    cells = _qualified_cell(conn)
    job = _job_for(cells)
    leases = LeaseRegistry()
    lease = leases.issue(
        job_digest=job.aggregate(), nonce=job.nonce, now=NOW, ttl_seconds=60
    )
    first = arm(
        job,
        approved_digest=job.aggregate(),
        preflight=run_preflight(job, _live(cells)),
        leases=leases,
        lease_id=lease.lease_id,
        now=NOW + 1,
    )
    assert first.armed
    cells.calibrate_frame(
        workcell_version_id="cell-7",
        frame_id="tcp",
        calibration_digest="9" * 64,
        now=NOW + 10,
    )
    result = run_preflight(job, _live(cells))
    assert "coordinate_frame_calibration" in result.failures
    retry = leases.issue(
        job_digest=job.aggregate(), nonce=job.nonce, now=NOW + 20, ttl_seconds=60
    )
    second = arm(
        job,
        approved_digest=job.aggregate(),
        preflight=result,
        leases=leases,
        lease_id=retry.lease_id,
        now=NOW + 21,
    )
    assert not second.armed
    assert "preflight:coordinate_frame_calibration" in second.refusals
    # History survives: both calibrations of the frame remain recorded.
    with conn.transaction() as db:
        rows = db.execute(
            "SELECT COUNT(*) AS n FROM protocol_frame_calibrations"
            " WHERE frame_id = 'tcp'"
        ).fetchone()
    assert rows["n"] == 2


# --- durable rights ----------------------------------------------------


def test_rights_store_is_durable_and_revocation_refuses_by_name(conn) -> None:
    store = RightsStore(conn)
    grant = EvidenceRights(
        rights_id="rights-1",
        owner_ids=("supplier-a",),
        evidence_scope=("obs-1",),
        formula_discovery=True,
    )
    store.save(grant, now=NOW)
    assert store.get("rights-1") == grant
    with pytest.raises(ValueError, match="new rights contract"):
        store.save(grant, now=NOW + 1)
    store.require("rights-1", "formula_discovery", evidence_id="obs-1", now=NOW + 2)
    with pytest.raises(RightsError, match="permission_not_granted"):
        store.require(
            "rights-1", "model_weight_training", evidence_id="obs-1", now=NOW + 2
        )
    store.revoke("rights-1", at=NOW + 10)
    with pytest.raises(RightsError, match="rights_revoked"):
        store.require(
            "rights-1", "formula_discovery", evidence_id="obs-1", now=NOW + 11
        )
    # The contract itself is still readable — revoked, not erased.
    assert store.get("rights-1") is not None
    with pytest.raises(RightsError, match="unknown_rights"):
        store.require("rights-9", "formula_discovery", evidence_id="obs-1", now=NOW)
