"""R1 exit gates for the evidence and observation spine.

Each test names the acceptance test(s) from the build plan it realizes
at contract level (docs/robotic-actuation-protocol-plan.md, stage R1).
"""

from __future__ import annotations

import pytest

from oolu.evidence import (
    CalibrationRecord,
    CalibrationRegistry,
    DeviceRegistry,
    DeviceStatus,
    EvidenceGrade,
    EvidenceIngress,
    EvidenceLedger,
    EvidenceRights,
    GradeFacts,
    HmacDeviceKeys,
    IngressRefusal,
    LineageError,
    LineageLedger,
    MeasurandCalibration,
    Observation,
    ObservationResult,
    ProcessingStep,
    RawChunk,
    RawEvidenceManifest,
    RightsError,
    Uncertainty,
    apply_calibration_correction,
    chunk_hash,
    grade,
    merkle_root,
    require_right,
)

DEVICE = "gateway-1"
KEY = b"device-key-under-test"


def make_manifest(payloads: dict[str, bytes]) -> RawEvidenceManifest:
    chunks = tuple(
        RawChunk(
            chunk_id=chunk_id,
            channel_ids=("force", "displacement"),
            start_time="2026-07-27T10:00:00Z",
            end_time="2026-07-27T10:00:10Z",
            sample_count=10_000,
            dropped_sample_count=0,
            content_hash=chunk_hash(payload),
        )
        for chunk_id, payload in payloads.items()
    )
    return RawEvidenceManifest(
        evidence_bundle_id="bundle-1",
        test_job_id="job-1",
        authorization_digest="a" * 64,
        machine_instance_id="dyno-1",
        sensor_instance_ids=("force-7",),
        specimen_instance_ids=("damper-42",),
        method_id="method-fv",
        method_version="1.0.0",
        started_at="2026-07-27T10:00:00Z",
        completed_at="2026-07-27T10:05:00Z",
        clock_source="ptp",
        firmware_digest="b" * 64,
        chunks=chunks,
        merkle_root=merkle_root([chunk.content_hash for chunk in chunks]),
    )


def make_ingress() -> tuple[EvidenceIngress, HmacDeviceKeys, DeviceRegistry]:
    devices = DeviceRegistry()
    devices.register(DEVICE)
    keys = HmacDeviceKeys()
    keys.register_key(DEVICE, KEY)
    return EvidenceIngress(devices, keys), keys, devices


def calibration(*, valid_until: int | None = 2_000) -> CalibrationRecord:
    return CalibrationRecord(
        calibration_record_id="cal-1",
        sensor_or_instrument_id="force-7",
        calibration_provider_id="lab-9",
        method_id="cal-method-1",
        calibration_date=1_000,
        valid_until=valid_until,
        measurands=(
            MeasurandCalibration(
                quantity_kind="force",
                unit="N",
                range_minimum=-5_000.0,
                range_maximum=5_000.0,
                correction_offset=1.5,
                correction_gain=1.01,
                standard_uncertainty=2.0,
            ),
        ),
    )


# --- ingress -----------------------------------------------------------


def test_signed_bundle_from_registered_device_is_stored() -> None:
    ingress, keys, _ = make_ingress()
    manifest = make_manifest({"chunk-1": b"raw-a", "chunk-2": b"raw-b"})
    signature = keys.sign(device_id=DEVICE, payload=manifest.signing_bytes())
    refusals = ingress.receive(
        manifest,
        device_id=DEVICE,
        signature=signature,
        chunk_payloads={"chunk-1": b"raw-a", "chunk-2": b"raw-b"},
        now=100,
    )
    assert refusals == ()
    assert ingress.get("bundle-1") is not None


def test_unregistered_device_cannot_upload() -> None:
    ingress, keys, _ = make_ingress()
    manifest = make_manifest({"chunk-1": b"raw-a"})
    refusals = ingress.receive(
        manifest,
        device_id="ghost-device",
        signature="whatever",
        chunk_payloads={"chunk-1": b"raw-a"},
        now=100,
    )
    assert IngressRefusal.UNREGISTERED_DEVICE in refusals
    assert ingress.get("bundle-1") is None


