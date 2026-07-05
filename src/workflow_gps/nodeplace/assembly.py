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

from pydantic import BaseModel, ConfigDict, Field

from ..billing.pricing import PricingEngine
from ..orchestrator.assembler import ContractAssembler, GoalSpec
from ..skills.contract import NodeContract, Slot, SubgraphBody
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


def preview_assembly(
    assembler: CandidateAssembler,
    price_book: PriceBook,
    goal: GoalSpec,
    *,
    query: str = "",
    fill_gaps: bool = False,
) -> AssemblyPreview:
    """Assemble a goal over the marketplace and price the plan, read-only."""
    marketplace = assembler.contracts(query)
    by_id = {entry.contract.id: entry for entry in marketplace}
    result = ContractAssembler(
        [entry.contract for entry in marketplace],
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
