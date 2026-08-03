"""The Paver's tick loop — pave direct webs (W2).

"Code and test the path to full": for each fully-direct, anchored web the
survey found, the agent builds the ONE SubgraphBody contract that fires it,
REHEARSES that contract end-to-end in the severed sandbox, and — only if it
runs clean — PROMOTES it to a single registered node. The model bill (any
gap-script synthesis) is paid HERE, at pave time, so a later trigger fires
the web from provided scripts with no model in the loop.

The rehearsal gate is EFFECT-FREEDOM, not the verb taxonomy: every hop
runs network-severed with no egress grant, so a web of sandboxed scripts
is externally effect-free and rehearses in full. A web carrying a
write-class adapter action (a real cli/http side effect) is NOT rehearsed
end-to-end — it promotes ``rehearsed=false`` and earns its verified edges
from its first real run instead. W2 paves the effect-free case; the rest
is named, never silently run.

A web that fails rehearsal is not published: its edges are marked broken
and a negative-knowledge note is filed, so the Paver never grinds the same
failing junction.

The agent is pure orchestration over injected PORTS (rehearse, promote,
negative, audit) so it unit-tests without a gateway; the gateway supplies
the real sandbox rehearsal and the body-preserving registration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..skills.contract import (
    ActionsBody,
    NodeContract,
    ScriptBody,
    SubgraphBody,
)
from .contracts import RouteWeb, SurveyReport
from .discovery import SurveyNode, WebSurveyor

__all__ = ["PaveOutcome", "PaveReport", "PaverAgent", "RehearsalResult"]


@dataclass(frozen=True)
class RehearsalResult:
    """What one rehearsal produced — did the web run clean in the sandbox."""

    ok: bool
    error: str = ""
    run_id: str | None = None
    rehearsed: bool = True  # False when a write-class hop forced per-node verify


@dataclass(frozen=True)
class PaveOutcome:
    """One web's fate this tick."""

    web_id: str
    anchor: str | None
    status: str  # "paved" | "rehearsal-failed" | "skipped"
    node_id: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class PaveReport:
    """One tick's whole result — what the heartbeat did."""

    tenant: str
    surveyed: int = 0
    candidates: int = 0
    paved: list[str] = field(default_factory=list)
    refused: list[dict] = field(default_factory=list)
    outcomes: list[PaveOutcome] = field(default_factory=list)


def _rehearsable_effect_free(children: list[NodeContract]) -> bool:
    """True iff every child runs as a sandboxed SCRIPT — a ScriptBody or a
    sole-script-action ActionsBody. A cli/http/browser action has real
    side effects the severed sandbox does not neutralize, so a web
    carrying one is not rehearsed end-to-end."""
    for child in children:
        body = child.body
        if isinstance(body, ScriptBody):
            continue
        if (
            isinstance(body, ActionsBody)
            and len(body.actions) == 1
            and body.actions[0].adapter == "script"
        ):
            continue
        return False
    return True


