"""R5 exit gates for verified feedback and the science stack.

Each test names the acceptance test or phase gate it realizes
(docs/robotic-actuation-protocol-plan.md, stage R5).
"""

from __future__ import annotations

import pytest

from oolu.actuation import ActionIntent, CapabilityFamily, Disposition
from oolu.actuation.contracts import ExecutionEvidenceBundle
from oolu.durable import DurableConnection
from oolu.evidence import EvidenceGrade, EvidenceRights, RightsError
from oolu.registries import RightsStore
from oolu.science import (
    CandidateObservation,
    DatasetDefinition,
    Dimension,
    FormulaCandidate,
    FormulaCandidateRegistry,
    InspectionFinding,
    OutcomeKind,
    ProcessCapabilityLedger,
    ProcessOutcome,
    PromotionRefused,
    StateDeltaRefused,
    Term,
    ValidationEvidence,
    WorldModelRegistry,
    build_dataset,
    model_use_gate,
    promote,
    propose_experiment,
    qualify_state_delta,
    route_failure,
    update_with_evidence,
    validate_safety_function,
)
from oolu.science.formulas import CandidateStatus

NOW = 1_000

FORCE = Dimension(powers={"kg": 1, "m": 1, "s": -2})
VELOCITY = Dimension(powers={"m": 1, "s": -1})
DAMPING = Dimension(powers={"kg": 1, "s": -1})


def bundle(**overrides) -> ExecutionEvidenceBundle:
    fields = dict(
        job_digest="j" * 64,
        program_digest="p" * 64,
        started_at="2026-07-27T10:00:00Z",
        ended_at="2026-07-27T10:05:00Z",
        commanded_trace_ref="trace://commanded/job-1",
        actual_trace_ref="trace://actual/job-1",
        pre_workpiece_state_id="wps-0",
        disposition=Disposition.ACCEPTED,
    )
    fields.update(overrides)
    return ExecutionEvidenceBundle(**fields)


# --- state-delta qualification (Phase 8) --------------------------------


def test_state_delta_requires_independent_inspection() -> None:
    with pytest.raises(StateDeltaRefused) as excinfo:
        qualify_state_delta(
            bundle(),
            (),
            delta_id="delta-1",
            expected_properties=("clamped",),
            post_state_id="wps-1",
        )
    assert "no_independent_verification" in excinfo.value.reasons
    disputed = (
        InspectionFinding(
            observation_id="obs-9", observed_property="clamped", confirmed=False
        ),
    )
    with pytest.raises(StateDeltaRefused) as excinfo:
        qualify_state_delta(
            bundle(),
            disputed,
            delta_id="delta-1",
            expected_properties=("clamped",),
            post_state_id="wps-1",
        )
    assert "disputed_property:clamped" in excinfo.value.reasons


def test_qualified_delta_traces_digest_deep() -> None:
    # Phase 8 gate 1: the record reads back to the job digest, program,
    # traces, and inspection observations.
    delta = qualify_state_delta(
        bundle(),
        (
            InspectionFinding(
                observation_id="obs-9", observed_property="clamped", confirmed=True
            ),
        ),
        delta_id="delta-1",
        expected_properties=("clamped",),
        post_state_id="wps-1",
    )
    refs = delta.lineage_refs()
    assert refs["job_digest"] == "j" * 64
    assert refs["program_digest"] == "p" * 64
    assert refs["inspection_observation_ids"] == ("obs-9",)
    assert all(value for value in refs.values())


def test_failure_routes_to_declared_disposition_never_rollback() -> None:
    # Acceptance test 30.
    declared = ("rework", "quarantine")
    with pytest.raises(StateDeltaRefused, match="generic_rollback"):
        route_failure(declared_disposition_paths=declared, requested_path="rollback")
    with pytest.raises(StateDeltaRefused, match="undeclared_disposition_path"):
        route_failure(declared_disposition_paths=declared, requested_path="scrap")
    assert (
        route_failure(declared_disposition_paths=declared, requested_path="quarantine")
        is Disposition.QUARANTINED
    )