def test_invalid_signature_is_refused() -> None:
    # Acceptance test 2's ingress half: without a valid device
    # signature the bundle never enters the store, so nothing built on
    # it can claim E1.
    ingress, keys, _ = make_ingress()
    manifest = make_manifest({"chunk-1": b"raw-a"})
    refusals = ingress.receive(
        manifest,
        device_id=DEVICE,
        signature="0" * 64,
        chunk_payloads={"chunk-1": b"raw-a"},
        now=100,
    )
    assert refusals == (IngressRefusal.INVALID_SIGNATURE,)


def test_modified_chunk_fails_integrity() -> None:
    ingress, keys, _ = make_ingress()
    manifest = make_manifest({"chunk-1": b"raw-a"})
    signature = keys.sign(device_id=DEVICE, payload=manifest.signing_bytes())
    refusals = ingress.receive(
        manifest,
        device_id=DEVICE,
        signature=signature,
        chunk_payloads={"chunk-1": b"raw-TAMPERED"},
        now=100,
    )
    assert IngressRefusal.CHUNK_HASH_MISMATCH in refusals


def test_resume_is_idempotent_and_conflict_is_named() -> None:
    ingress, keys, _ = make_ingress()
    manifest = make_manifest({"chunk-1": b"raw-a"})
    payloads = {"chunk-1": b"raw-a"}
    signature = keys.sign(device_id=DEVICE, payload=manifest.signing_bytes())
    assert (
        ingress.receive(
            manifest,
            device_id=DEVICE,
            signature=signature,
            chunk_payloads=payloads,
            now=100,
        )
        == ()
    )
    # The interrupted upload retries: same bundle, same content — one copy.
    assert (
        ingress.receive(
            manifest,
            device_id=DEVICE,
            signature=signature,
            chunk_payloads=payloads,
            now=105,
        )
        == ()
    )
    assert ingress.get("bundle-1").received_at == 100
    # Same bundle id, different content: refused by name, not overwritten.
    conflicting = make_manifest({"chunk-1": b"raw-DIFFERENT"})
    refusals = ingress.receive(
        conflicting,
        device_id=DEVICE,
        signature=keys.sign(device_id=DEVICE, payload=conflicting.signing_bytes()),
        chunk_payloads={"chunk-1": b"raw-DIFFERENT"},
        now=110,
    )
    assert IngressRefusal.CONTENT_CONFLICT in refusals


def test_revoked_credentials_block_new_uploads_but_keep_history() -> None:
    # Acceptance test 16.
    ingress, keys, devices = make_ingress()
    manifest = make_manifest({"chunk-1": b"raw-a"})
    signature = keys.sign(device_id=DEVICE, payload=manifest.signing_bytes())
    ingress.receive(
        manifest,
        device_id=DEVICE,
        signature=signature,
        chunk_payloads={"chunk-1": b"raw-a"},
        now=100,
    )
    devices.set_status(DEVICE, DeviceStatus.REVOKED)
    later = make_manifest({"chunk-1": b"raw-a"}).model_copy(
        update={"evidence_bundle_id": "bundle-2"}
    )
    refusals = ingress.receive(
        later,
        device_id=DEVICE,
        signature=keys.sign(device_id=DEVICE, payload=later.signing_bytes()),
        chunk_payloads={"chunk-1": b"raw-a"},
        now=200,
    )
    assert IngressRefusal.CREDENTIAL_REVOKED in refusals
    assert ingress.get("bundle-2") is None
    assert ingress.get("bundle-1") is not None  # history survives revocation


def test_merkle_root_binds_every_chunk() -> None:
    hashes = [chunk_hash(payload) for payload in (b"a", b"b", b"c")]
    root = merkle_root(hashes)
    assert merkle_root(hashes[:2] + [chunk_hash(b"c-modified")]) != root
    assert merkle_root(hashes) == root  # deterministic, odd count included
    with pytest.raises(ValueError):
        merkle_root([])


# --- calibration -------------------------------------------------------


def test_calibration_failures_are_named() -> None:
    registry = CalibrationRegistry()
    registry.register(calibration())
    expired = registry.check(
        "cal-1", quantity_kind="force", unit="N", value=100.0, test_time=3_000
    )
    assert "calibration_expired" in expired.failures
    out_of_range = registry.check(
        "cal-1", quantity_kind="force", unit="N", value=9_000.0, test_time=1_500
    )
    assert "value_outside_calibrated_range" in out_of_range.failures
    wrong_measurand = registry.check(
        "cal-1", quantity_kind="temperature", unit="K", value=300.0, test_time=1_500
    )
    assert "measurand_mismatch" in wrong_measurand.failures
    assert registry.check(
        "cal-1", quantity_kind="force", unit="N", value=100.0, test_time=1_500
    ).valid


