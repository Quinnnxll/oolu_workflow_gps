"""Evidence grades E0–E6 — a ladder computed from facts, never asserted.

The plan's central inversion lives here: trust is multidimensional, and
the grade is a *summary* of separately established facts, not a
replacement for them. Two properties are structural:

* **The ladder is cumulative.** A grade holds only if its own
  requirements and every lower grade's requirements hold. A perfect
  device signature (E1's concern) with an expired calibration (E2's
  concern) stops the ladder at E1 — authentication never buys
  metrological validity.
* **Grade at time of use is a record, not a pointer.** When a consumer
  relies on evidence, it records the grade *and its basis* as an
  immutable ``GradeAtUse``. A later calibration revocation flags every
  use whose basis included that record — for review, with names — but
  never rewrites the decision record itself.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict


class EvidenceGrade(str, Enum):
    E0_UNVERIFIED_TELEMETRY = "E0"
    E1_AUTHENTICATED_DEVICE_OBSERVATION = "E1"
    E2_CALIBRATED_MEASUREMENT = "E2"
    E3_CONTROLLED_METHOD_RESULT = "E3"
    E4_ACCREDITED_OR_AUTHORIZED_RESULT = "E4"
    E5_INDEPENDENTLY_REPRODUCED = "E5"
    E6_FORMULA_VALIDATION = "E6"


class GradeFacts(BaseModel):
    """The separately established facts a grade summarizes.

    Every field defaults to ``False``: absence of proof is absence of
    grade. Callers derive these from the modules that own each fact —
    ingress owns the identity and signature facts, the calibration
    registry owns calibration validity, the method layer (R2) owns
    procedure conformance — and never assert them directly from device
    self-reports.
    """

    model_config = ConfigDict(frozen=True)

    # E1 — authenticated device observation
    registered_device_identity: bool = False
    valid_device_signature: bool = False
    accepted_firmware_state: bool = False
    # E2 — calibrated measurement
    valid_calibration_record: bool = False
    uncertainty_declared: bool = False
    range_and_measurand_match: bool = False
    # E3 — controlled method result
    approved_test_method: bool = False
    complete_environment_and_specimen_identity: bool = False
    procedure_conformance: bool = False
    # E4 — accredited or authorized result
    scope_match: bool = False
    authorized_signatory: bool = False
    # E5 — independently reproduced
    independent_actor_or_facility: bool = False
    replication_acceptance_passed: bool = False
    # E6 — formula validation evidence
    holdout_validation: bool = False
    domain_and_unit_constraints: bool = False
    model_uncertainty_declared: bool = False
    scientific_approval: bool = False


_LADDER: tuple[tuple[EvidenceGrade, tuple[str, ...]], ...] = (
    (
        EvidenceGrade.E1_AUTHENTICATED_DEVICE_OBSERVATION,
        (
            "registered_device_identity",
            "valid_device_signature",
            "accepted_firmware_state",
        ),
    ),
    (
        EvidenceGrade.E2_CALIBRATED_MEASUREMENT,
        (
            "valid_calibration_record",
            "uncertainty_declared",
            "range_and_measurand_match",
        ),
    ),
    (
        EvidenceGrade.E3_CONTROLLED_METHOD_RESULT,
        (
            "approved_test_method",
            "complete_environment_and_specimen_identity",
            "procedure_conformance",
        ),
    ),
    (
        EvidenceGrade.E4_ACCREDITED_OR_AUTHORIZED_RESULT,
        ("scope_match", "authorized_signatory"),
    ),
    (
        EvidenceGrade.E5_INDEPENDENTLY_REPRODUCED,
        ("independent_actor_or_facility", "replication_acceptance_passed"),
    ),
    (
        EvidenceGrade.E6_FORMULA_VALIDATION,
        (
            "holdout_validation",
            "domain_and_unit_constraints",
            "model_uncertainty_declared",
            "scientific_approval",
        ),
    ),
)


def grade(facts: GradeFacts) -> EvidenceGrade:
    """The highest grade whose cumulative requirements all hold."""

    achieved = EvidenceGrade.E0_UNVERIFIED_TELEMETRY
    for candidate, requirements in _LADDER:
        if not all(getattr(facts, name) for name in requirements):
            break
        achieved = candidate
    return achieved


class GradeAtUse(BaseModel):
    """One consumer's reliance on one piece of evidence, frozen at use."""

    model_config = ConfigDict(frozen=True)

    use_id: str
    evidence_id: str
    consumer_ref: str
    grade_at_use: EvidenceGrade
    graded_at: int
    basis_facts: GradeFacts
    basis_calibration_record_ids: tuple[str, ...] = ()


class ReviewFlag(BaseModel):
    """A named reason a past use now deserves review — never a rewrite."""

    model_config = ConfigDict(frozen=True)

    use_id: str
    evidence_id: str
    reason: str
    raised_at: int


class EvidenceLedger:
    """Append-only reliance records plus the flags revocation raises.

    There is deliberately no way to update or delete a ``GradeAtUse``:
    a decision taken on since-revoked evidence stays exactly as taken,
    and the flag is the review trigger.
    """

    def __init__(self) -> None:
        self._uses: list[GradeAtUse] = []
        self._flags: list[ReviewFlag] = []
        self._counter = 0

    def record_use(
        self,
        *,
        evidence_id: str,
        consumer_ref: str,
        facts: GradeFacts,
        now: int,
        basis_calibration_record_ids: tuple[str, ...] = (),
    ) -> GradeAtUse:
        self._counter += 1
        use = GradeAtUse(
            use_id=f"use-{self._counter:08d}",
            evidence_id=evidence_id,
            consumer_ref=consumer_ref,
            grade_at_use=grade(facts),
            graded_at=now,
            basis_facts=facts,
            basis_calibration_record_ids=basis_calibration_record_ids,
        )
        self._uses.append(use)
        return use

    def uses(self) -> tuple[GradeAtUse, ...]:
        return tuple(self._uses)

    def flags(self) -> tuple[ReviewFlag, ...]:
        return tuple(self._flags)

    def flag_calibration_revoked(
        self, calibration_record_id: str, *, now: int
    ) -> tuple[ReviewFlag, ...]:
        """Flag every use whose basis included the revoked record."""

        raised: list[ReviewFlag] = []
        for use in self._uses:
            if calibration_record_id in use.basis_calibration_record_ids:
                raised.append(
                    ReviewFlag(
                        use_id=use.use_id,
                        evidence_id=use.evidence_id,
                        reason=(f"calibration_record_revoked:{calibration_record_id}"),
                        raised_at=now,
                    )
                )
        self._flags.extend(raised)
        return tuple(raised)