def test_simulation_alone_cannot_validate_a_safety_function() -> None:
    # Acceptance test 34.
    sim_only = (
        ValidationEvidence(evidence_id="v-1", kind="simulation"),
        ValidationEvidence(evidence_id="v-2", kind="digital_twin"),
    )
    assert validate_safety_function("estop-1", sim_only) == (
        "simulation_alone_cannot_validate_safety_function",
    )
    with_physical = (
        *sim_only,
        ValidationEvidence(evidence_id="v-3", kind="physical_test"),
    )
    assert validate_safety_function("estop-1", with_physical) == ()
    assert validate_safety_function("estop-1", ()) == (
        "no_validation_evidence:estop-1",
    )


# --- capability learning (test 35, Phase 8 gate 4) ----------------------


def rights_store(tmp_path, *, scope: tuple[str, ...], **permissions) -> RightsStore:
    conn = DurableConnection(tmp_path / "rights.db")
    store = RightsStore(conn)
    store.save(
        EvidenceRights(
            rights_id="rights-1",
            owner_ids=("plant-1",),
            evidence_scope=scope,
            **permissions,
        ),
        now=NOW,
    )
    return store


def test_negative_outcomes_enter_learning_under_rights(tmp_path) -> None:
    store = rights_store(
        tmp_path, scope=("j" * 64, "k" * 64, "l" * 64), route_training=True
    )
    ledger = ProcessCapabilityLedger(store)
    ledger.ingest(
        ProcessOutcome(
            job_digest="j" * 64,
            recipe_version_id="recipe-5",
            node_version_id="node-robot-1",
            kind=OutcomeKind.ACCEPTED,
            rights_id="rights-1",
        ),
        now=NOW,
    )
    ledger.ingest(
        ProcessOutcome(
            job_digest="k" * 64,
            recipe_version_id="recipe-5",
            node_version_id="node-robot-1",
            kind=OutcomeKind.SCRAPPED,
            deviation_count=1,
            rights_id="rights-1",
        ),
        now=NOW,
    )
    ledger.ingest(
        ProcessOutcome(
            job_digest="l" * 64,
            recipe_version_id="recipe-5",
            node_version_id="node-robot-1",
            kind=OutcomeKind.NEAR_MISS,
            rights_id="rights-1",
        ),
        now=NOW,
    )
    # Negatives lower the routing signal and remain queryable.
    assert ledger.historical_yield("recipe-5") == pytest.approx(1 / 3)
    kinds = {o.kind for o in ledger.negative_outcomes()}
    assert kinds == {OutcomeKind.SCRAPPED, OutcomeKind.NEAR_MISS}
    # And learning without route_training rights refuses upstream.
    unlicensed = ProcessOutcome(
        job_digest="m" * 64,
        recipe_version_id="recipe-5",
        node_version_id="node-robot-1",
        kind=OutcomeKind.ACCEPTED,
        rights_id="rights-1",
    )
    with pytest.raises(RightsError, match="evidence_out_of_scope"):
        ledger.ingest(unlicensed, now=NOW)


# --- the dataset builder (Phase 10, tests 9, 13, 14) --------------------


def observation(**overrides) -> CandidateObservation:
    fields = dict(
        observation_id="obs-1",
        variable="force",
        value=100.0,
        unit="N",
        method_family="dynamometer",
        facility_id="lab-a",
        specimen_id="damper-1",
        calibration_record_ids=("cal-a",),
        evidence_grade=EvidenceGrade.E3_CONTROLLED_METHOD_RESULT,
        rights_id="rights-1",
    )
    fields.update(overrides)
    return CandidateObservation(**fields)


def definition() -> DatasetDefinition:
    return DatasetDefinition(
        dataset_id="ds-fv",
        scientific_question="force_velocity_relationship",
        variables={"force": "N", "velocity": "m/s"},
        minimum_evidence_grade=EvidenceGrade.E3_CONTROLLED_METHOD_RESULT,
        allowed_method_families=("dynamometer",),
    )


