"""Observations, processing lineage, and corrections as new artifacts.

The canonical observation follows the sensor-ontology shape — feature,
property, result with unit and uncertainty, phenomenon and result time,
sensor, procedure — and carries its provenance: which raw chunks it
derives from, through which processing steps, under which calibration
records.

Lineage is a ledger of processing steps keyed by what they *produced*.
Reconstruction walks backward from any artifact to raw chunk refs; a
ref nobody produced and no manifest declares is a ``LineageError``, not
a silent gap. Calibration correction is the worked example of the
"corrections are new artifacts" invariant: it emits a derived
observation plus the step that made it, and touches nothing upstream.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .calibration import CalibrationRecord
from .grades import EvidenceGrade


class Uncertainty(BaseModel):
    model_config = ConfigDict(frozen=True)

    standard_uncertainty: float | None = None
    expanded_uncertainty: float | None = None
    coverage_factor: float | None = None

    @property
    def declared(self) -> bool:
        return (
            self.standard_uncertainty is not None
            or self.expanded_uncertainty is not None
        )


class ObservationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    value: float | tuple[float, ...]
    unit: str
    uncertainty: Uncertainty | None = None


class ProcessingStep(BaseModel):
    """One versioned transformation: inputs in, one artifact out."""

    model_config = ConfigDict(frozen=True)

    step_id: str
    node_name: str
    node_version: str
    input_refs: tuple[str, ...]
    output_ref: str
    parameters: dict[str, str | int | float | bool] = Field(default_factory=dict)


class Observation(BaseModel):
    model_config = ConfigDict(frozen=True)

    observation_id: str
    feature_of_interest_id: str
    observed_property: str
    result: ObservationResult
    phenomenon_time: str
    result_time: str
    sensor_instance_id: str
    procedure_id: str
    specimen_instance_id: str
    calibration_record_ids: tuple[str, ...] = ()
    raw_evidence_refs: tuple[str, ...] = ()
    quality_flags: tuple[str, ...] = ()
    evidence_grade: EvidenceGrade = EvidenceGrade.E0_UNVERIFIED_TELEMETRY
    signature_verification_state: str = "unknown"  # valid | invalid | unknown


class LineageError(ValueError):
    pass


class LineageLedger:
    """Append-only processing steps, replayable from raw refs upward."""

    def __init__(self) -> None:
        self._by_output: dict[str, ProcessingStep] = {}

    def record(self, step: ProcessingStep) -> None:
        if step.output_ref in self._by_output:
            raise LineageError(
                f"artifact {step.output_ref!r} already has a producing step; "
                "derived artifacts are immutable"
            )
        self._by_output[step.output_ref] = step

    def reconstruct(
        self, ref: str, *, raw_refs: frozenset[str]
    ) -> tuple[ProcessingStep, ...]:
        """Every step between ``ref`` and the raw chunks, leaves first.

        Terminal inputs must be declared raw refs (chunk ids from a
        stored manifest); anything else is a broken chain.
        """

        ordered: list[ProcessingStep] = []
        seen: set[str] = set()

        def walk(current: str) -> None:
            if current in raw_refs:
                return
            step = self._by_output.get(current)
            if step is None:
                raise LineageError(
                    f"artifact {current!r} has no producing step and is "
                    "not a raw evidence ref"
                )
            if current in seen:
                return
            seen.add(current)
            for input_ref in step.input_refs:
                walk(input_ref)
            ordered.append(step)

        walk(ref)
        return tuple(ordered)


def apply_calibration_correction(
    source: Observation,
    calibration: CalibrationRecord,
    ledger: LineageLedger,
    *,
    corrected_observation_id: str,
    step_id: str,
) -> Observation:
    """Derive a corrected observation; the source remains untouched.

    The correction model (offset and gain) comes from the calibration
    record's measurand entry for the source's property and unit, the
    step is recorded in the ledger, and the derived observation carries
    both the calibration record and the source in its provenance.
    """

    if not isinstance(source.result.value, float):
        raise LineageError("correction of array results arrives with R2's pipeline")
    entry = calibration.measurand(source.observed_property, source.result.unit)
    if entry is None:
        raise LineageError(
            f"calibration {calibration.calibration_record_id!r} has no "
            f"measurand for {source.observed_property!r} in "
            f"{source.result.unit!r}"
        )
    corrected_value = source.result.value * entry.correction_gain + (
        entry.correction_offset
    )
    uncertainty = source.result.uncertainty
    if entry.standard_uncertainty is not None:
        uncertainty = Uncertainty(standard_uncertainty=entry.standard_uncertainty)
    ledger.record(
        ProcessingStep(
            step_id=step_id,
            node_name="calibration_correction",
            node_version="1",
            input_refs=(source.observation_id,),
            output_ref=corrected_observation_id,
            parameters={
                "calibration_record_id": calibration.calibration_record_id,
                "correction_offset": entry.correction_offset,
                "correction_gain": entry.correction_gain,
            },
        )
    )
    return source.model_copy(
        update={
            "observation_id": corrected_observation_id,
            "result": ObservationResult(
                value=corrected_value,
                unit=source.result.unit,
                uncertainty=uncertainty,
            ),
            "calibration_record_ids": (
                *source.calibration_record_ids,
                calibration.calibration_record_id,
            ),
        }
    )