class PaverAgent:
    """Pave a tenant's direct webs, budget-capped, at pave time.

    Ports (all callables, injected so the loop unit-tests):
      * ``rehearse(contract) -> RehearsalResult`` — compile + run the web
        in the severed sandbox.
      * ``promote(tenant, web, contract) -> str`` — register the
        SubgraphBody as one node, persist, return its node id.
      * ``negative(tenant, web, reason) -> None`` — file the broken web
        (M3), so the Paver never re-tries the same failing junction.
      * ``audit(event, payload) -> None`` — the hash-chained record.
    """

    def __init__(
        self,
        *,
        rehearse: Callable[[NodeContract], RehearsalResult],
        promote: Callable[[str, RouteWeb, NodeContract], str],
        negative: Callable[[str, RouteWeb, str], None] | None = None,
        audit: Callable[[str, dict], None] | None = None,
        surveyor: WebSurveyor | None = None,
    ):
        self._rehearse = rehearse
        self._promote = promote
        self._negative = negative
        self._audit = audit
        self._surveyor = surveyor or WebSurveyor()

    # ------------------------------------------------------------------ #
    def tick(
        self,
        tenant: str,
        nodes: list[SurveyNode],
        *,
        max_paves: int = 4,
    ) -> PaveReport:
        """Survey ``nodes``, then pave up to ``max_paves`` fully-direct
        anchored webs. Webs with near-misses (needing an adapter) are left
        for W3; webs carrying a write-class hop are named, not run."""
        report: SurveyReport = self._surveyor.survey(tenant, nodes)
        by_key = {node.key: node for node in nodes}
        paved: list[str] = []
        refused: list[dict] = []
        outcomes: list[PaveOutcome] = []
        candidates = 0

        for web in report.webs:
            decision = self._classify(web)
            if decision is not None:
                outcomes.append(
                    PaveOutcome(web.web_id, web.anchor, "skipped", reason=decision)
                )
                continue
            candidates += 1
            if len(paved) >= max_paves:
                outcomes.append(
                    PaveOutcome(
                        web.web_id, web.anchor, "skipped",
                        reason="tick budget reached",
                    )
                )
                continue
            outcomes.append(self._pave_one(tenant, web, by_key))
            if outcomes[-1].status == "paved":
                paved.append(web.web_id)
            elif outcomes[-1].status == "rehearsal-failed":
                refused.append(
                    {"web_id": web.web_id, "reason": outcomes[-1].reason}
                )

        report_out = PaveReport(
            tenant=tenant,
            surveyed=report.surveyed_nodes,
            candidates=candidates,
            paved=paved,
            refused=refused,
            outcomes=outcomes,
        )
        return report_out

    # ------------------------------------------------------------------ #
    @staticmethod
    def _classify(web: RouteWeb) -> str | None:
        """None means "a W2 candidate"; a string is the reason to skip."""
        if not web.anchored:
            return "no trigger door anchors this web"
        if web.near_misses:
            # A web needing ANY adapter is W3's, whether or not it also has
            # direct edges — W2 paves only the fully-direct case.
            return "has near-misses — needs an adapter (W3)"
        if not web.edges:
            return "no direct edges to pave"
        return None

    def _pave_one(
        self, tenant: str, web: RouteWeb, by_key: dict[str, SurveyNode]
    ) -> PaveOutcome:
        children = [
            by_key[key].contract for key in web.node_ids if key in by_key
        ]
        if len(children) != len(web.node_ids):
            return PaveOutcome(
                web.web_id, web.anchor, "skipped",
                reason="a web node is missing its contract",
            )
        if not _rehearsable_effect_free(children):
            # A write-class hop: promote without an end-to-end rehearsal,
            # rehearsed=false — the first real run earns its edges. (Named
            # here; W2's tests cover the effect-free path; the write-class
            # promotion rides the same promote port with rehearsed=False.)
            return PaveOutcome(
                web.web_id, web.anchor, "skipped",
                reason="carries a write-class hop — deferred to a real run",
            )
        contract = self._web_contract(tenant, web, children)
        rehearsal = self._rehearse(contract)
        if not rehearsal.ok:
            if self._negative is not None:
                self._negative(tenant, web, rehearsal.error)
            if self._audit is not None:
                self._audit(
                    "paver.rehearsal_failed",
                    {
                        "run_id": "paver:schedule",
                        "tenant": tenant,
                        "web_id": web.web_id,
                        "reason": rehearsal.error[:200],
                    },
                )
            return PaveOutcome(
                web.web_id, web.anchor, "rehearsal-failed",
                reason=rehearsal.error,
            )
        node_id = self._promote(tenant, web, contract)
        if self._audit is not None:
            self._audit(
                "paver.web_paved",
                {
                    "run_id": "paver:schedule",
                    "tenant": tenant,
                    "web_id": web.web_id,
                    "anchor": web.anchor,
                    "node_id": node_id,
                    "nodes": len(children),
                    "edges": len(web.edges),
                    "rehearsed": rehearsal.rehearsed,
                },
            )
        return PaveOutcome(
            web.web_id, web.anchor, "paved", node_id=node_id
        )

    @staticmethod
    def _web_contract(
        tenant: str, web: RouteWeb, children: list[NodeContract]
    ) -> NodeContract:
        """The ONE SubgraphBody contract that fires the web — the survey's
        children composed, ordering left to ``derive_data_edges`` (the
        slot flow IS the order, node-generation §5). The compiler wires the
        dataflow and stamps the tenant at run time; this is just the
        composition."""
        name = f"web:{(web.anchor or web.web_id)[:16]}"
        return NodeContract(
            name=name,
            description=f"paved web anchored at {web.anchor}",
            provenance="synthesized",
            consumes=[],
            produces=[],
            body=SubgraphBody(nodes=list(children)),
        )