def test_dataset_walls_exclude_by_name(tmp_path) -> None:
    # Acceptance test 9 plus Phase 10 gate 3.
    store = rights_store(
        tmp_path, scope=("obs-1", "obs-2", "obs-3", "obs-4"), formula_discovery=True
    )
    rows = (
        observation(),
        observation(observation_id="obs-2", specimen_id="damper-2", unit="lbf"),
        observation(
            observation_id="obs-3",
            specimen_id="damper-3",
            evidence_grade=EvidenceGrade.E1_AUTHENTICATED_DEVICE_OBSERVATION,
        ),
        observation(observation_id="obs-4", specimen_id="damper-1"),  # duplicate
        observation(observation_id="obs-5", specimen_id="damper-5"),  # no rights
    )
    manifest = build_dataset(
        definition(), rows, rights=store, dataset_version_id="dsv-1", now=NOW
    )
    assert manifest.included_observation_ids == ("obs-1",)
    reasons = {e.observation_id: e.reason for e in manifest.excluded}
    assert reasons["obs-2"] == "incompatible_unit:lbf"
    assert reasons["obs-3"] == "evidence_grade_below_minimum:E1"
    assert reasons["obs-4"] == "duplicate_specimen:damper-1"
    assert reasons["obs-5"].startswith("rights:")


def test_dataset_builds_are_deterministic(tmp_path) -> None:
    # Phase 10 gate 2.
    store = rights_store(tmp_path, scope=("obs-1", "obs-2"), formula_discovery=True)
    rows = (
        observation(),
        observation(observation_id="obs-2", specimen_id="damper-2"),
    )
    first = build_dataset(
        definition(), rows, rights=store, dataset_version_id="dsv-1", now=NOW
    )
    second = build_dataset(
        definition(),
        tuple(reversed(rows)),
        rights=store,
        dataset_version_id="dsv-1",
        now=NOW,
    )
    assert first.content_digest == second.content_digest
    smaller = build_dataset(
        definition(), rows[:1], rights=store, dataset_version_id="dsv-1", now=NOW
    )
    assert smaller.content_digest != first.content_digest


def two_lab_rows(shared_calibration: bool) -> tuple[CandidateObservation, ...]:
    calibration_b = ("cal-a",) if shared_calibration else ("cal-b",)
    return tuple(
        observation(
            observation_id=f"obs-a{i}",
            specimen_id=f"damper-a{i}",
            facility_id="lab-a",
        )
        for i in range(3)
    ) + tuple(
        observation(
            observation_id=f"obs-b{i}",
            specimen_id=f"damper-b{i}",
            facility_id="lab-b",
            calibration_record_ids=calibration_b,
        )
        for i in range(3)
    )


def test_two_labs_make_a_replication_split(tmp_path) -> None:
    # Acceptance test 13.
    rows = two_lab_rows(shared_calibration=False)
    store = rights_store(
        tmp_path,
        scope=tuple(r.observation_id for r in rows),
        formula_discovery=True,
    )
    manifest = build_dataset(
        definition(), rows, rights=store, dataset_version_id="dsv-2", now=NOW
    )
    assert manifest.facilities == ("lab-a", "lab-b")
    assert all(oid.startswith("obs-b") for oid in manifest.replication_split)
    assert manifest.independent_replication
    assert manifest.shared_calibration_pairs == ()


def test_shared_calibration_defeats_independence(tmp_path) -> None:
    # Acceptance test 14: detected before independence is claimed.
    rows = two_lab_rows(shared_calibration=True)
    store = rights_store(
        tmp_path,
        scope=tuple(r.observation_id for r in rows),
        formula_discovery=True,
    )
    manifest = build_dataset(
        definition(), rows, rights=store, dataset_version_id="dsv-3", now=NOW
    )
    assert manifest.replication_split  # the split exists...
    assert not manifest.independent_replication  # ...but is not independent
    assert manifest.shared_calibration_pairs[0][0] == "cal-a"


