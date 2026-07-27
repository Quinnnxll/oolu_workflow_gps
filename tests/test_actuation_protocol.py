"""R0 exit gates for the robotic actuation protocol foundation.

Each test names the acceptance test(s) from the build plan it realizes
at contract level (docs/robotic-actuation-protocol-plan.md, stage R0).
"""

from __future__ import annotations

import pytest

from oolu.actuation import (
    ActuationJob,
    ActuationState,
    LeaseRegistry,
    LiveWorkcellState,
    ParameterProposal,
    RecipeVersion,
    ReviewSignals,
    ReviewTier,
    ValidatedOperatingEnvelope,
    arm,
    assign_tier,
    can_transition,
    run_preflight,
    transition,
)
from oolu.actuation.contracts import EnvelopeViolation, ParameterBound
from oolu.actuation.digests import DigestError, aggregate_digest, canonical_json
from oolu.actuation.lease import LeaseRefusal
from oolu.actuation.lifecycle import TransitionError


def make_job(**overrides) -> ActuationJob:
    fields = dict(
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
        coordinate_frame_graph_digest="b" * 64,
        process_recipe_version_id="recipe-5",
        process_parameters={"clamp_force_mn": 12_000, "feed_um_s": 400},
        machine_program_digest="c" * 64,
        validated_operating_envelope_id="env-2",
        local_safety_configuration_digest="d" * 64,
        expected_state_delta={"clamped": True},
        approval_bundle_id="approvals-9",
        execution_window_start="2026-07-27T10:00:00Z",
        execution_window_end="2026-07-27T11:00:00Z",
        nonce="nonce-1",
    )
    fields.update(overrides)
    return ActuationJob(**fields)


def live_state_for(job: ActuationJob, **overrides) -> LiveWorkcellState:
    fields = dict(
        workcell_version_id=job.workcell_version_id,
        machine_instance_ids=job.machine_instance_ids,
        controller_instance_ids=job.controller_instance_ids,
        tool_instance_ids=job.tool_instance_ids,
        fixture_instance_ids=job.fixture_instance_ids,
        workpiece_instance_ids=job.workpiece_instance_ids,
        coordinate_frame_graph_digest=job.coordinate_frame_graph_digest,
        machine_program_digest=job.machine_program_digest,
        local_safety_configuration_digest=job.local_safety_configuration_digest,
        guards_closed=True,
        emergency_stop_clear=True,
        safety_controller_healthy=True,
        energy_state_nominal=True,
        clock_within_tolerance=True,
    )
    fields.update(overrides)
    return LiveWorkcellState(**fields)


# --- digests -----------------------------------------------------------


def test_canonical_json_is_order_independent() -> None:
    assert canonical_json({"b": 1, "a": "x"}) == canonical_json({"a": "x", "b": 1})


def test_floats_are_refused_in_digest_material() -> None:
    with pytest.raises(DigestError):
        canonical_json({"force": 1.5})


def test_aggregate_digest_refuses_missing_and_extra_fields() -> None:
    with pytest.raises(DigestError):
        aggregate_digest({"intent_id": "x"})
    complete = make_job()
    fields = {
        "intent_id": complete.intent_id,
        "plan_version_id": complete.plan_version_id,
    }
    with pytest.raises(DigestError):
        aggregate_digest({**fields, "unbound": "y"})


def test_any_bound_field_change_changes_the_digest() -> None:
    # Acceptance tests 4, 20, 21, 27: workpiece, tool, program, frame,
    # safety config, approvals — every substitution is a new digest.
    base = make_job().aggregate()
    changed = dict(
        workpiece_instance_ids=("wp-OTHER",),
        tool_instance_ids=("gripper-60mm",),
        machine_program_digest="e" * 64,
        coordinate_frame_graph_digest="f" * 64,
        local_safety_configuration_digest="9" * 64,
        approval_bundle_id="approvals-10",
        process_parameters={"clamp_force_mn": 12_001, "feed_um_s": 400},
        nonce="nonce-2",
    )
    digests = {base}
    for field, value in changed.items():
        digests.add(make_job(**{field: value}).aggregate())
    assert len(digests) == len(changed) + 1


