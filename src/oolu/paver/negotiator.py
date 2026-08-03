"""The negotiator — classify a producer→consumer junction (W3).

A near-miss the survey found is one of three things the Paver can act on:

  * ``direct``     — the ports already match (``Slot.matches``); no adapter.
  * ``mappable``   — same value_type and role, a DIFFERENT NAME: a pure
                     rename, poured as a DETERMINISTIC template, no model.
  * ``convertible``— the SAME NAME at a different value_type: a shape
                     change that needs an authored adapter (the model
                     writes the conversion, verified by execution).
  * ``none``       — the type system offers no bridge; the Paver leaves it.

Model advice (seat ``paver.match``) may PROPOSE which near-misses are
worth pairing, but it never creates an edge — the verdict here is a pure
function of the two slots, and only a passing test (in ``adapters.py``)
ever pours a real adapter. Advice proposes pairs; the type system and
verify-by-execution dispose.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..skills.contract import Slot

__all__ = ["ContractNegotiator", "NegotiationResult"]


@dataclass(frozen=True)
class NegotiationResult:
    """One junction's verdict, with the reason in words."""

    verdict: Literal["direct", "mappable", "convertible", "none"]
    reason: str
    produced_slot: str = ""
    consumed_slot: str = ""


class ContractNegotiator:
    """Classify a producer's output port against a consumer's input —
    pure, deterministic, model-free."""

    def negotiate(self, produced: Slot, consumed: Slot) -> NegotiationResult:
        if produced.matches(consumed):
            return NegotiationResult(
                "direct", "ports already match", produced.name, consumed.name
            )
        role_ok = not (produced.role and consumed.role) or (
            produced.role == consumed.role
        )
        if (
            produced.value_type == consumed.value_type
            and role_ok
            and produced.name != consumed.name
        ):
            return NegotiationResult(
                "mappable",
                f"rename {produced.name!r} -> {consumed.name!r} "
                f"(same {produced.value_type})",
                produced.name,
                consumed.name,
            )
        if (
            produced.name == consumed.name
            and produced.value_type != consumed.value_type
        ):
            return NegotiationResult(
                "convertible",
                f"convert {produced.name!r} from {produced.value_type} to "
                f"{consumed.value_type}",
                produced.name,
                consumed.name,
            )
        return NegotiationResult(
            "none",
            f"no bridge from {produced.name}:{produced.value_type} to "
            f"{consumed.name}:{consumed.value_type}",
            produced.name,
            consumed.name,
        )
