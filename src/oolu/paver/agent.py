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
from .contracts import NearMiss, RouteWeb, SurveyReport
from .discovery import SurveyNode, WebSurveyor

__all__ = [
    "DEFER",
    "AdapterBuild",
    "PaveOutcome",
    "PaveReport",
    "PaverAgent",
    "RehearsalResult",
]


class _Defer:
    """The junction is NOT READY to bridge this tick — the producer has
    not filed a value to author the adapter against yet (W3.1). Distinct
    from ``None`` (a GENUINE cannot-bridge: a ``none`` verdict, a candidate
    that never passed verify): a defer skips the web WITHOUT negative
    knowledge, so a later tick — once the producer has run — retries;
    ``None`` files the negative note that blocks a truly dead junction."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "DEFER"


DEFER = _Defer()


@dataclass(frozen=True)
class RehearsalResult:
    """What one rehearsal produced — did the web run clean in the sandbox."""

    ok: bool
    error: str = ""
    run_id: str | None = None
    rehearsed: bool = True  # False when a write-class hop forced per-node verify


@dataclass(frozen=True)
class AdapterBuild:
    """One bridged junction (W3): the adapter landed as its own citizen
    (``node_id``) plus the child contract to splice into the web — it
    CONSUMES the producer's produced slot and PRODUCES the consumer's
    consumed slot, so the compiler's dataflow derivation wires
    producer → adapter → consumer by slot match, and the near-miss
    becomes a direct chain. ``templated`` marks a deterministic rename."""

    node_id: str
    contract: NodeContract
    templated: bool = False


@dataclass(frozen=True)
class PaveOutcome:
    """One web's fate this tick."""

    web_id: str
    anchor: str | None
    # "paved" | "rehearsal-failed" | "adapter-failed" | "deferred" | "skipped"
    status: str
    node_id: str | None = None
    reason: str = ""
    adapters: int = 0  # junctions bridged by a synthesized adapter (W3)


