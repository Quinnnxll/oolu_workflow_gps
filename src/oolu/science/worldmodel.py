"""Promotion and the world-model registry — approved means earned.

``promote`` is the only door from candidate to world model, and it
demands what the plan's gates 3–5 demand (Phase 12):

* an independent replication split — a strong fit on one facility's
  data refuses with ``independent_replication_required`` (acceptance
  test 11), and a replication facility coupled by shared calibration
  refuses too (acceptance test 14's consequence);
* a passed holdout;
* three named reviewer roles, none of whom may be the discovery node
  that generated the candidate — a formula cannot approve itself
  (Phase 12's first gate).

The registry itself is immutability made durable: a version registers
once; challenge and supersession are statuses with evidence attached;
a challenged or superseded version remains fully retrievable so every
historical decision stays reproducible (acceptance test 12).
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict

from ..durable import DurableConnection
from .dataset import DatasetManifest
from .formulas import CandidateStatus, FormulaCandidateRegistry, StoredCandidate

REQUIRED_REVIEWER_ROLES = (
    "domain_scientist",
    "metrology_reviewer",
    "data_rights_reviewer",
)

_SCHEMA = """CREATE TABLE IF NOT EXISTS protocol_world_models (
    model_id TEXT NOT NULL,
    version TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    status_evidence_ref TEXT,
    registered_at INTEGER NOT NULL,
    PRIMARY KEY (model_id, version)
)"""


class WorldModelVersion(BaseModel):
    model_config = ConfigDict(frozen=True)

    model_id: str
    version: str
    candidate_id: str
    symbolic_expression: str
    parameter_estimates: dict[str, float]
    uncertainty_model: dict[str, float]
    applicability_domain: dict[str, str]
    training_dataset_version_id: str
    validation_dataset_version_id: str
    reviewer_roles: dict[str, str]


class PromotionRefused(ValueError):
    def __init__(self, reasons: tuple[str, ...]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


class WorldModelRegistry:
    def __init__(self, conn: DurableConnection) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def register(self, model: WorldModelVersion, *, now: int) -> None:
        with self._conn.transaction() as db:
            existing = db.execute(
                "SELECT version FROM protocol_world_models"
                " WHERE model_id = ? AND version = ?",
                (model.model_id, model.version),
            ).fetchone()
            if existing is not None:
                raise ValueError(
                    f"world model {model.model_id}@{model.version} exists; "
                    "approved versions are immutable — revise into a new version"
                )
            db.execute(
                "INSERT INTO protocol_world_models VALUES (?, ?, ?, 'approved',"
                " NULL, ?)",
                (model.model_id, model.version, model.model_dump_json(), now),
            )

    def get(self, model_id: str, version: str) -> tuple[WorldModelVersion, str] | None:
        """The version and its status — retrievable in every status."""

        with self._conn.transaction() as db:
            row = db.execute(
                "SELECT payload_json, status FROM protocol_world_models"
                " WHERE model_id = ? AND version = ?",
                (model_id, version),
            ).fetchone()
        if row is None:
            return None
        return (
            WorldModelVersion(**json.loads(row["payload_json"])),
            row["status"],
        )

    def _set_status(
        self, model_id: str, version: str, status: str, evidence_ref: str | None
    ) -> None:
        with self._conn.transaction() as db:
            cursor = db.execute(
                "UPDATE protocol_world_models SET status = ?,"
                " status_evidence_ref = ? WHERE model_id = ? AND version = ?",
                (status, evidence_ref, model_id, version),
            )
            if not cursor.rowcount:
                raise KeyError(f"unknown world model {model_id}@{version}")

    def challenge(self, model_id: str, version: str, *, evidence_ref: str) -> None:
        self._set_status(model_id, version, "challenged", evidence_ref)

    def supersede(self, model_id: str, version: str, *, by_version: str) -> None:
        self._set_status(model_id, version, "superseded", by_version)


def promote(
    stored: StoredCandidate,
    *,
    manifest: DatasetManifest,
    holdout_passed: bool,
    reviewers: dict[str, str],
    registry: WorldModelRegistry,
    candidates: FormulaCandidateRegistry,
    model_id: str,
    version: str,
    now: int,
) -> WorldModelVersion:
    """The one door from candidate to approved world model."""

    candidate = stored.candidate
    reasons: list[str] = []
    if stored.status is not CandidateStatus.SCIENTIFIC_REVIEW:
        reasons.append(f"candidate_not_in_scientific_review:{stored.status.value}")
    if not holdout_passed:
        reasons.append("holdout_validation_failed")
    if not manifest.replication_split:
        reasons.append("independent_replication_required")
    elif not manifest.independent_replication:
        reasons.append("replication_coupled_by_shared_calibration")
    if manifest.dataset_version_id == candidate.training_dataset_version_id:
        reasons.append("validation_dataset_same_as_training")
    for role in REQUIRED_REVIEWER_ROLES:
        if role not in reviewers:
            reasons.append(f"missing_reviewer_role:{role}")
        elif reviewers[role] == candidate.discovery_node:
            reasons.append(f"self_approval_prohibited:{role}")
    if reasons:
        raise PromotionRefused(tuple(reasons))
    model = WorldModelVersion(
        model_id=model_id,
        version=version,
        candidate_id=candidate.candidate_id,
        symbolic_expression=candidate.symbolic_expression,
        parameter_estimates=dict(candidate.parameter_estimates),
        uncertainty_model=dict(candidate.uncertainty_model),
        applicability_domain=dict(candidate.applicability_domain),
        training_dataset_version_id=candidate.training_dataset_version_id,
        validation_dataset_version_id=manifest.dataset_version_id,
        reviewer_roles=dict(reviewers),
    )
    registry.register(model, now=now)
    candidates.set_status(
        candidate.candidate_id,
        CandidateStatus.SUPERSEDED,
        reason=f"promoted:{model_id}@{version}",
    )
    return model


def model_use_gate(
    status: str,
    use: str,
    *,
    independent_validation: bool = False,
    human_review: bool = False,
) -> tuple[str, ...]:
    """§22.1: what a model version in this status may be used for."""

    if use == "advisory":
        return ()
    if use == "engineering_design":
        return () if status == "approved" else (f"model_not_approved:{status}",)
    if use == "safety_critical_execution":
        refusals: list[str] = []
        if status != "approved":
            refusals.append(f"model_not_approved:{status}")
        if not independent_validation:
            refusals.append("independent_validation_required")
        if not human_review:
            refusals.append("human_review_required")
        return tuple(refusals)
    return (f"unknown_use:{use}",)