# --- the formula sandbox (Phase 11, test 10) ----------------------------


def candidate(**overrides) -> FormulaCandidate:
    fields = dict(
        candidate_id="fc-1",
        symbolic_expression="F = c * v",
        dependent=Term(name="force", dimension=FORCE, unit="N"),
        independents=(
            Term(name="c", dimension=DAMPING, unit="kg/s"),
            Term(name="velocity", dimension=VELOCITY, unit="m/s"),
        ),
        computed_rhs_dimension=DAMPING.times(VELOCITY),
        parameter_estimates={"c": 1450.0},
        uncertainty_model={"parameter_c_std": 25.0},
        applicability_domain={"velocity": "0.05..1.0 m/s"},
        training_dataset_version_id="dsv-1",
        residual_rmse=2.1,
        discovery_node="discovery-sindy-1",
    )
    fields.update(overrides)
    return FormulaCandidate(**fields)


def test_dimensional_invalidity_is_automatic_rejection() -> None:
    # Acceptance test 10, and the failure stays stored (Phase 11 gate 3).
    registry = FormulaCandidateRegistry()
    bad = candidate(candidate_id="fc-bad", computed_rhs_dimension=VELOCITY)
    stored = registry.screen(bad)
    assert stored.status is CandidateStatus.REJECTED
    assert stored.reason == "dimensionally_invalid"
    assert registry.rejected()[0].candidate.candidate_id == "fc-bad"
    good = registry.screen(candidate())
    assert good.status is CandidateStatus.SCREENED


def test_no_candidate_path_reaches_approved() -> None:
    # Phase 11 gate 1: the registry has no edge into approval.
    registry = FormulaCandidateRegistry()
    registry.screen(candidate())
    assert not hasattr(CandidateStatus, "APPROVED")
    with pytest.raises(ValueError, match="illegal candidate transition"):
        registry.set_status("fc-1", CandidateStatus.SUPERSEDED)


# --- promotion and the world-model registry (Phase 12) ------------------


def review_ready_registry() -> FormulaCandidateRegistry:
    registry = FormulaCandidateRegistry()
    registry.screen(candidate())
    registry.set_status("fc-1", CandidateStatus.REPLICATION_REQUIRED)
    registry.set_status("fc-1", CandidateStatus.SCIENTIFIC_REVIEW)
    return registry


def validation_manifest(tmp_path, *, shared: bool):
    rows = two_lab_rows(shared_calibration=shared)
    store = rights_store(
        tmp_path,
        scope=tuple(r.observation_id for r in rows),
        formula_discovery=True,
    )
    return build_dataset(
        definition(), rows, rights=store, dataset_version_id="dsv-val", now=NOW
    )


REVIEWERS = {
    "domain_scientist": "dr-vasquez",
    "metrology_reviewer": "dr-osei",
    "data_rights_reviewer": "counsel-kim",
}


def test_strong_fit_without_replication_cannot_promote(tmp_path) -> None:
    # Acceptance test 11 and Phase 12 gate 1 (self-approval).
    candidates = review_ready_registry()
    conn = DurableConnection(tmp_path / "models.db")
    models = WorldModelRegistry(conn)
    manifest = validation_manifest(tmp_path, shared=False)
    lonely = manifest.model_copy(
        update={"replication_split": (), "independent_replication": False}
    )
    with pytest.raises(PromotionRefused) as excinfo:
        promote(
            candidates.get("fc-1"),
            manifest=lonely,
            holdout_passed=True,
            reviewers=REVIEWERS,
            registry=models,
            candidates=candidates,
            model_id="wm-damper-fv",
            version="1.0.0",
            now=NOW,
        )
    assert "independent_replication_required" in excinfo.value.reasons
    with pytest.raises(PromotionRefused) as excinfo:
        promote(
            candidates.get("fc-1"),
            manifest=manifest,
            holdout_passed=True,
            reviewers={**REVIEWERS, "metrology_reviewer": "discovery-sindy-1"},
            registry=models,
            candidates=candidates,
            model_id="wm-damper-fv",
            version="1.0.0",
            now=NOW,
        )
    assert "self_approval_prohibited:metrology_reviewer" in excinfo.value.reasons
    conn.close()