# --- leases ------------------------------------------------------------


def test_lease_is_single_use() -> None:
    # Acceptance test 22: a previously consumed lease cannot start a job.
    registry = LeaseRegistry()
    digest = make_job().aggregate()
    lease = registry.issue(job_digest=digest, nonce="n-1", now=100, ttl_seconds=60)
    first = registry.consume(
        lease_id=lease.lease_id, job_digest=digest, nonce="n-1", now=110
    )
    second = registry.consume(
        lease_id=lease.lease_id, job_digest=digest, nonce="n-1", now=111
    )
    assert first is None
    assert second is LeaseRefusal.ALREADY_CONSUMED


def test_expired_and_mismatched_leases_are_refused_by_name() -> None:
    registry = LeaseRegistry()
    digest = make_job().aggregate()
    lease = registry.issue(job_digest=digest, nonce="n-1", now=100, ttl_seconds=60)
    assert (
        registry.consume(
            lease_id=lease.lease_id, job_digest=digest, nonce="n-1", now=160
        )
        is LeaseRefusal.EXPIRED
    )
    assert (
        registry.consume(
            lease_id=lease.lease_id, job_digest="0" * 64, nonce="n-1", now=110
        )
        is LeaseRefusal.JOB_DIGEST_MISMATCH
    )
    assert (
        registry.consume(
            lease_id=lease.lease_id, job_digest=digest, nonce="WRONG", now=110
        )
        is LeaseRefusal.NONCE_MISMATCH
    )
    assert (
        registry.consume(lease_id="lease-missing", job_digest=digest, nonce="n", now=1)
        is LeaseRefusal.UNKNOWN_LEASE
    )
    # Refusals never spent the single use: the right material still arms.
    assert (
        registry.consume(
            lease_id=lease.lease_id, job_digest=digest, nonce="n-1", now=120
        )
        is None
    )


# --- preflight ---------------------------------------------------------


def test_clean_preflight_passes() -> None:
    job = make_job()
    assert run_preflight(job, live_state_for(job)).passed


def test_identity_mismatches_block_by_name() -> None:
    # Acceptance test 20: wrong workpiece, tool, fixture, or frame digest.
    job = make_job()
    result = run_preflight(
        job,
        live_state_for(
            job,
            tool_instance_ids=("gripper-60mm",),
            workpiece_instance_ids=("wp-OTHER",),
            coordinate_frame_graph_digest="f" * 64,
        ),
    )
    assert not result.passed
    assert {"tool_identity", "workpiece_identity", "coordinate_frame_calibration"} <= (
        set(result.failures)
    )


def test_safety_state_blocks_arming_locally() -> None:
    # Acceptance test 23: open guard, active e-stop, failed safety
    # device, or invalid safety configuration blocks arming.
    job = make_job()
    result = run_preflight(
        job,
        live_state_for(
            job,
            guards_closed=False,
            emergency_stop_clear=False,
            safety_controller_healthy=False,
            local_safety_configuration_digest="0" * 64,
        ),
    )
    assert {
        "guards_closed",
        "emergency_stop_clear",
        "safety_controller_health",
        "safety_configuration",
    } <= set(result.failures)


def test_modified_program_fails_digest_check() -> None:
    # Acceptance test 21.
    job = make_job()
    result = run_preflight(job, live_state_for(job, machine_program_digest="e" * 64))
    assert "controller_program_digest" in result.failures


# --- the arming gate ---------------------------------------------------


def test_arming_composes_digest_preflight_and_lease() -> None:
    job = make_job()
    registry = LeaseRegistry()
    lease = registry.issue(
        job_digest=job.aggregate(), nonce=job.nonce, now=100, ttl_seconds=60
    )
    decision = arm(
        job,
        approved_digest=job.aggregate(),
        preflight=run_preflight(job, live_state_for(job)),
        leases=registry,
        lease_id=lease.lease_id,
        now=110,
    )
    assert decision.armed and not decision.refusals


