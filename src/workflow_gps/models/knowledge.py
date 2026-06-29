"""Crowd-shareable knowledge — abstracted, scrub-safe, and intentionally limited.

This is the "Waze map-edit" layer: the system gets smarter as more people drive
the roads, but it shares *map edits*, never *journeys*. Only two categories of
knowledge live here, both carrying zero user data:

  1. DependencyHint  — import-name -> package-name (+ pinned version). Universally
     true, the single highest-value thing to centralise. Turns the import/package
     mismatch problem (trap #6) from a per-user cost into a derived-once fact.

  2. ErrorPattern    — an abstracted error *signature* mapped to the *kind* of fix
     that resolved it. No raw code, no raw stderr, no paths — only the normalised
     signature and a strategy enum.

DELIBERATELY ABSENT: raw synthesized scripts / route templates (#3). Serving one
user's cached code to another inverts the entire threat model — it executes a
stranger's code on your machine and lets a single poisoned contributor fan out
downstream. That category is excluded from the schema by design, not omission.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field, computed_field

from .errors import ErrorClass


class KnowledgeSource(str, Enum):
    """Provenance — drives the trust prior. Crowd knowledge is never trusted on
    arrival; it must earn its score through observed local success."""

    BUILTIN = "builtin"  # shipped static map — highest baseline trust
    LOCAL = "local"  # learned on this machine — earned, trusted
    CROWD = "crowd"  # centralised + abstracted + scrubbed — lowest prior


class RecalcStrategy(str, Enum):
    """The *kind* of route change that resolved an error class. The shareable unit
    of error knowledge — abstract enough to carry no user data."""

    INSTALL_DEPENDENCY = "install_dependency"
    REWRITE_CODE = "rewrite_code"
    BUMP_TEMPERATURE = (
        "bump_temperature"  # trap #5: break out of the identical-broken-code rut
    )
    ESCALATE_TIER = "escalate_tier"
    HALT = "halt"


def _trust(success: int, failure: int, base_prior: float) -> float:
    """Shrink an observed success rate toward a provenance-based prior.

    A single success must not yield trust 1.0. We weight the observation by sample
    size (prior strength = 4 trials) so a hint earns trust gradually. Crowd hints
    therefore start cautious and converge only with corroborating local outcomes.
    """
    total = success + failure
    if total == 0:
        return round(base_prior, 4)
    observed = success / total
    weight = total / (total + 4)  # 0 at no data, -> 1 as evidence accrues
    return round(base_prior * (1 - weight) + observed * weight, 4)


class DependencyHint(BaseModel):
    """#1 — import-name -> package-name. Crowd-shareable; contains zero user data."""

    import_name: str = Field(..., description="What the code imports, e.g. 'cv2'.")
    package_name: str = Field(
        ..., description="What pip/uv must install, e.g. 'opencv-python'."
    )
    pinned_version: str | None = Field(
        default=None, description="Reproducible pin, if known."
    )
    source: KnowledgeSource = KnowledgeSource.LOCAL
    success_count: int = 0
    failure_count: int = 0
    last_seen: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    _BASE_PRIOR = {
        KnowledgeSource.BUILTIN: 0.95,
        KnowledgeSource.LOCAL: 0.60,
        KnowledgeSource.CROWD: 0.50,
    }

    @computed_field  # type: ignore[prop-decorator]
    @property
    def trust_score(self) -> float:
        return _trust(
            self.success_count, self.failure_count, self._BASE_PRIOR[self.source]
        )


class ErrorPattern(BaseModel):
    """#2 — abstracted error signature -> resolving strategy. Scrubbed before storage.

    Carries the normalised `error_signature` (no paths, no literals — see
    errors.normalise_message) and the strategy that worked. Never raw code/stderr.
    """

    error_class: ErrorClass
    error_signature: str = Field(..., description="Normalised, scrub-safe signature.")
    strategy: RecalcStrategy
    source: KnowledgeSource = KnowledgeSource.LOCAL
    success_count: int = 0
    failure_count: int = 0
    last_seen: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    _BASE_PRIOR = {
        KnowledgeSource.BUILTIN: 0.90,
        KnowledgeSource.LOCAL: 0.55,
        KnowledgeSource.CROWD: 0.45,
    }

    @computed_field  # type: ignore[prop-decorator]
    @property
    def trust_score(self) -> float:
        return _trust(
            self.success_count, self.failure_count, self._BASE_PRIOR[self.source]
        )