def test_revocation_is_timestamped_never_deleted() -> None:
    registry = CalibrationRegistry()
    registry.register(calibration())
    registry.revoke("cal-1", at=1_600)
    # A check anchored before the revocation still answers honestly...
    assert registry.check(
        "cal-1", quantity_kind="force", unit="N", value=100.0, test_time=1_500
    ).valid
    # ...while a check made after it fails by name, record intact.
    after = registry.check(
        "cal-1",
        quantity_kind="force",
        unit="N",
        value=100.0,
        test_time=1_500,
        checked_at=1_700,
    )
    assert "calibration_revoked" in after.failures
    assert registry.get("cal-1") is not None


# --- grades ------------------------------------------------------------


def authenticated_facts(**overrides) -> GradeFacts:
    fields = dict(
        registered_device_identity=True,
        valid_device_signature=True,
        accepted_firmware_state=True,
    )
    fields.update(overrides)
    return GradeFacts(**fields)


def test_expired_calibration_caps_grade_at_e1() -> None:
    # Acceptance test 1: a valid device with an expired calibration
    # cannot create E2 evidence.
    registry = CalibrationRegistry()
    registry.register(calibration())
    check = registry.check(
        "cal-1", quantity_kind="force", unit="N", value=100.0, test_time=3_000
    )
    facts = authenticated_facts(
        valid_calibration_record=check.valid,
        uncertainty_declared=True,
        range_and_measurand_match="value_outside_calibrated_range"
        not in check.failures,
    )
    assert grade(facts) is EvidenceGrade.E1_AUTHENTICATED_DEVICE_OBSERVATION


def test_invalid_signature_caps_grade_at_e0() -> None:
    # Acceptance test 2: a calibrated device with an invalid signature
    # cannot create E1 evidence — higher facts cannot skip lower rungs.
    facts = GradeFacts(
        registered_device_identity=True,
        valid_device_signature=False,
        accepted_firmware_state=True,
        valid_calibration_record=True,
        uncertainty_declared=True,
        range_and_measurand_match=True,
    )
    assert grade(facts) is EvidenceGrade.E0_UNVERIFIED_TELEMETRY


def test_full_ladder_to_e4() -> None:
    facts = authenticated_facts(
        valid_calibration_record=True,
        uncertainty_declared=True,
        range_and_measurand_match=True,
        approved_test_method=True,
        complete_environment_and_specimen_identity=True,
        procedure_conformance=True,
        scope_match=True,
        authorized_signatory=True,
    )
    assert grade(facts) is EvidenceGrade.E4_ACCREDITED_OR_AUTHORIZED_RESULT


def test_grade_at_use_is_flagged_by_revocation_never_rewritten() -> None:
    registry = CalibrationRegistry()
    registry.register(calibration())
    ledger = EvidenceLedger()
    facts = authenticated_facts(
        valid_calibration_record=True,
        uncertainty_declared=True,
        range_and_measurand_match=True,
    )
    use = ledger.record_use(
        evidence_id="obs-1",
        consumer_ref="design-decision-77",
        facts=facts,
        now=1_500,
        basis_calibration_record_ids=("cal-1",),
    )
    assert use.grade_at_use is EvidenceGrade.E2_CALIBRATED_MEASUREMENT
    registry.revoke("cal-1", at=1_600)
    flags = ledger.flag_calibration_revoked("cal-1", now=1_600)
    assert len(flags) == 1
    assert flags[0].use_id == use.use_id
    assert "cal-1" in flags[0].reason
    # The reliance record itself is exactly as taken.
    assert ledger.uses()[0].grade_at_use is EvidenceGrade.E2_CALIBRATED_MEASUREMENT
    unrelated = ledger.flag_calibration_revoked("cal-other", now=1_700)
    assert unrelated == ()


# --- observations and lineage -----------------------------------------


