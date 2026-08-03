"""Gate-aware paving (W5) — the Paver speaks gates.

A paved web may carry ``guard`` and ``loop`` edges: candidates enter at
PAVE time, ride the composed SubgraphBody through the SAME rehearsal
every web earns promotion by, and are promoted only on a clean pass —
the G-series gate scheduler evaluates them in rehearsal exactly as it
will at every later trigger. A guard-declined child rehearses as an
honest SKIP; a loop that exhausts its budget FAILS the rehearsal loudly
and the web is refused promotion (negative-noted, never paved broken).

The deterministic candidate source is the HUMAN's own rules: an SOP
``require_guard`` projected onto the web's children. Web children are
NODES (every function node's action is ``script/run``), so the SOP's
fnmatch patterns match the child NODE NAMES here — the web-side analog
of matching operations on a route. The route discipline carries over:

  * source + gated child both present  -> a guard edge, poured verbatim.
  * gated child present, source absent -> the web CANNOT express the
    human's rule and is REFUSED (never paved unguarded).
  * gated child absent                 -> the rule does not apply.

Loop candidates travel the same port (the vocabulary and the whole
compose→rehearse→promote→fire pipeline carry them end-to-end); an
AUTOMATIC flaky-adapter retry-loop author is named future work — the
G-series loop is repeat-WHILE-EVIDENCE, and a retry-on-FAILURE loop
needs semantics the gate machinery deliberately does not have yet.

Model advice never mints a gate: candidates come from the human's SOPs
(or an explicit caller), and only a passing rehearsal promotes them —
the same containment discipline as W3's adapters.
"""

from __future__ import annotations

from fnmatch import fnmatchcase

from ..skills.contract import ContractEdge, NodeContract
from ..skills.sop import StandardOperatingProcedure

__all__ = ["derive_gate_candidates"]


def _matching_children(
    pattern: str, children: list[NodeContract]
) -> list[NodeContract]:
    """The children whose NAME matches the SOP pattern — case-folded
    fnmatch, so 'send invoice*' finds the node titled 'Send Invoice'."""
    lowered = pattern.casefold()
    return [
        child
        for child in children
        if fnmatchcase(child.name.casefold(), lowered)
    ]


def derive_gate_candidates(
    sops: list[StandardOperatingProcedure],
    children: list[NodeContract],
) -> tuple[list[ContractEdge], str]:
    """``(edges, "")`` — the guard edges the tenant's SOPs impose on this
    web — or ``([], reason)`` when a rule applies but the web cannot
    EXPRESS it (the gated child is present without its evidence source):
    such a web is refused, never paved unguarded.

    Pure and deterministic: the same SOPs and the same children always
    derive the same edges, in SOP order then child order, deduplicated
    on (source, target). The predicate is carried VERBATIM — the same
    ``Postcondition`` the gate scheduler evaluates."""
    edges: list[ContractEdge] = []
    seen: set[tuple[str, str]] = set()
    for sop in sops:
        for rule in sop.require_guard:
            targets = _matching_children(rule.operation, children)
            if not targets:
                continue  # the gated operation is not in this web
            sources = _matching_children(rule.source, children)
            pairs = [
                (source, target)
                for source in sources
                for target in targets
                if source.id != target.id
            ]
            if not pairs:
                return [], (
                    f"SOP '{sop.name}' guards '{rule.operation}' on "
                    f"evidence from '{rule.source}', which this web "
                    "cannot express — refused, never paved unguarded"
                )
            for source, target in pairs:
                key = (source.id, target.id)
                if key in seen:
                    continue
                edges.append(
                    ContractEdge(
                        source=source.id,
                        target=target.id,
                        relation="guard",
                        guard=rule.when,
                    )
                )
                seen.add(key)
    return edges, ""
