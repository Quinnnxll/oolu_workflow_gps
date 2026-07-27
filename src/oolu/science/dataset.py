"""The scientific dataset builder — deterministic, rights-checked, named.

Only compatible and authorized evidence enters a dataset, and what was
*excluded* is as much a part of the manifest as what was included:

* rights are checked per observation through the durable store —
  evidence without ``formula_discovery`` permission is excluded by
  name, never silently used (acceptance test 9);
* grade, unit, and method-family walls exclude with reasons (Phase 10's
  third gate);
* duplicate specimens are detected, kept once, and flagged;
* the replication split holds rows from a facility disjoint from the
  training facilities (acceptance test 13) — and shared calibration
  dependencies across facilities are detected *before* independence is
  claimed: if the replication facility shares a calibration record
  with a training facility, the manifest says so and refuses to call
  itself independent (acceptance test 14);
* the content digest is canonical over the definition and the sorted
  membership, so the same inputs rebuild to the same digest, forever
  (Phase 10's second gate).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from ..actuation.digests import canonical_json, sha256_hex
from ..evidence.grades import EvidenceGrade
from ..evidence.rights import RightsError
from ..registries.rights_store import RightsStore

DATASET_DOMAIN = b"oolu-dataset/v1\n"

_GRADE_ORDER = {grade: index for index, grade in enumerate(EvidenceGrade)}


class CandidateObservation(BaseModel):
    model_config = ConfigDict(frozen=True)

    observation_id: str
    variable: str
    value: float
    unit: str
    method_family: str
    facility_id: str
    specimen_id: str
    calibration_record_ids: tuple[str, ...]
    evidence_grade: EvidenceGrade
    rights_id: str


class DatasetDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)

    dataset_id: str
    scientific_question: str
    variables: dict[str, str]  # variable name -> required unit
    minimum_evidence_grade: EvidenceGrade
    allowed_method_families: tuple[str, ...]


class ExcludedObservation(BaseModel):
    model_config = ConfigDict(frozen=True)

    observation_id: str
    reason: str


class DatasetManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    dataset_version_id: str
    dataset_id: str
    included_observation_ids: tuple[str, ...]
    excluded: tuple[ExcludedObservation, ...]
    training_split: tuple[str, ...]
    holdout_split: tuple[str, ...]
    replication_split: tuple[str, ...]
    facilities: tuple[str, ...]
    shared_calibration_pairs: tuple[tuple[str, str], ...]
    independent_replication: bool
    content_digest: str


def _wall_reasons(
    definition: DatasetDefinition,
    observation: CandidateObservation,
    rights: RightsStore,
    now: int,
) -> str | None:
    required_unit = definition.variables.get(observation.variable)
    if required_unit is None:
        return f"variable_not_in_definition:{observation.variable}"
    if observation.unit != required_unit:
        return f"incompatible_unit:{observation.unit}"
    if observation.method_family not in definition.allowed_method_families:
        return f"method_family_not_allowed:{observation.method_family}"
    if (
        _GRADE_ORDER[observation.evidence_grade]
        < _GRADE_ORDER[definition.minimum_evidence_grade]
    ):
        return f"evidence_grade_below_minimum:{observation.evidence_grade.value}"
    try:
        rights.require(
            observation.rights_id,
            "formula_discovery",
            evidence_id=observation.observation_id,
            now=now,
        )
    except RightsError as refusal:
        return f"rights:{refusal.reason}"
    return None


def build_dataset(
    definition: DatasetDefinition,
    observations: tuple[CandidateObservation, ...],
    *,
    rights: RightsStore,
    dataset_version_id: str,
    now: int,
) -> DatasetManifest:
    included: list[CandidateObservation] = []
    excluded: list[ExcludedObservation] = []
    seen_specimens: dict[tuple[str, str], str] = {}

    for observation in sorted(observations, key=lambda o: o.observation_id):
        reason = _wall_reasons(definition, observation, rights, now)
        if reason is None:
            duplicate_key = (observation.specimen_id, observation.variable)
            if duplicate_key in seen_specimens:
                reason = f"duplicate_specimen:{observation.specimen_id}"
            else:
                seen_specimens[duplicate_key] = observation.observation_id
        if reason is not None:
            excluded.append(
                ExcludedObservation(
                    observation_id=observation.observation_id, reason=reason
                )
            )
            continue
        included.append(observation)

    facilities = tuple(sorted({o.facility_id for o in included}))

    # Replication rows come from the lexicographically last facility —
    # deterministic, and disjoint from training by construction.
    replication_facility = facilities[-1] if len(facilities) >= 2 else None
    replication = tuple(
        o.observation_id
        for o in included
        if replication_facility is not None and o.facility_id == replication_facility
    )
    remaining = [o for o in included if o.observation_id not in set(replication)]
    holdout = tuple(o.observation_id for o in remaining[4::5])
    training = tuple(
        o.observation_id for o in remaining if o.observation_id not in set(holdout)
    )

    # Shared calibration detection across facilities (test 14): a
    # calibration record serving two facilities couples their results.
    by_calibration: dict[str, set[str]] = {}
    for observation in included:
        for record_id in observation.calibration_record_ids:
            by_calibration.setdefault(record_id, set()).add(observation.facility_id)
    shared_pairs = tuple(
        sorted(
            (record_id, ",".join(sorted(facility_set)))
            for record_id, facility_set in by_calibration.items()
            if len(facility_set) > 1
        )
    )
    replication_coupled = any(
        replication_facility in pair[1].split(",") for pair in shared_pairs
    )
    independent = bool(replication) and not replication_coupled

    content_digest = sha256_hex(
        DATASET_DOMAIN
        + canonical_json(
            {
                "dataset_id": definition.dataset_id,
                "variables": definition.variables,
                "minimum_evidence_grade": definition.minimum_evidence_grade.value,
                "included": sorted(o.observation_id for o in included),
                "training": sorted(training),
                "holdout": sorted(holdout),
                "replication": sorted(replication),
            }
        )
    )
    return DatasetManifest(
        dataset_version_id=dataset_version_id,
        dataset_id=definition.dataset_id,
        included_observation_ids=tuple(o.observation_id for o in included),
        excluded=tuple(excluded),
        training_split=training,
        holdout_split=holdout,
        replication_split=replication,
        facilities=facilities,
        shared_calibration_pairs=shared_pairs,
        independent_replication=independent,
        content_digest=content_digest,
    )