def test_arming_refuses_on_digest_mismatch_without_spending_lease() -> None:
    job = make_job()
    registry = LeaseRegistry()
    lease = registry.issue(
        job_digest=job.aggregate(), nonce=job.nonce, now=100, ttl_seconds=60
    )
    tampered_approval = make_job(tool_instance_ids=("gripper-60mm",)).aggregate()
    decision = arm(
        job,
        approved_digest=tampered_approval,
        preflight=run_preflight(job, live_state_for(job)),
        leases=registry,
        lease_id=lease.lease_id,
        now=110,
    )
    assert not decision.armed
    assert "aggregate_digest_mismatch" in decision.refusals
    # The refused arming did not consume the lease.
    assert (
        registry.consume(
            lease_id=lease.lease_id,
            job_digest=job.aggregate(),
            nonce=job.nonce,
            now=115,
        )
        is None
    )


def test_arming_refuses_on_preflight_failure_and_spent_lease() -> None:
    job = make_job()
    registry = LeaseRegistry()
    lease = registry.issue(
        job_digest=job.aggregate(), nonce=job.nonce, now=100, ttl_seconds=60
    )
    bad_preflight = run_preflight(job, live_state_for(job, guards_closed=False))
    refused = arm(
        job,
        approved_digest=job.aggregate(),
        preflight=bad_preflight,
        leases=registry,
        lease_id=lease.lease_id,
        now=110,
    )
    assert not refused.armed and "preflight:guards_closed" in refused.refusals
    ok = arm(
        job,
        approved_digest=job.aggregate(),
        preflight=run_preflight(job, live_state_for(job)),
        leases=registry,
        lease_id=lease.lease_id,
        now=120,
    )
    assert ok.armed
    replay = arm(
        job,
        approved_digest=job.aggregate(),
        preflight=run_preflight(job, live_state_for(job)),
        leases=registry,
        lease_id=lease.lease_id,
        now=125,
    )
    assert not replay.armed and "lease:already_consumed" in replay.refusals


# --- lifecycle ---------------------------------------------------------


def test_no_shortcut_from_proposed_to_armed() -> None:
    assert not can_transition(ActuationState.PROPOSED, ActuationState.ARMED)
    with pytest.raises(TransitionError):
        transition(ActuationState.PROPOSED, ActuationState.ARMED)


def test_safety_stop_never_resumes() -> None:
    # Acceptance test 31: a safety trip cannot be automatically
    # restarted; the only exit is containment, then a new proposal.
    for target in ActuationState:
        allowed = can_transition(ActuationState.SAFE_STOPPED, target)
        assert allowed == (target is ActuationState.CONTAINED)


def test_rework_reenters_the_full_gate_sequence() -> None:
    assert can_transition(ActuationState.REWORK, ActuationState.PROPOSED)
    assert not can_transition(ActuationState.REWORK, ActuationState.COMPILED)


def test_happy_path_is_exactly_the_gate_order() -> None:
    order = [
        ActuationState.PROPOSED,
        ActuationState.SIMULATED,
        ActuationState.REVIEWED,
        ActuationState.COMPILED,
        ActuationState.PREFLIGHT,
        ActuationState.ARMED,
        ActuationState.EXECUTING,
        ActuationState.VERIFYING,
        ActuationState.ACCEPTED,
    ]
    state = order[0]
    for target in order[1:]:
        state = transition(state, target)
    assert state is ActuationState.ACCEPTED


# --- review tiers ------------------------------------------------------


def test_simulation_only_needs_no_approval() -> None:
    decision = assign_tier(
        ReviewSignals(
            physical_execution_requested=False,
            within_certified_cell=False,
            within_validated_envelope=False,
        )
    )
    assert decision.tier is ReviewTier.A0_SIMULATION_ONLY
    assert not decision.physical_execution_permitted


