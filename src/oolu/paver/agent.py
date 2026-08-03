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


def _dedupe_slots(slots) -> list:
    """The distinct slots by (name, value_type, role), first-seen order."""
    out: list = []
    seen: set = set()
    for slot in slots:
        key = (slot.name, slot.value_type, slot.role)
        if key not in seen:
            seen.add(key)
            out.append(slot)
    return out


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
        is_paved: Callable[[str, RouteWeb], bool] | None = None,
        is_blocked: Callable[[str, RouteWeb], bool] | None = None,
        surveyor: WebSurveyor | None = None,
    ):
        self._rehearse = rehearse
        self._promote = promote
        self._negative = negative
        self._audit = audit
        # Idempotence ports (W2.1): a web already paved (unchanged) is not
        # re-paved; a web a prior failure blocked is not re-ground. Without
        # these the tick would re-rehearse and re-promote every web every
        # tick — minting duplicate nodes and starving the budget.
        self._is_paved = is_paved or (lambda tenant, web: False)
        self._is_blocked = is_blocked or (lambda tenant, web: False)
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
        anchored webs. Webs already paved (unchanged) or blocked by a prior
        failure are skipped BEFORE the budget so they never re-rehearse or
        starve fresh candidates. Webs with near-misses are left for W3;
        webs carrying a write-class hop are named, not run. The budget caps
        REHEARSALS (the expensive sandbox run), not just promotions."""
        report: SurveyReport = self._surveyor.survey(tenant, nodes)
        by_key = {node.key: node for node in nodes}
        paved: list[str] = []
        refused: list[dict] = []
        outcomes: list[PaveOutcome] = []
        candidates = 0
        rehearsed = 0

        for web in report.webs:
            decision = self._classify(web, by_key)
            if decision is not None:
                outcomes.append(
                    PaveOutcome(web.web_id, web.anchor, "skipped", reason=decision)
                )
                continue
            # Already paved (unchanged), or blocked by a recorded failure:
            # neither re-rehearses. Idempotence is what makes "the model
            # bill is paid at pave time" and "never grind the same junction"
            # true across ticks.
            if self._is_paved(tenant, web):
                outcomes.append(
                    PaveOutcome(
                        web.web_id, web.anchor, "skipped", reason="already paved"
                    )
                )
                continue
            if self._is_blocked(tenant, web):
                outcomes.append(
                    PaveOutcome(
                        web.web_id, web.anchor, "skipped",
                        reason="blocked by a prior rehearsal failure",
                    )
                )
                continue
            candidates += 1
            if rehearsed >= max_paves:
                outcomes.append(
                    PaveOutcome(
                        web.web_id, web.anchor, "skipped",
                        reason="tick budget reached",
                    )
                )
                continue
            rehearsed += 1
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
    def _classify(
        self, web: RouteWeb, by_key: dict[str, SurveyNode]
    ) -> str | None:
        """None means "a W2 candidate"; a string is the reason to skip.
        The effect-freedom check lives here (not in _pave_one) so a
        write-class web is classified out BEFORE the budget and the
        idempotence ports."""
        if not web.anchored:
            return "no trigger door anchors this web"
        if web.near_misses:
            # A web needing ANY adapter is W3's, whether or not it also has
            # direct edges — W2 paves only the fully-direct case.
            return "has near-misses — needs an adapter (W3)"
        if not web.edges:
            return "no direct edges to pave"
        children = [by_key[key].contract for key in web.node_ids if key in by_key]
        if len(children) != len(web.node_ids):
            return "a web node is missing its contract"
        if not _rehearsable_effect_free(children):
            # A write-class hop (cli/http/browser side effects): W2 paves
            # only the effect-free case; the write-class web is deferred to
            # a real run (named, never silently rehearsed).
            return "carries a write-class hop — deferred to a real run"
        return None

    def _pave_one(
        self, tenant: str, web: RouteWeb, by_key: dict[str, SurveyNode]
    ) -> PaveOutcome:
        children = [
            by_key[key].contract for key in web.node_ids if key in by_key
        ]
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
        composition.

        The web's EXTERNAL interface is its boundary (W2.1): it consumes
        the inputs no child inside the web produces, and produces the
        outputs no child inside consumes — so the promoted node advertises
        real slots and is assemblable, not an opaque slotless node."""
        produced_inside = [p for child in children for p in child.produces]
        consumed_inside = [c for child in children for c in child.consumes]
        consumes = _dedupe_slots(
            slot
            for slot in consumed_inside
            if not any(p.matches(slot) for p in produced_inside)
        )
        produces = _dedupe_slots(
            slot
            for slot in produced_inside
            if not any(slot.matches(c) for c in consumed_inside)
        )
        name = f"web:{(web.anchor or web.web_id)[:16]}"
        return NodeContract(
            name=name,
            description=f"paved web anchored at {web.anchor}",
            provenance="synthesized",
            consumes=consumes,
            produces=produces,
            body=SubgraphBody(nodes=list(children)),
        )