@dataclass(frozen=True)
class PaveReport:
    """One tick's whole result — what the heartbeat did."""

    tenant: str
    surveyed: int = 0
    candidates: int = 0
    adapters_built: int = 0  # junctions bridged by a synthesized adapter (W3)
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
        build_adapter: (
            Callable[[str, NearMiss, SurveyNode, SurveyNode], AdapterBuild | None]
            | None
        ) = None,
        surveyor: WebSurveyor | None = None,
    ):
        self._rehearse = rehearse
        self._promote = promote
        self._negative = negative
        self._audit = audit
        # The adapter port (W3): bridge one near-miss junction — negotiate,
        # sample the producer's real value, synthesize+verify, land the
        # adapter as a citizen, and return the child to splice in. None
        # means this Paver paves only fully-direct webs (the W2 posture);
        # a web with near-misses is then skipped, never forced.
        self._build_adapter = build_adapter
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
        """Survey ``nodes``, then pave up to ``max_paves`` anchored webs.
        Webs already paved (unchanged) or blocked by a prior failure are
        skipped BEFORE the budget so they never re-rehearse or starve
        fresh candidates. A web with near-misses is bridged by synthesized
        adapters (W3) when an adapter builder is configured, else skipped;
        a web carrying a write-class hop is named, not run. The budget caps
        REHEARSALS (the expensive sandbox run), not just promotions."""
        report: SurveyReport = self._surveyor.survey(tenant, nodes)
        by_key = {node.key: node for node in nodes}
        paved: list[str] = []
        refused: list[dict] = []
        outcomes: list[PaveOutcome] = []
        candidates = 0
        rehearsed = 0
        adapters_built = 0

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
            outcome = self._pave_one(tenant, web, by_key)
            outcomes.append(outcome)
            adapters_built += outcome.adapters
            if outcome.status == "paved":
                paved.append(web.web_id)
            elif outcome.status in ("rehearsal-failed", "adapter-failed"):
                refused.append(
                    {"web_id": web.web_id, "reason": outcome.reason}
                )

        report_out = PaveReport(
            tenant=tenant,
            surveyed=report.surveyed_nodes,
            candidates=candidates,
            adapters_built=adapters_built,
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
        if web.near_misses and self._build_adapter is None:
            # A web needing an adapter but no builder is configured: this
            # Paver paves only fully-direct webs (the W2 posture).
            return "has near-misses but no adapter builder is configured"
        if not web.edges and not web.near_misses:
            return "no edges to pave"
        children = [by_key[key].contract for key in web.node_ids if key in by_key]
        if len(children) != len(web.node_ids):
            return "a web node is missing its contract"
        if not _rehearsable_effect_free(children):
            # A write-class hop (cli/http/browser side effects): W2 paves
            # only the effect-free case; the write-class web is deferred to
            # a real run (named, never silently rehearsed). The synthesized
            # adapters are scripts (effect-free), so a near-miss over
            # sandboxed nodes still rehearses in full.
            return "carries a write-class hop — deferred to a real run"
        return None

    def _pave_one(
        self, tenant: str, web: RouteWeb, by_key: dict[str, SurveyNode]
    ) -> PaveOutcome:
        children = [
            by_key[key].contract for key in web.node_ids if key in by_key
        ]
        # Bridge every near-miss into an adapter child FIRST (W3): a web
        # only paves once all its junctions are direct, so a junction the
        # Paver cannot adapt fails the WHOLE web (named, negative-noted),
        # never a silent partial pave. Each adapter is already a
        # birth-verified citizen by the time it returns.
        adapters: list[NodeContract] = []
        if web.near_misses:
            bridged, problem, deferred = self._bridge_near_misses(
                tenant, web, by_key
            )
            if bridged is None and deferred:
                # NOT READY, not broken: a junction's producer has not
                # filed a value yet. Skip the web this tick WITHOUT
                # negative knowledge, so a later tick retries once the
                # value exists (the "defer, not fail" invariant, W3.1).
                return PaveOutcome(
                    web.web_id, web.anchor, "deferred", reason=problem
                )
            if bridged is None:
                if self._negative is not None:
                    self._negative(tenant, web, problem)
                if self._audit is not None:
                    self._audit(
                        "paver.adapter_failed",
                        {
                            "run_id": "paver:schedule",
                            "tenant": tenant,
                            "web_id": web.web_id,
                            "reason": problem[:200],
                        },
                    )
                return PaveOutcome(
                    web.web_id, web.anchor, "adapter-failed", reason=problem
                )
            adapters = [build.contract for build in bridged]

        contract = self._web_contract(tenant, web, children + adapters)
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
                reason=rehearsal.error, adapters=len(adapters),
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
                    "adapters": len(adapters),
                    "rehearsed": rehearsal.rehearsed,
                },
            )
        return PaveOutcome(
            web.web_id, web.anchor, "paved", node_id=node_id,
            adapters=len(adapters),
        )

    def _bridge_near_misses(
        self, tenant: str, web: RouteWeb, by_key: dict[str, SurveyNode]
    ) -> tuple[list[AdapterBuild] | None, str, bool]:
        """Turn each near-miss into an adapter child, or classify why not.
        The adapter port (gateway-supplied) negotiates the junction,
        samples the producer's real value, synthesizes+verifies, and lands
        the adapter as a citizen. It returns one of three things per
        junction: an :class:`AdapterBuild` (bridged), ``DEFER`` (not ready
        — the producer has not filed a value yet), or ``None`` (a GENUINE
        cannot-bridge).

        Returns ``(builds, reason, deferred)``: a genuine failure DOMINATES
        (``(None, reason, False)`` — negative-noted), else a defer yields
        ``(None, reason, True)`` — retried next tick, never negative-noted;
        all-bridged yields ``(builds, "", False)``. One unbridged junction
        fails the whole web: a paved web with a hole is not paved."""
        builds: list[AdapterBuild] = []
        deferred = False
        defer_reason = ""
        for miss in web.near_misses:
            src = by_key.get(miss.source)
            tgt = by_key.get(miss.target)
            if src is None or tgt is None:
                return (
                    None,
                    f"near-miss {miss.produced_slot!r}->{miss.consumed_slot!r} "
                    "is missing an endpoint contract",
                    False,
                )
            build = self._build_adapter(tenant, miss, src, tgt)
            if build is DEFER:
                # Not ready — remember it, but keep scanning: a GENUINE
                # failure elsewhere still dominates (it should negative-note,
                # not silently defer forever).
                deferred = True
                defer_reason = (
                    f"{miss.source}:{miss.produced_slot} -> "
                    f"{miss.target}:{miss.consumed_slot} not ready "
                    "(producer has not filed a value yet)"
                )
                continue
            if build is None:
                return (
                    None,
                    f"could not adapt {miss.source}:{miss.produced_slot} -> "
                    f"{miss.target}:{miss.consumed_slot} ({miss.reason})",
                    False,
                )
            builds.append(build)
        if deferred:
            return None, defer_reason, True
        return builds, "", False

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
