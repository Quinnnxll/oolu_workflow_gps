"""Evidence rights — named, separable permissions that refuse by name.

The §16.1 contract with the plan's one non-negotiable separation kept
structural: ``formula_discovery`` and ``model_weight_training`` are
independent permissions, and no code path here (or anywhere) may treat
one as implying the other. This module is the typed contract and its
gate; durable storage and the wiring into OoLu's grant spine arrive
with R2's registries.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

PERMISSIONS = frozenset(
    {
        "store",
        "internal_use",
        "counterparty_use",
        "aggregate",
        "formula_discovery",
        "route_training",
        "model_weight_training",
        "publish_claims",
        "commercial_license",
    }
)


class RightsError(PermissionError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class EvidenceRights(BaseModel):
    """One owner-granted rights contract over a set of evidence."""

    model_config = ConfigDict(frozen=True)

    rights_id: str
    owner_ids: tuple[str, ...]
    evidence_scope: tuple[str, ...]
    store: bool = False
    internal_use: bool = False
    counterparty_use: bool = False
    aggregate: bool = False
    formula_discovery: bool = False
    route_training: bool = False
    model_weight_training: bool = False
    publish_claims: bool = False
    commercial_license: bool = False
    minimum_aggregation_count: int | None = None
    valid_until: int | None = None


def require_right(
    rights: EvidenceRights, permission: str, *, evidence_id: str, now: int
) -> None:
    """Refuse by name unless ``permission`` is granted, in scope, live."""

    if permission not in PERMISSIONS:
        raise RightsError(f"unknown_permission:{permission}")
    if evidence_id not in rights.evidence_scope:
        raise RightsError(f"evidence_out_of_scope:{evidence_id}")
    if rights.valid_until is not None and now > rights.valid_until:
        raise RightsError(f"rights_expired:{rights.rights_id}")
    if not getattr(rights, permission):
        raise RightsError(f"permission_not_granted:{permission}")
