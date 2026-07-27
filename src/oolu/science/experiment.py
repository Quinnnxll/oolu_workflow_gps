"""Closed-loop experiment proposals — the loop closes at the front door.

An approved model's uncertain region generates the next test goal, and
the §23 priority arithmetic ranks it. Two Phase 13 gates are
structural:

* **The proposal is an intent, nothing more.** It carries an R0
  ``ActionIntent`` — a type with no approval, lease, or program field
  to smuggle authorization through. Physical execution re-enters the
  protocol at routing, review, approval, compilation, and arming like
  every other goal.
* **New evidence yields a candidate, never a mutation.**
  ``update_with_evidence`` derives a fresh draft candidate pointing at
  its parent; the existing model version is frozen and the registry
  refuses overwrites.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from ..actuation.contracts import ActionIntent
from .formulas import FormulaCandidate


class ExperimentProposal(BaseModel):
    model_config = ConfigDict(frozen=True)

    proposal_id: str
    world_model_id: str
    world_model_version: str
    uncertainty_region: dict[str, str]
    priority: float
    intent: ActionIntent


def propose_experiment(
    *,
    proposal_id: str,
    world_model_id: str,
    world_model_version: str,
    uncertainty_region: dict[str, str],
    intent: ActionIntent,
    expected_information_gain: float,
    applicability_expansion_value: float,
    product_risk_reduction: float,
    route_reuse_value: float,
    test_cost: float,
    specimen_cost: float,
    machine_time: float,
    safety_risk: float,
) -> ExperimentProposal:
    """Rank the next useful experiment; authorize nothing."""

    priority = (
        expected_information_gain
        + applicability_expansion_value
        + product_risk_reduction
        + route_reuse_value
        - test_cost
        - specimen_cost
        - machine_time
        - safety_risk
    )
    return ExperimentProposal(
        proposal_id=proposal_id,
        world_model_id=world_model_id,
        world_model_version=world_model_version,
        uncertainty_region=uncertainty_region,
        priority=priority,
        intent=intent,
    )


def update_with_evidence(
    candidate: FormulaCandidate,
    *,
    new_candidate_id: str,
    new_training_dataset_version_id: str,
    revised_parameters: dict[str, float],
) -> FormulaCandidate:
    """A revision is a new draft candidate with its parent named."""

    return candidate.model_copy(
        update={
            "candidate_id": new_candidate_id,
            "training_dataset_version_id": new_training_dataset_version_id,
            "parameter_estimates": dict(revised_parameters),
            "parent_candidate_id": candidate.candidate_id,
        }
    )