def test_outside_envelope_is_prohibited_not_escalated() -> None:
    decision = assign_tier(
        ReviewSignals(
            physical_execution_requested=True,
            within_certified_cell=True,
            within_validated_envelope=False,
            # Danger signals cannot buy a tier for an unvalidated envelope.
            irreversible_or_high_energy=True,
        )
    )
    assert decision.tier is ReviewTier.A5_PROHIBITED
    assert not decision.physical_execution_permitted


def test_high_consequence_requires_dual_control() -> None:
    # Acceptance test 18: a high-risk physical job waits for the
    # required human approval — two named safety roles.
    decision = assign_tier(
        ReviewSignals(
            physical_execution_requested=True,
            within_certified_cell=True,
            within_validated_envelope=True,
            human_robot_shared_space=True,
        )
    )
    assert decision.tier is ReviewTier.A4_HIGH_CONSEQUENCE
    assert decision.dual_control
    assert set(decision.required_safety_roles) == {
        "process_owner",
        "safety_or_quality_owner",
    }


def test_material_change_outranks_validated_repeat() -> None:
    decision = assign_tier(
        ReviewSignals(
            physical_execution_requested=True,
            within_certified_cell=True,
            within_validated_envelope=True,
            previously_validated_repeat=True,
            new_material_or_supplier_lot=True,
        )
    )
    assert decision.tier is ReviewTier.A3_MATERIAL_CHANGE


def test_first_run_defaults_to_supervised_commissioning() -> None:
    decision = assign_tier(
        ReviewSignals(
            physical_execution_requested=True,
            within_certified_cell=True,
            within_validated_envelope=True,
        )
    )
    assert decision.tier is ReviewTier.A1_SUPERVISED_COMMISSIONING
    assert decision.continuous_supervision


# --- envelopes and learned parameters ---------------------------------


def envelope() -> ValidatedOperatingEnvelope:
    return ValidatedOperatingEnvelope(
        envelope_id="env-2",
        workcell_version_id="cell-7",
        bounds={
            "clamp_force_mn": ParameterBound(unit="mN", minimum=8_000, maximum=15_000),
            "feed_um_s": ParameterBound(unit="um/s", minimum=100, maximum=600),
        },
    )


def recipe() -> RecipeVersion:
    return RecipeVersion(
        recipe_version_id="recipe-5",
        capability_id="cap-clamp",
        envelope_id="env-2",
        parameters={"clamp_force_mn": 12_000, "feed_um_s": 400},
        status="approved",
        approval_required=False,
    )


def test_out_of_envelope_proposal_is_rejected() -> None:
    # Acceptance test 32.
    proposal = ParameterProposal(
        proposal_id="prop-1",
        recipe_version_id="recipe-5",
        parameters={"clamp_force_mn": 16_000, "feed_um_s": 400},
    )
    with pytest.raises(EnvelopeViolation) as excinfo:
        proposal.apply(recipe(), envelope(), new_version_id="recipe-6")
    assert any("clamp_force_mn" in v for v in excinfo.value.violations)


def test_unbounded_or_missing_parameters_are_violations() -> None:
    proposal = ParameterProposal(
        proposal_id="prop-2",
        recipe_version_id="recipe-5",
        parameters={"clamp_force_mn": 12_000, "spindle_rpm": 3_000},
    )
    with pytest.raises(EnvelopeViolation) as excinfo:
        proposal.apply(recipe(), envelope(), new_version_id="recipe-6")
    joined = " ".join(excinfo.value.violations)
    assert "spindle_rpm" in joined and "feed_um_s" in joined


def test_in_envelope_proposal_still_owes_approval() -> None:
    # Acceptance test 33: an in-envelope change creates a new draft
    # version with approval required — it never mutates the parent.
    parent = recipe()
    proposal = ParameterProposal(
        proposal_id="prop-3",
        recipe_version_id="recipe-5",
        parameters={"clamp_force_mn": 13_000, "feed_um_s": 450},
    )
    child = proposal.apply(parent, envelope(), new_version_id="recipe-6")
    assert child.status == "draft" and child.approval_required
    assert child.parent_version_id == parent.recipe_version_id
    assert parent.parameters == {"clamp_force_mn": 12_000, "feed_um_s": 400}
