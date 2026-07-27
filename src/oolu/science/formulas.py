"""The formula-discovery sandbox — candidates that cannot crown themselves.

Discovery may propose; it cannot declare truth (§20). Three walls:

* **Dimensional validity is automatic** (acceptance test 10): every
  candidate declares its variables' dimensions and the dimension its
  right-hand side computes to; a mismatch rejects the candidate at the
  screen, mechanically, before any human sees a fit statistic.
* **Failed candidates are stored, not hidden** (Phase 11's third gate):
  rejection is a status with a reason in the registry, and hiding
  failed candidate models is on the plan's prohibited list.
* **There is no path to "approved" here** (Phase 11's first gate): the
  registry's transition whitelist has no edge into approval; promotion
  happens only in ``worldmodel.promote``, which demands replication and
  distinct reviewers.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class Dimension(BaseModel):
    """A physical dimension as base-unit powers, e.g. N = kg·m·s⁻²."""

    model_config = ConfigDict(frozen=True)

    powers: dict[str, int] = Field(default_factory=dict)

    def normalized(self) -> dict[str, int]:
        return {base: power for base, power in self.powers.items() if power != 0}

    def same_as(self, other: "Dimension") -> bool:
        return self.normalized() == other.normalized()

    def times(self, other: "Dimension") -> "Dimension":
        merged = dict(self.powers)
        for base, power in other.powers.items():
            merged[base] = merged.get(base, 0) + power
        return Dimension(powers=merged)


class Term(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    dimension: Dimension
    unit: str


class CandidateStatus(str, Enum):
    GENERATED = "generated"
    SCREENED = "screened"
    EXPERIMENT_REQUIRED = "experiment_required"
    REPLICATION_REQUIRED = "replication_required"
    SCIENTIFIC_REVIEW = "scientific_review"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


_LEGAL: dict[CandidateStatus, frozenset[CandidateStatus]] = {
    CandidateStatus.GENERATED: frozenset(
        {CandidateStatus.SCREENED, CandidateStatus.REJECTED}
    ),
    CandidateStatus.SCREENED: frozenset(
        {
            CandidateStatus.EXPERIMENT_REQUIRED,
            CandidateStatus.REPLICATION_REQUIRED,
            CandidateStatus.SCIENTIFIC_REVIEW,
            CandidateStatus.REJECTED,
        }
    ),
    CandidateStatus.EXPERIMENT_REQUIRED: frozenset(
        {CandidateStatus.SCREENED, CandidateStatus.REJECTED}
    ),
    CandidateStatus.REPLICATION_REQUIRED: frozenset(
        {CandidateStatus.SCIENTIFIC_REVIEW, CandidateStatus.REJECTED}
    ),
    CandidateStatus.SCIENTIFIC_REVIEW: frozenset(
        {CandidateStatus.REJECTED, CandidateStatus.SUPERSEDED}
    ),
    CandidateStatus.REJECTED: frozenset(),
    CandidateStatus.SUPERSEDED: frozenset(),
}


class FormulaCandidate(BaseModel):
    """A proposed relationship, with everything Phase 11 demands declared:
    units, dimensions, uncertainty, applicability, and source dataset."""

    model_config = ConfigDict(frozen=True)

    candidate_id: str
    symbolic_expression: str
    dependent: Term
    independents: tuple[Term, ...]
    computed_rhs_dimension: Dimension
    parameter_estimates: dict[str, float]
    uncertainty_model: dict[str, float]
    applicability_domain: dict[str, str]
    training_dataset_version_id: str
    residual_rmse: float | None
    discovery_node: str
    parent_candidate_id: str | None = None


class StoredCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate: FormulaCandidate
    status: CandidateStatus
    reason: str | None = None


class FormulaCandidateRegistry:
    """Append-only candidates with a whitelisted status machine."""

    def __init__(self) -> None:
        self._stored: dict[str, StoredCandidate] = {}

    def screen(self, candidate: FormulaCandidate) -> StoredCandidate:
        """Register and screen: the dimensional gate runs automatically."""

        if candidate.candidate_id in self._stored:
            raise ValueError(f"candidate {candidate.candidate_id!r} already registered")
        if not candidate.dependent.dimension.same_as(candidate.computed_rhs_dimension):
            stored = StoredCandidate(
                candidate=candidate,
                status=CandidateStatus.REJECTED,
                reason="dimensionally_invalid",
            )
        else:
            stored = StoredCandidate(
                candidate=candidate, status=CandidateStatus.SCREENED
            )
        self._stored[candidate.candidate_id] = stored
        return stored

    def get(self, candidate_id: str) -> StoredCandidate | None:
        return self._stored.get(candidate_id)

    def set_status(
        self, candidate_id: str, status: CandidateStatus, *, reason: str | None = None
    ) -> None:
        stored = self._stored.get(candidate_id)
        if stored is None:
            raise KeyError(f"unknown candidate {candidate_id!r}")
        if status not in _LEGAL[stored.status]:
            raise ValueError(
                f"illegal candidate transition {stored.status.value} -> {status.value}"
            )
        self._stored[candidate_id] = StoredCandidate(
            candidate=stored.candidate, status=status, reason=reason
        )

    def rejected(self) -> tuple[StoredCandidate, ...]:
        """Failed candidates remain stored and searchable — always."""

        return tuple(
            stored
            for stored in self._stored.values()
            if stored.status is CandidateStatus.REJECTED
        )
