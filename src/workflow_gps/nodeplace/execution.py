"""Contract execution — one money path for gateway and desktop surfaces.

``execute_contract`` is the running counterpart of ``assembly.preview_assembly``:
it takes the contract a surface previewed, clears every marketplace node at a
*committed* price (a real run moves the market), binds the run once with the
lineage-weighted aggregate share split, executes the compiled DAG, and appends
the ``workflow.executed`` audit event — the same event the metering deriver
pays from, and only on platform-verified success. Because the gateway endpoint
and the desktop confirm button both call this, there is exactly one place where
contract runs turn into money.

``compile_runnable`` is the shared admission gate: it compiles the contract and
refuses reserved (irreversible) actions with :class:`ReservedActionsError` —
a ``PermissionError``, so surfaces map it to 403 — keeping the human-approval
invariant intact on every direct path.
"""

from __future__ import annotations

from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from ..metering.models import NoderShare, RunBinding
from ..orchestrator.contract import contract_to_blueprint
from ..orchestrator.state import Blueprint, RoutePlan
from ..skills.contract import NodeContract, SubgraphBody
from .economics import CandidateAssembler
from .market import PriceBook
from .rewards import lineage_shares, reward_multiplier


class ReservedActionsError(PermissionError):
    """The contract contains reserved actions; only the approval flow may run it."""


class ContractNodeOutcome(BaseModel):
    """One marketplace node's committed economics inside the run."""

    model_config = ConfigDict(frozen=True)

    version_id: str
    cleared: float
    noders: list[str] = Field(default_factory=list)


class ContractMarket(BaseModel):
    model_config = ConfigDict(frozen=True)

    gross: float = 0.0
    provider_cost: float = 0.0
    nodes: list[ContractNodeOutcome] = Field(default_factory=list)
    noders: list[str] = Field(default_factory=list)


class ContractRunResult(BaseModel):
    """What actually happened: execution status plus the committed economics."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    status: str
    error: str | None = None
    outcomes: list[dict] = Field(default_factory=list)  # per-action status/error
    market: ContractMarket = Field(default_factory=ContractMarket)


def compile_runnable(contract: NodeContract) -> Blueprint:
    """Compile for direct execution; reserved actions are refused loudly.

    Raises ``ValueError`` for an uncompilable contract and
    :class:`ReservedActionsError` when any action is reserved — direct paths
    never get to skip the orchestrator's approval flow.
    """
    blueprint = contract_to_blueprint(contract)
    if any(item.reserved for item in blueprint.actions):
        raise ReservedActionsError(
            "contract contains reserved actions; submit it through the "
            "orchestrator flow so approval can be granted"
        )
    return blueprint


def execute_contract(
    contract: NodeContract,
    blueprint: Blueprint,
    *,
    runner,  # orchestrator.DagRouteRunner (any WorkflowExecutor-compatible)
    assembler: CandidateAssembler,
    price_book: PriceBook,
    attribution,  # metering.AttributionStore
    audit,  # append(event_type, payload) -> the deriver's verified source
    consumer_tenant: str,
    consumer_principal: str,
    run_id: str | None = None,
) -> ContractRunResult:
    """Run an assembled contract with multi-node marketplace binding.

    Every marketplace child clears at a committed price. The run gets ONE
    aggregate :class:`RunBinding` (run bindings key on ``run_id``) whose
    shares merge each node's lineage split weighted by its cleared price —
    so the deriver pays every noder in the chain from one verified event.
    """
    run_id = run_id or uuid4().hex
    children = (
        contract.body.nodes if isinstance(contract.body, SubgraphBody) else [contract]
    )
    market_nodes: list[ContractNodeOutcome] = []
    merged: dict[str, float] = {}
    gross_total = 0.0
    cost_total = 0.0
    representative: str | None = None
    for child in children:
        entry = assembler.assemble_version(child.id)
        if entry is None:
            continue  # a local/gap node: no marketplace economics
        candidate, signals = entry.candidate, entry.signals
        cleared = price_book.clear(
            class_key=candidate.class_key,
            node_class=candidate.node_class,
            ask=candidate.cleared_price,
            cost=candidate.cost,
            substitutes=signals.substitutes,
        )  # commit: a real run moves the market reference
        representative = representative or candidate.version_id
        gross_total += cleared.cleared
        cost_total += candidate.cost.automation_cost
        shares = lineage_shares(
            candidate.noder_principal,
            assembler.lineage_for(candidate.version_id),
            executing_multiplier=reward_multiplier(signals).multiplier,
        )
        for share in shares:
            merged[share.noder_principal] = merged.get(share.noder_principal, 0.0) + (
                share.weight * share.multiplier * cleared.cleared
            )
        market_nodes.append(
            ContractNodeOutcome(
                version_id=candidate.version_id,
                cleared=cleared.cleared,
                noders=[s.noder_principal for s in shares],
            )
        )
    if representative is not None and gross_total > 0:
        total_weight = sum(merged.values())
        attribution.bind(
            RunBinding(
                run_id=run_id,
                version_id=representative,
                consumer_tenant=consumer_tenant,
                consumer_principal=consumer_principal,
                gross=gross_total,
                provider_cost=cost_total,
                shares=[
                    NoderShare(
                        noder_principal=principal,
                        weight=weight / total_weight,
                    )
                    for principal, weight in sorted(merged.items())
                ],
            )
        )

    record = runner.execute(
        RoutePlan(chosen=blueprint, alternatives=[], total_cost=0.0),
        idempotency_key=f"{run_id}:exec:1",
        attempt=1,
    )
    # The same audit event the deriver mints money from — but only on a
    # verified success, exactly like orchestrated runs.
    audit.append(
        "workflow.executed",
        {
            "run_id": run_id,
            "status": record.status.value,
            "idempotency_key": record.idempotency_key,
        },
    )
    return ContractRunResult(
        run_id=run_id,
        status=record.status.value,
        error=record.error,
        outcomes=[
            {"status": o.status.value, "error": o.error} for o in record.action_outcomes
        ],
        market=ContractMarket(
            gross=gross_total,
            provider_cost=cost_total,
            nodes=market_nodes,
            noders=sorted(merged),
        ),
    )
