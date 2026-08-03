"""The survey — discover which nodes could feed which, and group them.

``WebSurveyor.survey`` is a PURE function of the tenant's contracts and
their trigger doors: it builds a :class:`SlotIndex` over the contracts,
emits a ``direct`` candidate edge wherever a producer's output port
exactly matches a consumer's input (``Slot.matches``), records the
near-misses the type system almost joins, groups the whole into connected
components, and anchors each component at a node carrying an external
trigger door. No code is authored, no adapter is built, nothing is run —
the survey draws the map W2+ paves.

Two disciplines carry over from W0's decisions:

  * Only producers that FILE PORT VALUES form candidate edges — a node
    whose function is a ``script`` action (every function and program
    node). A demonstrated/marketplace ActionsBody outcome (cli, http,
    browser) need not carry a result payload, so an ``output://`` edge
    from one could never fill — a candidate that can never be paved is
    worse than no candidate. Consumers may be any body.

  * Edges key by the CANONICAL producer key (the desk node id the
    single-node path files under), so a surveyed edge and W0's value pipe
    name the same port. The caller supplies each node's canonical key.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid5, NAMESPACE_URL

from ..skills.contract import ActionsBody, NodeContract, ScriptBody, Slot
from ..skills.index import SlotIndex
from .contracts import NearMiss, RouteWeb, SurveyReport, WebEdge

__all__ = ["SurveyNode", "WebSurveyor"]


def _files_port_values(contract: NodeContract) -> bool:
    """True iff this producer's success files port values W0 could bind
    against — a ScriptBody, or an ActionsBody whose SOLE action is a
    ``script`` action (the shape every function and program node
    publishes, and exactly the shape the compiler can wire — a mixed
    ``[http, script]`` body neither wires nor rehearses effect-free, so
    it must not source a candidate edge either). A cli/http/browser skill
    files nothing to bind."""
    body = contract.body
    if isinstance(body, ScriptBody):
        return True
    if isinstance(body, ActionsBody):
        return len(body.actions) == 1 and body.actions[0].adapter == "script"
    return False


@dataclass(frozen=True)
class SurveyNode:
    """One node offered to the survey: its contract, its canonical key,
    and its trigger door (if any). ``anchor_kind`` is None for a node with
    no external door — it can be a station on a web, never its anchor."""

    key: str  # canonical producer key (desk node id)
    contract: NodeContract
    anchor_kind: str | None = None  # "webhook" | "pulse" | None


def _near_miss_reason(produced: Slot, consumed: Slot) -> str | None:
    """Why a producer's port ALMOST feeds a consumer's input — or None
    when the pair is unrelated. A direct match is not a near-miss and
    returns None here (the caller checks ``matches`` first).

    The "different name" case demands a SHARED, DECLARED role — otherwise
    every ``str`` port would near-miss every ``str`` input and the map
    would drown in noise. A shared role ("path", "url", …) is the signal
    that two differently-named slots are the same KIND of thing, a bridge
    a rename adapter could pave. The "same name" case needs no role: the
    name is already the signal, the type is what an adapter converts."""
    if (
        produced.value_type == consumed.value_type
        and produced.name != consumed.name
        and produced.role
        and consumed.role
        and produced.role == consumed.role
    ):
        return "same type+role, different name"
    if produced.name == consumed.name and produced.value_type != consumed.value_type:
        return "same name, convertible type"
    return None


class _Components:
    """Union-find over node keys — connected components of the edge graph."""

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def add(self, key: str) -> None:
        self._parent.setdefault(key, key)

    def find(self, key: str) -> str:
        self.add(key)
        root = key
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[key] != root:  # path compression
            self._parent[key], key = root, self._parent[key]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for key in self._parent:
            out.setdefault(self.find(key), []).append(key)
        return out


@dataclass
class WebSurveyor:
    """Survey a tenant's nodes into a web map — deterministic and pure."""

    def survey(self, tenant: str, nodes: list[SurveyNode]) -> SurveyReport:
        by_key = {node.key: node for node in nodes}
        # The index is a projection over THIS survey's contracts, rebuilt
        # every tick, never authoritative (the M1 law).
        index = SlotIndex(node.contract for node in nodes)
        producer_key = {node.contract.id: node.key for node in nodes}

        edges: list[WebEdge] = []
        near: list[NearMiss] = []
        seen_edges: set[tuple[str, str, str]] = set()
        seen_near: set[tuple[str, str, str, str]] = set()
        components = _Components()
        for node in nodes:
            components.add(node.key)

        for consumer in nodes:
            for slot in consumer.contract.consumes:
                # Direct: a producer's port EXACTLY matches this input.
                for producer in index.producers(slot):
                    if producer.id == consumer.contract.id:
                        continue
                    if not _files_port_values(producer):
                        continue  # only fileable producers form edges
                    src = producer_key.get(producer.id)
                    if src is None:
                        continue
                    marker = (src, consumer.key, slot.name)
                    if marker in seen_edges:
                        continue
                    seen_edges.add(marker)
                    edges.append(
                        WebEdge(
                            source=src,
                            target=consumer.key,
                            slot=slot.name,
                            value_type=slot.value_type,
                        )
                    )
                    components.union(src, consumer.key)
                # Near-miss: an ALMOST-match the negotiator (W3) may bridge.
                for producer in nodes:
                    if producer.contract.id == consumer.contract.id:
                        continue
                    if not _files_port_values(producer.contract):
                        continue
                    for produced in producer.contract.produces:
                        if produced.matches(slot):
                            continue  # a direct match, already an edge
                        reason = _near_miss_reason(produced, slot)
                        if reason is None:
                            continue
                        key = (
                            producer.key,
                            consumer.key,
                            produced.name,
                            slot.name,
                        )
                        if key in seen_near:
                            continue
                        seen_near.add(key)
                        near.append(
                            NearMiss(
                                source=producer.key,
                                target=consumer.key,
                                produced_slot=produced.name,
                                consumed_slot=slot.name,
                                reason=reason,
                            )
                        )
                        # A near-miss is a candidate bridge — the two nodes
                        # ARE related (the survey's whole job), so they share
                        # a web even before an adapter paves the gap.
                        components.union(producer.key, consumer.key)

        return SurveyReport(
            tenant=tenant,
            surveyed_nodes=len(nodes),
            webs=self._assemble_webs(tenant, by_key, edges, near, components),
        )

    def _assemble_webs(
        self,
        tenant: str,
        by_key: dict[str, SurveyNode],
        edges: list[WebEdge],
        near: list[NearMiss],
        components: _Components,
    ) -> list[RouteWeb]:
        groups = components.groups()
        edges_by_root: dict[str, list[WebEdge]] = {}
        for edge in edges:
            edges_by_root.setdefault(components.find(edge.source), []).append(edge)
        near_by_root: dict[str, list[NearMiss]] = {}
        for miss in near:
            near_by_root.setdefault(components.find(miss.source), []).append(miss)

        webs: list[RouteWeb] = []
        for root, members in sorted(groups.items()):
            web_edges = edges_by_root.get(root, [])
            web_near = near_by_root.get(root, [])
            # A lone node with no edge AND no near-miss is not a web — the
            # map holds only nodes that could connect to something.
            if not web_edges and not web_near:
                continue
            anchors = sorted(
                key for key in members if by_key[key].anchor_kind is not None
            )
            anchor = anchors[0] if anchors else None
            anchor_kind = by_key[anchor].anchor_kind if anchor else None
            # Deterministic id from the tenant + the sorted members, so the
            # same shape keeps the same web_id across ticks (the map is
            # stable to inspect, not reshuffled every survey).
            web_id = uuid5(
                NAMESPACE_URL, f"paver:{tenant}:" + ",".join(sorted(members))
            ).hex
            webs.append(
                RouteWeb(
                    web_id=web_id,
                    tenant=tenant,
                    anchor=anchor,
                    anchor_kind=anchor_kind,
                    node_ids=sorted(members),
                    edges=sorted(
                        web_edges, key=lambda e: (e.source, e.target, e.slot)
                    ),
                    near_misses=sorted(
                        web_near,
                        key=lambda m: (m.source, m.target, m.consumed_slot),
                    ),
                )
            )
        return webs
