"""Physical-state-delta qualification and failure disposition.

A machine claiming "I did it" is telemetry; a state delta is *qualified*
only when an independent inspection route confirms each expected
property (§24.5 gate 13). The qualified record carries the job digest —
which binds intent, plan, approvals, program, machine, tool, fixture,
and frames — plus the execution traces and inspection observations, so
Phase 8's first exit gate (every accepted change traces all the way
back) is a matter of reading fields, not reconstructing history.

Failure disposition is a closed vocabulary (acceptance test 30): an
irreversible failure routes to a disposition path the capability
*declared* — rework, quarantine, scrap, or containment. "Rollback" is
not a member of the ``Disposition`` enum and the router refuses the
word by name; physics does not have an undo stack.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from ..actuation.contracts import Disposition, ExecutionEvidenceBundle


class StateDeltaRefused(ValueError):
    def __init__(self, reasons: tuple[str, ...]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


class InspectionFinding(BaseModel):
    """One independently observed property of the post-state."""

    model_config = ConfigDict(frozen=True)

    observation_id: str
    observed_property: str
    confirmed: bool


class QualifiedStateDelta(BaseModel):
    model_config = ConfigDict(frozen=True)

    delta_id: str
    job_digest: str
    program_digest: str
    pre_state_id: str
    post_state_id: str
    verified_properties: tuple[str, ...]
    inspection_observation_ids: tuple[str, ...]
    commanded_trace_ref: str
    actual_trace_ref: str
    disposition: Disposition

    def lineage_refs(self) -> dict[str, str | tuple[str, ...]]:
        """The backward trace, digest-deep: the job digest binds intent,
        plan, approvals, machine, tool, fixture, and frames."""

        return {
            "job_digest": self.job_digest,
            "program_digest": self.program_digest,
            "commanded_trace_ref": self.commanded_trace_ref,
            "actual_trace_ref": self.actual_trace_ref,
            "inspection_observation_ids": self.inspection_observation_ids,
            "pre_state_id": self.pre_state_id,
            "post_state_id": self.post_state_id,
        }


def qualify_state_delta(
    bundle: ExecutionEvidenceBundle,
    findings: tuple[InspectionFinding, ...],
    *,
    delta_id: str,
    expected_properties: tuple[str, ...],
    post_state_id: str,
) -> QualifiedStateDelta:
    """Qualify a claimed state change against independent inspection."""

    reasons: list[str] = []
    if not findings:
        reasons.append("no_independent_verification")
    confirmed = {f.observed_property for f in findings if f.confirmed}
    disputed = {f.observed_property for f in findings if not f.confirmed}
    for prop in expected_properties:
        if prop in disputed:
            reasons.append(f"disputed_property:{prop}")
        elif prop not in confirmed:
            reasons.append(f"unverified_property:{prop}")
    if reasons:
        raise StateDeltaRefused(tuple(reasons))
    return QualifiedStateDelta(
        delta_id=delta_id,
        job_digest=bundle.job_digest,
        program_digest=bundle.program_digest,
        pre_state_id=bundle.pre_workpiece_state_id,
        post_state_id=post_state_id,
        verified_properties=tuple(sorted(expected_properties)),
        inspection_observation_ids=tuple(f.observation_id for f in findings),
        commanded_trace_ref=bundle.commanded_trace_ref,
        actual_trace_ref=bundle.actual_trace_ref,
        disposition=bundle.disposition,
    )


def route_failure(
    *,
    declared_disposition_paths: tuple[str, ...],
    requested_path: str,
) -> Disposition:
    """Route a failed physical operation to a declared disposition.

    Refuses "rollback" by name, refuses undeclared paths, and returns a
    member of the closed ``Disposition`` vocabulary — never an inverse
    motion (acceptance test 30, recovery policy §24.9).
    """

    if requested_path in ("rollback", "inverse_motion", "undo"):
        raise StateDeltaRefused(("generic_rollback_is_not_a_disposition",))
    if requested_path not in declared_disposition_paths:
        raise StateDeltaRefused((f"undeclared_disposition_path:{requested_path}",))
    mapping = {
        "rework": Disposition.REWORKED,
        "quarantine": Disposition.QUARANTINED,
        "scrap": Disposition.SCRAPPED,
        "contain": Disposition.CONTAINED,
        "containment": Disposition.CONTAINED,
    }
    disposition = mapping.get(requested_path)
    if disposition is None:
        raise StateDeltaRefused((f"unknown_disposition_path:{requested_path}",))
    return disposition
