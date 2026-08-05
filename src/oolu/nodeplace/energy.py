"""The energy reading — the EBM ask, translated honestly (V5).

One additive scalar per candidate web:

    E(web) = Σ_nodes [ −log(q_i) + λ·cost_i + μ·(1−trust_i) ]
             − ν·cohesion(edges)

where ``q_i`` is the node's SAMPLED posterior success (the same Thompson
draw the picks explore with, so cold start explores by construction and
a freshly published fit node keeps earning draws), cost is the measured
cost EWMA, trust is the reputation signal clamped to [0, 1], and
cohesion is the co-occurrence strength of the web's own pairs
(``links.cohesion_lookup``). Lower is better; the beam minimizes ACROSS
alternative assemblies. Today's ``expected_success`` product is exactly
``e^{−Σ −log q}`` — this is that seam, widened by the cost, trust, and
cohesion terms and made comparable across webs.

The weights are declared constants, not learned knobs: small enough
that verified success stays the dominant term, real enough that a
dearer, less-trusted, never-co-run web must earn its place.
"""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict, Field

# How much energy one unit of measured cost adds — success stays the
# dominant term at everyday prices.
LAMBDA_COST = 0.05
# How much a fully untrusted node (trust 0) adds.
MU_TRUST = 0.30
# How much a perfectly cohesive web (every pair proven together) saves.
NU_COHESION = 0.20
# A sampled probability is clamped away from 0 so one cold node cannot
# push a web's energy to infinity — unproven is expensive, not fatal.
_MIN_Q = 1e-4


class EnergyTerm(BaseModel):
    """One node's contribution to its web's energy."""

    model_config = ConfigDict(frozen=True)

    name: str
    q: float  # sampled posterior success, (0, 1]
    cost: float = 0.0
    trust: float = 1.0  # [0, 1]

    @property
    def value(self) -> float:
        q = min(1.0, max(_MIN_Q, self.q))
        trust = min(1.0, max(0.0, self.trust))
        return -math.log(q) + LAMBDA_COST * max(0.0, self.cost) + MU_TRUST * (
            1.0 - trust
        )


class WebEnergy(BaseModel):
    """A web's full reading: the scalar and the terms it came from."""

    model_config = ConfigDict(frozen=True)

    total: float
    cohesion: float = 0.0
    terms: list[EnergyTerm] = Field(default_factory=list)


def web_energy(terms: list[EnergyTerm], *, cohesion: float = 0.0) -> WebEnergy:
    """The one additive scalar. ``cohesion`` is [0, 1] from
    ``links.cohesion_lookup`` — a web whose pairs finished together
    before reads lower than a stranger web of equal parts."""
    bounded = min(1.0, max(0.0, cohesion))
    total = sum(term.value for term in terms) - NU_COHESION * bounded
    return WebEnergy(total=total, cohesion=bounded, terms=list(terms))