def test_promotion_succeeds_and_versions_are_immutable(tmp_path) -> None:
    # Phase 12 gates 3-4 and acceptance test 12.
    candidates = review_ready_registry()
    conn = DurableConnection(tmp_path / "models.db")
    models = WorldModelRegistry(conn)
    manifest = validation_manifest(tmp_path, shared=False)
    model = promote(
        candidates.get("fc-1"),
        manifest=manifest,
        holdout_passed=True,
        reviewers=REVIEWERS,
        registry=models,
        candidates=candidates,
        model_id="wm-damper-fv",
        version="1.0.0",
        now=NOW,
    )
    assert model.validation_dataset_version_id == "dsv-val"
    assert candidates.get("fc-1").status is CandidateStatus.SUPERSEDED
    # Immutable: the same version cannot be re-registered.
    with pytest.raises(ValueError, match="immutable"):
        models.register(model, now=NOW + 1)
    # Challenged remains fully retrievable (test 12).
    models.challenge("wm-damper-fv", "1.0.0", evidence_ref="contradiction-obs-77")
    retrieved, status = models.get("wm-damper-fv", "1.0.0")
    assert status == "challenged"
    assert retrieved.symbolic_expression == "F = c * v"
    models.supersede("wm-damper-fv", "1.0.0", by_version="1.1.0")
    assert models.get("wm-damper-fv", "1.0.0")[1] == "superseded"
    conn.close()


def test_model_use_gate_enforces_status_and_reviews() -> None:
    assert model_use_gate("challenged", "advisory") == ()
    assert model_use_gate("challenged", "engineering_design") == (
        "model_not_approved:challenged",
    )
    refusals = model_use_gate("approved", "safety_critical_execution")
    assert "independent_validation_required" in refusals
    assert "human_review_required" in refusals
    assert (
        model_use_gate(
            "approved",
            "safety_critical_execution",
            independent_validation=True,
            human_review=True,
        )
        == ()
    )


# --- closed-loop experiments (Phase 13) ---------------------------------


def test_proposals_are_intents_and_updates_are_new_candidates() -> None:
    intent = ActionIntent(
        intent_id="intent-exp-1",
        goal_id="goal-uncertainty-1",
        requested_by="oolu-experiment-planner",
        desired_state_predicate={"tested": True},
        input_workpiece_state_id="wps-0",
        allowed_capability_families=(CapabilityFamily.TEST_ACTUATION,),
    )
    proposal = propose_experiment(
        proposal_id="exp-1",
        world_model_id="wm-damper-fv",
        world_model_version="1.0.0",
        uncertainty_region={"velocity": "0.8..1.0 m/s", "temperature": "-20C"},
        intent=intent,
        expected_information_gain=5.0,
        applicability_expansion_value=2.0,
        product_risk_reduction=1.5,
        route_reuse_value=0.5,
        test_cost=1.0,
        specimen_cost=0.5,
        machine_time=0.5,
        safety_risk=1.0,
    )
    assert proposal.priority == pytest.approx(6.0)
    # The proposal carries an intent only — no approval, lease, or
    # program field exists to smuggle authorization through.
    assert set(proposal.intent.model_dump()) == set(intent.model_dump())
    revised = update_with_evidence(
        candidate(),
        new_candidate_id="fc-2",
        new_training_dataset_version_id="dsv-4",
        revised_parameters={"c": 1470.0},
    )
    assert revised.parent_candidate_id == "fc-1"
    assert revised.candidate_id == "fc-2"
    assert candidate().parameter_estimates == {"c": 1450.0}  # parent untouched
