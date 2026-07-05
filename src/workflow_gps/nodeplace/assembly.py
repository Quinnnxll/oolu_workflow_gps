"""Assembly previews — one computation for gateway and desktop surfaces.

``preview_assembly`` backward-chains a goal through the marketplace's slot
vocabularies and returns everything a surface needs to render the plan
*before anything runs*: the assembled contract, per-node cleared-price
previews, and the lineage-aware payout split settlement would use.
Read-only by construction — prices preview with ``commit=False`` and no
ledger is touched — so the gateway endpoint and the desktop shell render
the same numbers from the same math.
"""

from __future__ import annotations

import random

from pydantic import BaseModel, ConfigDict, Field

from ..billing.pricing import PricingEngine
from ..knowledge.traces import TraceStore, route_node_key
from ..orchestrator.assembler import ContractAssembler, GoalSpec
from ..skills.contract import NodeContract, NodeStats, Slot, SubgraphBody
from .economics import CandidateAssembler
from .market import PriceBook
from .rewards import commission_rate, lineage_shares, reward_multiplier

PREVIEW_REASON = "preview: accrues only on platform-verified success"


class PayoutPreviewEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    noder_principal: str
    amount: float
    reason: str = PREVIEW_REASON


class AssemblyNodePreview(BaseModel):
    """One selected node with its planning-time economics."""

    model_config = ConfigDict(frozen=True)

    name: str
    kind: str
    gap: bool = False
    version_id: str | None = None
    cleared: dict | None = None  # ClearedPrice dump: full force breakdown
    payout_previews: list[PayoutPreviewEntry] = Field(default_factory=list)
    platform_margin_preview: float | None = None


class AssemblyPreview(BaseModel):
    """The complete planning preview a surface renders."""

    model_config = ConfigDict(frozen=True)

    complete: bool
    selected: list[str] = Field(default_factory=list)
    gap_filled: list[str] = Field(default_factory=list)
    missing: list[Slot] = Field(default_factory=list)
    contract: NodeContract | None = None
    nodes: list[AssemblyNodePreview] = Field(default_factory=list)
    estimated_gross_total: float = 0.0
    platform_margin_preview: float = 0.0


def _personalize(
    contract: NodeContract, trace_store: TraceStore, context: str
) -> NodeContract:
    """Fold the caller's own confirmed-run history into a contract's stats.

    Marketplace stats are platform-wide verified counts; the trace store
    holds what happened when *this* caller ran the node. Both are evidence
    about the same Beta posterior, so they add — and the personal cost EWMA
    (what the caller actually paid) supersedes the listed automation cost.
    A node with no personal history is returned untouched.
    """
    personal = trace_store.posterior(route_node_key(contract.name), context)
    if personal.observations == 0:
        return contract
    base = contract.stats or NodeStats()
    paid = trace_store.expected_cost(route_node_key(contract.name), context)
    return contract.model_copy(
        update={
            "stats": NodeStats(
                successes=base.successes + personal.successes,
                failures=base.failures + personal.failures,
                cost_ewma=paid if paid is not None else base.cost_ewma,
            )
        }
    )


def preview_assembly(
    assembler: CandidateAssembler,
    price_book: PriceBook,
    goal: GoalSpec,
    *,
    query: str = "",
    fill_gaps: bool = False,
    trace_store: TraceStore | None = None,
    trace_context: str = "",
    rng: random.Random | None = None,
) -> AssemblyPreview:
    """Assemble a goal over the marketplace and price the plan, read-only.

    With a ``trace_store``, the library the assembler picks from carries the
    caller's own confirmed-run history on top of platform-verified counts —
    so every executed contract sharpens the next assembly, per
    ``trace_context`` bucket (e.g. per tenant).

    With an ``rng``, producer picks are Thompson-sampled from those same
    posteriors instead of taken greedily: unproven alternatives get real
    chances in proportion to how uncertain their quality still is, and the
    exploration collapses onto the winner as confirmed runs accumulate.
    Without one, picks are deterministic (best posterior mean, stable
    tie-breaks) — the right default for a preview the user is about to pay
    for.
    """
    marketplace = assembler.contracts(query)
    by_id = {entry.contract.id: entry for entry in marketplace}
    library = [entry.contract for entry in marketplace]
    if trace_store is not None:
        library = [_personalize(c, trace_store, trace_context) for c in library]
    result = ContractAssembler(
        library,
        rng=rng,
        fill_gaps_with_scripts=fill_gaps,
    ).assemble(goal)

    nodes: list[AssemblyNodePreview] = []
    gross_total = 0.0
    margin_total = 0.0
    children = (
        result.contract.body.nodes
        if result.contract is not None
        and isinstance(result.contract.body, SubgraphBody)
        else []
    )
    for child in children:
        entry = by_id.get(child.id)
        if entry is None:  # a synthesized gap node: no economics yet
            nodes.append(
                AssemblyNodePreview(
                    name=child.name, kind=child.body.kind.value, gap=True
                )
            )
            continue
        candidate, signals = entry.assembled.candidate, entry.assembled.signals
        cleared = price_book.clear(
            class_key=candidate.class_key,
            node_class=candidate.node_class,
            ask=candidate.cleared_price,
            cost=candidate.cost,
            substitutes=signals.substitutes,
            commit=False,  # planning must not move the market
        )
        breakdown = reward_multiplier(signals)
        rho = commission_rate(
            candidate.node_class,
            scarcity_bonus=0.5 / (1.0 + max(0, signals.substitutes)),
        )
        shares = lineage_shares(
            candidate.noder_principal,
            assembler.lineage_for(candidate.version_id),
            executing_multiplier=breakdown.multiplier,
        )
        split = PricingEngine(rho=rho).price(
            gross=cleared.cleared,
            provider_cost=candidate.cost.automation_cost,
            shares=shares,
        )
        gross_total += cleared.cleared
        margin_total += split.platform_micros / 1_000_000
        nodes.append(
            AssemblyNodePreview(
                name=child.name,
                kind=child.body.kind.value,
                version_id=candidate.version_id,
                cleared=cleared.model_dump(mode="json"),
                payout_previews=[
                    PayoutPreviewEntry(
                        noder_principal=principal, amount=micros / 1_000_000
                    )
                    for principal, micros in sorted(split.noder_micros.items())
                ],
                platform_margin_preview=split.platform_micros / 1_000_000,
            )
        )

    return AssemblyPreview(
        complete=result.complete,
        selected=result.selected,
        gap_filled=result.gap_filled,
        missing=result.missing,
        contract=result.contract,
        nodes=nodes,
        estimated_gross_total=gross_total,
        platform_margin_preview=margin_total,
    )
