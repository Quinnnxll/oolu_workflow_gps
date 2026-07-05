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
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field

from ..billing.pricing import PricingEngine
from ..knowledge.traces import TraceStore, route_node_key
from ..orchestrator.assembler import ContractAssembler, GoalSpec, ProposalModel
from ..skills.contract import (
    ContractEdge,
    NodeContract,
    NodeStats,
    Slot,
    SubgraphBody,
    derive_data_edges,
)
from ..skills.inputs import inputs_manifest
from .budget import BudgetPolicy, BudgetVerdict, assess_budget
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
    # What the proposal model's advice cost while assembling this plan.
    # Not part of the market gross (no noder earns it), but real money the
    # budget verdict below judges alongside the gross.
    planning_cost: float = 0.0
    # The declared creative inputs of the assembled plan — name (qualified
    # per node), description, type, default, and bounds — so a surface can
    # show exactly which values a run will need and what happens if nobody
    # fills them (the defaults). Fill them via POST /v1/runs/contract
    # {"inputs": {...}} or leave them to the configured value patcher.
    inputs: list[dict] = Field(default_factory=list)
    # The plan's chance of verified success in THIS caller's hands: the
    # product of each picked node's posterior mean (personal history folded
    # over marketplace counts; independence assumed, documented). A gap
    # node counts at its uniform prior, 0.5 — unproven synthesis is a coin
    # flip and the number should say so. None when nothing assembled.
    expected_success: float | None = None
    # Orderings the caller's own runs consistently exhibited, stamped onto
    # the contract as learned edges: [{"first": name, "then": name}, ...].
    learned_order: list[dict[str, str]] = Field(default_factory=list)
    # The plan's cost judged against caps, thresholds, spending behavior,
    # and the (possibly partial) linked wallet.
    budget: BudgetVerdict | None = None


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


def _with_learned_order(
    contract: NodeContract, trace_store: TraceStore
) -> tuple[NodeContract, list[dict[str, str]]]:
    """Stamp trace-derived orderings onto a subgraph as learned edges.

    Slot flow orders what data forces; everything else is parallel by
    default. But when the caller's own runs consistently completed one
    child before another (``derive_edges``: enough observations, one
    direction nearly always), that order is knowledge — so it rides the
    contract as ``provenance="learned"`` edges the compiler already
    honors, and the scheduler stops racing steps the user's history says
    are ordered. Edges that data flow or explicit edges already imply —
    or contradict — are dropped: typed slots outrank statistics, and a
    contradiction must surface as parallelism, never as a learned cycle.
    """
    body = contract.body
    if not isinstance(body, SubgraphBody):
        return contract, []
    names: dict[str, list[str]] = {}
    for child in body.nodes:
        names.setdefault(child.name, []).append(child.id)
    # Ambiguous names cannot be attributed to one child: leave them out.
    key_to_id = {
        route_node_key(name): ids[0] for name, ids in names.items() if len(ids) == 1
    }
    derived = trace_store.derive_edges(list(key_to_id))
    if not derived:
        return contract, []
    fixed = {(e.source, e.target) for e in body.edges} | {
        (e.source, e.target) for e in derive_data_edges(body.nodes)
    }
    id_to_name = {child.id: child.name for child in body.nodes}
    learned: list[ContractEdge] = []
    for first_key, second_key in derived:
        source, target = key_to_id[first_key], key_to_id[second_key]
        if (source, target) in fixed or (target, source) in fixed:
            continue
        learned.append(ContractEdge(source=source, target=target, provenance="learned"))
    if not learned:
        return contract, []
    stamped = contract.model_copy(
        update={
            "body": SubgraphBody(
                nodes=list(body.nodes), edges=list(body.edges) + learned
            )
        }
    )
    order = [
        {"first": id_to_name[e.source], "then": id_to_name[e.target]} for e in learned
    ]
    return stamped, order


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
    proposal_model: ProposalModel | None = None,
    cost_weight: float = 0.0,
    budget: BudgetPolicy | None = None,
    # goal_class | None -> the caller's committed run grosses (None = all
    # classes). A lookup, not a list: the plan's class is only known after
    # assembly, and behavior is judged within its own class of goal.
    spend_lookup: Callable[[str | None], list[float] | None] | None = None,
    wallet_balance: float | None = None,
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

    With a ``proposal_model``, a model's opinion enters each pick as a
    prior over the same posteriors — and what the advice cost surfaces as
    ``planning_cost``, judged by the budget verdict together with the
    market gross: a plan that needed advice is honestly dearer.

    A positive ``cost_weight`` makes picks cost-aware: candidates rank by
    expected utility (quality minus weighted personal cost) instead of
    quality alone, so a slightly-less-proven cheap node can honestly beat
    a proven expensive one — by exactly the trade the caller declared.
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
        proposal_model=proposal_model,
        cost_weight=cost_weight,
    ).assemble(goal)
    personalized = {c.id: c for c in library}

    contract = result.contract
    learned_order: list[dict[str, str]] = []
    if contract is not None and trace_store is not None:
        contract, learned_order = _with_learned_order(contract, trace_store)

    nodes: list[AssemblyNodePreview] = []
    gross_total = 0.0
    margin_total = 0.0
    dominant: tuple[float, str] | None = None  # (cleared, class_key)
    children = (
        contract.body.nodes
        if contract is not None and isinstance(contract.body, SubgraphBody)
        else []
    )
    expected_success: float | None = None
    if contract is not None:
        # The plan succeeds when every picked node does (independence
        # assumed): the product of posterior means over the personalized
        # library. Gap nodes carry no history — their uniform prior mean
        # of 0.5 keeps the number honest about unproven synthesis.
        expected_success = 1.0
        for child in children:
            stats = (personalized.get(child.id) or child).stats or NodeStats()
            expected_success *= stats.success_mean
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
        if dominant is None or cleared.cleared > dominant[0]:
            dominant = (cleared.cleared, candidate.class_key)
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
        contract=contract,
        nodes=nodes,
        estimated_gross_total=gross_total,
        platform_margin_preview=margin_total,
        planning_cost=result.planning_cost,
        expected_success=expected_success,
        inputs=(
            [
                {
                    "name": entry.qualified,
                    "node": entry.node_name,
                    "description": entry.spec.description,
                    "value_type": entry.spec.value_type,
                    "default": entry.spec.default,
                    "minimum": entry.spec.minimum,
                    "maximum": entry.spec.maximum,
                    "choices": entry.spec.choices,
                }
                for entry in inputs_manifest(contract)
            ]
            if contract is not None
            else []
        ),
        learned_order=learned_order,
        budget=assess_budget(
            # Planning advice was real spend: budgets judge the whole cost
            # of the plan, not just the market's share of it.
            gross_total + result.planning_cost,
            policy=budget,
            spend_history=spend_lookup(None) if spend_lookup else None,
            class_history=(
                spend_lookup(dominant[1])
                if spend_lookup and dominant is not None
                else None
            ),
            goal_class=dominant[1] if dominant else None,
            wallet_balance=wallet_balance,
        ),
    )
