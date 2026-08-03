"""SlotIndex — the producers/consumers lookup the route-finding proof conceded.

``route-finding-proof.md`` §1 concedes the assembler's producer choice
"scans the whole library per slot". This is the missing index: one O(N)
pass over a contract library buckets every produced and consumed slot by
``(name, value_type)``, and each lookup re-applies ``Slot.matches`` over
its (small) bucket — the EXACT predicate the scan used, over a pre-narrowed
candidate set, in the same library order. Behavior-identical ranking is the
law here, pinned by a parity test: the index changes what the assembler
*touches*, never what it *picks*.

A projection, not a truth (M1 law): built from the library it was handed,
rebuilt whenever the library changes, never authoritative. A live registry
caller rebuilds per survey/assembly; a stale index is the caller's bug the
same way a stale cache would be.
"""

from __future__ import annotations

from typing import Iterable

from .contract import NodeContract, Slot

__all__ = ["SlotIndex"]


class SlotIndex:
    """Who produces — and who consumes — each slot, by exact vocabulary.

    Buckets key on ``(name, value_type)`` because ``Slot.matches`` requires
    both to agree; the role rule (a role constrains only when BOTH sides
    declare one) is asymmetric, so it is re-checked per lookup rather than
    baked into the key.
    """

    def __init__(self, contracts: Iterable[NodeContract]):
        self._producers: dict[tuple[str, str], list[NodeContract]] = {}
        self._consumers: dict[tuple[str, str], list[NodeContract]] = {}
        for contract in contracts:
            for slot in contract.produces:
                self._bucket(self._producers, slot, contract)
            for slot in contract.consumes:
                self._bucket(self._consumers, slot, contract)

    @staticmethod
    def _bucket(
        index: dict[tuple[str, str], list[NodeContract]],
        slot: Slot,
        contract: NodeContract,
    ) -> None:
        bucket = index.setdefault((slot.name, slot.value_type), [])
        # A contract with two same-name/type slots stays one entry —
        # lookups dedupe by id anyway, but a clean bucket is cheaper.
        if not bucket or bucket[-1].id != contract.id:
            bucket.append(contract)

    def producers(self, slot: Slot) -> list[NodeContract]:
        """Every contract one of whose produced slots satisfies ``slot`` —
        library order, the same order the scan would have found them."""
        out: list[NodeContract] = []
        seen: set[str] = set()
        for contract in self._producers.get((slot.name, slot.value_type), []):
            if contract.id in seen:
                continue
            if any(produced.matches(slot) for produced in contract.produces):
                out.append(contract)
                seen.add(contract.id)
        return out

    def consumers(self, slot: Slot) -> list[NodeContract]:
        """Every contract one of whose consumed slots is satisfied by
        ``slot`` (the produced side) — library order."""
        out: list[NodeContract] = []
        seen: set[str] = set()
        for contract in self._consumers.get((slot.name, slot.value_type), []):
            if contract.id in seen:
                continue
            if any(slot.matches(consumed) for consumed in contract.consumes):
                out.append(contract)
                seen.add(contract.id)
        return out