def raw_observation() -> Observation:
    return Observation(
        observation_id="obs-raw-1",
        feature_of_interest_id="damper-42",
        observed_property="force",
        result=ObservationResult(
            value=100.0, unit="N", uncertainty=Uncertainty(standard_uncertainty=5.0)
        ),
        phenomenon_time="2026-07-27T10:00:05Z",
        result_time="2026-07-27T10:05:00Z",
        sensor_instance_id="force-7",
        procedure_id="method-fv",
        specimen_instance_id="damper-42",
        raw_evidence_refs=("chunk-1",),
    )


def test_correction_derives_a_new_artifact_and_raw_stays_unchanged() -> None:
    # Acceptance test 5: raw evidence remains unchanged after
    # calibration correction.
    source = raw_observation()
    ledger = LineageLedger()
    ledger.record(
        ProcessingStep(
            step_id="step-0",
            node_name="construct_observation",
            node_version="1",
            input_refs=("chunk-1",),
            output_ref="obs-raw-1",
        )
    )
    corrected = apply_calibration_correction(
        source,
        calibration(),
        ledger,
        corrected_observation_id="obs-corrected-1",
        step_id="step-1",
    )
    assert corrected.result.value == pytest.approx(100.0 * 1.01 + 1.5)
    assert corrected.result.uncertainty.standard_uncertainty == 2.0
    assert "cal-1" in corrected.calibration_record_ids
    # The source observation is untouched — frozen and unmodified.
    assert source.result.value == 100.0
    assert source.calibration_record_ids == ()


def test_lineage_reconstructs_to_raw_chunks() -> None:
    # Acceptance test 6: every observation can reconstruct its
    # processing lineage.
    ledger = LineageLedger()
    ledger.record(
        ProcessingStep(
            step_id="step-0",
            node_name="construct_observation",
            node_version="1",
            input_refs=("chunk-1", "chunk-2"),
            output_ref="obs-raw-1",
        )
    )
    apply_calibration_correction(
        raw_observation(),
        calibration(),
        ledger,
        corrected_observation_id="obs-corrected-1",
        step_id="step-1",
    )
    chain = ledger.reconstruct(
        "obs-corrected-1", raw_refs=frozenset({"chunk-1", "chunk-2"})
    )
    assert [step.step_id for step in chain] == ["step-0", "step-1"]


def test_broken_lineage_raises_by_name() -> None:
    ledger = LineageLedger()
    ledger.record(
        ProcessingStep(
            step_id="step-1",
            node_name="calibration_correction",
            node_version="1",
            input_refs=("obs-raw-MISSING",),
            output_ref="obs-corrected-1",
        )
    )
    with pytest.raises(LineageError, match="obs-raw-MISSING"):
        ledger.reconstruct("obs-corrected-1", raw_refs=frozenset({"chunk-1"}))


def test_derived_artifacts_cannot_be_reproduced_twice() -> None:
    ledger = LineageLedger()
    step = ProcessingStep(
        step_id="step-1",
        node_name="calibration_correction",
        node_version="1",
        input_refs=("chunk-1",),
        output_ref="obs-1",
    )
    ledger.record(step)
    with pytest.raises(LineageError, match="immutable"):
        ledger.record(step.model_copy(update={"step_id": "step-2"}))


# --- rights ------------------------------------------------------------


def rights(**overrides) -> EvidenceRights:
    fields = dict(
        rights_id="rights-1",
        owner_ids=("supplier-a",),
        evidence_scope=("obs-1",),
        formula_discovery=True,
    )
    fields.update(overrides)
    return EvidenceRights(**fields)


def test_formula_discovery_never_implies_weight_training() -> None:
    grant = rights()
    require_right(grant, "formula_discovery", evidence_id="obs-1", now=100)
    with pytest.raises(RightsError, match="permission_not_granted"):
        require_right(grant, "model_weight_training", evidence_id="obs-1", now=100)


def test_rights_refuse_out_of_scope_expired_and_unknown() -> None:
    grant = rights(valid_until=200)
    with pytest.raises(RightsError, match="evidence_out_of_scope"):
        require_right(grant, "formula_discovery", evidence_id="obs-OTHER", now=100)
    with pytest.raises(RightsError, match="rights_expired"):
        require_right(grant, "formula_discovery", evidence_id="obs-1", now=300)
    with pytest.raises(RightsError, match="unknown_permission"):
        require_right(grant, "world_domination", evidence_id="obs-1", now=100)
