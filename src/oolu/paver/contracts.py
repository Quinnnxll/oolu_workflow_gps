"""The Paver's data models — the map, frozen and serializable.

A ``WebEdge`` is one directed connection producer→consumer carrying a
slot. A ``NearMiss`` is a pair the type system ALMOST joins — same
type+role but a different name, or the same name at a convertible type —
a candidate the negotiator (W3) will try to adapt, never an edge yet. A
``RouteWeb`` is a connected component of edges anchored at a node with an
external trigger door (a webhook, a pulse schedule): trigger that anchor
and, once paved, the whole web fires. The ``SurveyReport`` is one tick's
whole map.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["NearMiss", "RouteWeb", "SurveyReport", "WebEdge"]


class WebEdge(BaseModel):
    """One directed producer→consumer connection, by canonical node key.

    ``source``/``target`` are the canonical producer keys (the desk node
    id W0 files under, so a paved edge and the value pipe speak the same
    name). ``slot`` is the slot name carried. ``kind="direct"`` is an
    exact ``Slot.matches``; W3 adds ``"adapted"`` when a synthesized
    adapter bridges a near-miss. ``status`` walks candidate → paved →
    broken → retired as the Paver pours and tests; W1 only ever mints
    ``candidate``."""

    model_config = ConfigDict(frozen=True)

    source: str
    target: str
    slot: str
    value_type: str = "str"
    kind: Literal["direct", "adapted"] = "direct"
    adapter_id: str | None = None
    status: Literal["candidate", "paved", "broken", "retired"] = "candidate"


class NearMiss(BaseModel):
    """A producer/consumer pair the type system almost joins — the
    negotiator's raw material (W3), never an edge on its own. ``reason``
    names why it is not a direct match, in words, so the map is honest
    about what a paver would have to bridge."""

    model_config = ConfigDict(frozen=True)

    source: str  # producer canonical key
    target: str  # consumer canonical key
    produced_slot: str
    consumed_slot: str
    reason: str  # "same type+role, different name" | "same name, convertible type"


class RouteWeb(BaseModel):
    """One connected component of the tenant's node graph, anchored at a
    trigger door. ``anchor`` is the node whose door fans out; ``anchor_kind``
    names the door ("webhook" | "pulse"). ``node_ids`` is every node the
    component touches; ``node_content`` carries each member's registered
    CONTENT hash (P2), so the signature is content-sensitive. ``status=
    "surveying"`` until W2 paves it."""

    model_config = ConfigDict(frozen=True)

    web_id: str
    tenant: str
    anchor: str | None = None
    anchor_kind: str | None = None
    node_ids: list[str] = Field(default_factory=list)
    node_content: dict[str, str] = Field(default_factory=dict)
    edges: list[WebEdge] = Field(default_factory=list)
    near_misses: list[NearMiss] = Field(default_factory=list)
    status: Literal["surveying", "paving", "paved", "stale"] = "surveying"

    @property
    def anchored(self) -> bool:
        return self.anchor is not None

    def signature(self) -> str:
        """A stable content hash of the web's nodes, direct edges, AND each
        member's registered content hash (P2) — two surveys of the SAME
        web share it, so a paved web is skipped next tick; a web whose
        shape OR any member's implementation moved gets a new signature
        and re-paves. A same-id function edit re-paves; a superseded
        promotion is retired by the promotion that replaces it."""
        import hashlib

        payload = "|".join(
            [
                ",".join(sorted(self.node_ids)),
                ";".join(
                    sorted(f"{e.source}>{e.target}:{e.slot}" for e in self.edges)
                ),
                ";".join(
                    f"{key}={value}"
                    for key, value in sorted(self.node_content.items())
                ),
            ]
        )
        return hashlib.sha256(payload.encode()).hexdigest()


class SurveyReport(BaseModel):
    """One survey tick's whole map — what the heartbeat produced and the
    HTTP surface reads back."""

    model_config = ConfigDict(frozen=True)

    tenant: str
    surveyed_nodes: int = 0
    webs: list[RouteWeb] = Field(default_factory=list)

    @property
    def direct_edges(self) -> int:
        return sum(len(web.edges) for web in self.webs)

    @property
    def near_misses(self) -> int:
        return sum(len(web.near_misses) for web in self.webs)

    @property
    def anchored_webs(self) -> int:
        return sum(1 for web in self.webs if web.anchored)
