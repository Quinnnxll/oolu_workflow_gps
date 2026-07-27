"""Safety-function validation — physical evidence or nothing.

Invariant 19 and acceptance test 34, as a gate: a digital twin, an AI
risk score, or a passing simulation is *supporting* evidence; a safety
function (emergency stop, guard interlock, safe speed, energy
isolation) is validated only by physical test evidence at the cell.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

PHYSICAL_KINDS = ("physical_test",)
SUPPORTING_KINDS = ("simulation", "analysis", "digital_twin", "risk_score")


class ValidationEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    evidence_id: str
    kind: str  # physical_test | simulation | analysis | digital_twin | risk_score


def validate_safety_function(
    safety_function_id: str, evidence: tuple[ValidationEvidence, ...]
) -> tuple[str, ...]:
    """Named reasons the safety function is not validated. Empty means
    validated — and that always required at least one physical test."""

    if not evidence:
        return (f"no_validation_evidence:{safety_function_id}",)
    unknown = [
        e.kind for e in evidence if e.kind not in PHYSICAL_KINDS + SUPPORTING_KINDS
    ]
    if unknown:
        return tuple(f"unknown_evidence_kind:{kind}" for kind in unknown)
    if not any(e.kind in PHYSICAL_KINDS for e in evidence):
        return ("simulation_alone_cannot_validate_safety_function",)
    return ()
