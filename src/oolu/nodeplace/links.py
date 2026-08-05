"""The link dimensions, joined into one candidate graph (V5).

Beside the paver's deterministic slot edges (untouched — ``Slot.matches``
is physics and stays exact), a candidate has three more STORED relations
worth reading together:

- **usage co-occurrence** — which versions actually ran together, at
  what outcome (``TraceStore.version_cooccurrence``, outcome-aware) and
  which kept being bound to the same runs
  (``AttributionStore.version_pairs``, count-only);
- **lineage** — the recorded derivation chain
  (``NodeVersion.lineage``, immutable since contribution);
- **class** — the market classification (``classify_listing``), the
  substitutes dimension.

``candidate_graph`` joins them into one read-only picture, and
``cohesion_lookup`` folds the co-occurrence dimension into the single
bounded scalar the web energy's ``ν·cohesion`` term reads. The embedding
dimension is deliberately NOT an edge kind here: law 4 — models (and
embeddings) advise RANKING; they never mint edges.
"""

from __future__ import annotations

from typing import Callable, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .economics import classify_listing

# A class with more members than this emits no pairwise class edges —
# the membership is on the nodes; pairwise edges over a big class would
# be noise squared.
_CLASS_EDGE_CAP = 12

# A pair must have run together this often before its strength stops
# being discounted — three shared runs is history, one is coincidence.
_PROVEN_TOGETHER = 3


class LinkNode(BaseModel):
    model_config = ConfigDict(frozen=True)

    version_id: str
    node_id: str
    title: str
    class_key: str


class LinkEdge(BaseModel):
    """One typed relation between two candidate versions."""

    model_config = ConfigDict(frozen=True)

    source: str  # version_id
    target: str  # version_id
    kind: str  # "cooccurrence" | "lineage" | "class"
    # cooccurrence: how often together / how often that ended verified.
    together: int = 0
    successes: int = 0
    # lineage: the ancestor's level (1 = direct parent).
    level: int = 0


class LinkGraph(BaseModel):
    model_config = ConfigDict(frozen=True)

    nodes: list[LinkNode] = Field(default_factory=list)
    edges: list[LinkEdge] = Field(default_factory=list)


def pair_strength(together: int, successes: int) -> float:
    """One pair's cohesion contribution, 0..1: its verified success rate,
    discounted while the shared history is thin — a pair that ran
    together once is closer to coincidence than to a road."""
    if together <= 0:
        return 0.0
    rate = successes / together
    return rate * min(1.0, together / _PROVEN_TOGETHER)


def cohesion_lookup(
    traces=None, attribution=None
) -> Callable[[Sequence[str]], float]:
    """The web energy's cohesion hand: mean pair strength over a web's
    version pairs. Outcome-aware trace pairs speak first; binding-only
    pairs (no outcome recorded) count at half a proven pair's weight —
    being chosen together is evidence, just weaker than finishing
    together. Missing stores mean cohesion 0 — the term never invents."""
    trace_pairs: dict[tuple[str, str], tuple[int, int]] = {}
    binding_pairs: dict[tuple[str, str], int] = {}
    if traces is not None:
        try:
            trace_pairs = traces.version_cooccurrence()
        except Exception:  # noqa: BLE001 - cohesion is a bonus, never a wall
            trace_pairs = {}
    if attribution is not None:
        try:
            binding_pairs = attribution.version_pairs()
        except Exception:  # noqa: BLE001
            binding_pairs = {}

    def cohesion(version_ids: Sequence[str]) -> float:
        versions = sorted(set(v for v in version_ids if v))
        if len(versions) < 2:
            return 0.0
        total = 0.0
        pairs = 0
        for i, a in enumerate(versions):
            for b in versions[i + 1 :]:
                pairs += 1
                counted = trace_pairs.get((a, b))
                if counted is not None:
                    total += pair_strength(*counted)
                    continue
                bound = binding_pairs.get((a, b), 0)
                if bound:
                    total += 0.5 * min(1.0, bound / _PROVEN_TOGETHER)
        return total / pairs if pairs else 0.0

    return cohesion


def candidate_graph(
    registry,
    *,
    traces=None,
    attribution=None,
    query: str = "",
    limit: int = 200,
) -> LinkGraph:
    """The one candidate graph: discovery's versions as nodes, the three
    stored link dimensions as typed edges. Read-only, bounded (discovery
    is indexed and the co-occurrence reads cap themselves), and exactly
    as honest as its stores — an empty dimension draws nothing."""
    listings = registry.discover(query)[: max(1, int(limit))]
    nodes: list[LinkNode] = []
    version_ids: set[str] = set()
    by_class: dict[str, list[str]] = {}
    for listing in listings:
        version = registry.get_version(listing.version_id)
        if version is None:
            continue
        _node_class, class_key = classify_listing(listing.tags, listing.title)
        nodes.append(
            LinkNode(
                version_id=listing.version_id,
                node_id=version.node_id,
                title=listing.title,
                class_key=class_key,
            )
        )
        version_ids.add(listing.version_id)
        by_class.setdefault(class_key, []).append(listing.version_id)

    edges: list[LinkEdge] = []
    # Co-occurrence: pairs inside the graph, from either store.
    trace_pairs = (
        traces.version_cooccurrence() if traces is not None else {}
    )
    binding_pairs = (
        attribution.version_pairs() if attribution is not None else {}
    )
    seen_pairs: set[tuple[str, str]] = set()
    for (a, b), (together, successes) in trace_pairs.items():
        if a in version_ids and b in version_ids:
            edges.append(
                LinkEdge(
                    source=a,
                    target=b,
                    kind="cooccurrence",
                    together=together,
                    successes=successes,
                )
            )
            seen_pairs.add((a, b))
    for (a, b), together in binding_pairs.items():
        if (a, b) in seen_pairs:
            continue
        if a in version_ids and b in version_ids:
            edges.append(
                LinkEdge(
                    source=a, target=b, kind="cooccurrence", together=together
                )
            )
    # Lineage: each graph version's recorded ancestors, when also present.
    for node in nodes:
        version = registry.get_version(node.version_id)
        if version is None:
            continue
        for record in version.lineage:
            if record.ancestor_version_id in version_ids:
                edges.append(
                    LinkEdge(
                        source=record.ancestor_version_id,
                        target=node.version_id,
                        kind="lineage",
                        level=record.level,
                    )
                )
    # Class: pairwise only within small classes; membership itself rides
    # on every node's class_key.
    for _class_key, members in by_class.items():
        if len(members) < 2 or len(members) > _CLASS_EDGE_CAP:
            continue
        for i, a in enumerate(members):
            for b in members[i + 1 :]:
                edges.append(LinkEdge(source=a, target=b, kind="class"))
    return LinkGraph(nodes=nodes, edges=edges)
