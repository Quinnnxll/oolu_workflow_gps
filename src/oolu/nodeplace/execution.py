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

from typing import Mapping, NamedTuple
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from ..knowledge.traces import NodeObservation, TraceStore, route_node_key
from ..metering.models import NoderShare, RunBinding
from ..orchestrator.contract import compile_with_owners
from ..orchestrator.state import Blueprint, ExecutionRecord, RoutePlan
from ..skills.contract import NodeContract, SubgraphBody
from ..skills.models import ExecutionStatus
from .economics import CandidateAssembler
from .market import PriceBook
from .rewards import lineage_shares, reward_multiplier


class ReservedActionsError(PermissionError):
    """The contract contains reserved actions; only the approval flow may run it."""


class CompiledContract(NamedTuple):
    """A contract compiled once: the runnable blueprint plus, for every
    action id, the name of the top-level child that contributed it (the
    key trace statistics accumulate under)."""

    blueprint: Blueprint
    owners: dict[str, str]


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


def compile_contract(contract: NodeContract) -> CompiledContract:
    """Compile without the reserved gate (raises ``ValueError`` if broken).

    For surfaces that HOLD reserved contracts for explicit approval rather
    than refusing them — the approval flow calls this after an authorized
    identity session grants the approval. Unattended paths must use
    :func:`compile_runnable` instead.
    """
    blueprint, owners = compile_with_owners(contract)
    return CompiledContract(blueprint=blueprint, owners=owners)


def reserved_operations(compiled: CompiledContract) -> list[str]:
    """The reserved (irreversible) operations a compiled contract contains."""
    return sorted(
        {
            f"{item.action.adapter}/{item.action.operation}"
            for item in compiled.blueprint.actions
            if item.reserved
        }
    )


def compile_runnable(contract: NodeContract) -> CompiledContract:
    """Compile for direct execution; reserved actions are refused loudly.

    Raises ``ValueError`` for an uncompilable contract and
    :class:`ReservedActionsError` when any action is reserved — direct paths
    never get to skip the orchestrator's approval flow.
    """
    compiled = compile_contract(contract)
    if reserved_operations(compiled):
        raise ReservedActionsError(
            "contract contains reserved actions; submit it through the "
            "orchestrator flow so approval can be granted"
        )
    return compiled


# The adapters whose actions carry a node's egress consent: the http hand
# enforces the stamp directly; the script hand passes it to the web broker
# that answers the sandbox's http_request calls — one consent, two hands.
_EGRESS_ADAPTERS = ("http", "script")


def stamp_egress_grants(
    contract: NodeContract,
    compiled: CompiledContract,
    grants: Mapping[str, tuple[str, ...]],
    open_grants: Mapping[str, tuple[str, ...]] | None = None,
) -> CompiledContract:
    """Stamp each node-owned http and script action with its egress grant.

    ``grants`` maps a child's version id to the owning node's consented
    hosts (``WorkDesk.network_grants``) — REGISTERED nodes only. A child
    present in ``grants`` gets ``_egress_hosts`` stamped onto every http
    action it contributed, even when the grant is empty: the executor
    fails an empty grant closed, so a registered node reaches nothing
    until its responsible human consents. Children absent from ``grants``
    (ad-hoc, unregistered) stay unstamped — they are the submitter's own
    direct request, governed by the machine policy alone.

    ``open_grants`` is the OTHER regime, for nodes under a Supernode set
    up under the global account (a verified legal entity): version id →
    the org's own blocked hosts. Those children get ``_egress_open`` and
    ``_egress_blocked`` instead of an allowlist — their web stands open,
    minus what the org chose to refuse. Open beats the allow-grant when
    both apply: verification is the wider consent.

    Call this at EXECUTION time, not hold time: consent is withdrawable,
    so a held contract must run under the grants of the moment it is
    approved, not the moment it was submitted."""
    children = (
        contract.body.nodes if isinstance(contract.body, SubgraphBody) else [contract]
    )
    open_grants = open_grants or {}
    id_by_name = {child.name: child.id for child in children}
    stamped: list = []
    changed = False
    for item in compiled.blueprint.actions:
        action = item.action
        owner = compiled.owners.get(action.id)
        version_id = id_by_name.get(owner or "")
        if action.adapter in _EGRESS_ADAPTERS and version_id in open_grants:
            action = action.model_copy(
                update={
                    "parameters": {
                        **action.parameters,
                        "_egress_open": True,
                        "_egress_blocked": list(open_grants[version_id]),
                    }
                }
            )
            item = item.model_copy(update={"action": action})
            changed = True
        elif action.adapter in _EGRESS_ADAPTERS and version_id in grants:
            action = action.model_copy(
                update={
                    "parameters": {
                        **action.parameters,
                        "_egress_hosts": list(grants[version_id]),
                    }
                }
            )
            item = item.model_copy(update={"action": action})
            changed = True
        stamped.append(item)
    if not changed:
        return compiled
    return CompiledContract(
        blueprint=compiled.blueprint.model_copy(update={"actions": stamped}),
        owners=compiled.owners,
    )


class ContractEstimate(NamedTuple):
    """A pre-commit cost estimate plus what kind of job this mostly is."""

    gross: float
    # The class key of the child carrying the most cost — the class the
    # budget layer buckets spending behavior under.
    goal_class: str | None


def estimate_contract_gross(
    contract: NodeContract,
    *,
    assembler: CandidateAssembler,
    price_book: PriceBook,
) -> ContractEstimate:
    """What this contract would cost to run, without committing anything.

    The same per-child clearing the run performs, in preview mode — so a
    budget can be enforced BEFORE any price moves the market or any
    binding is written.
    """
    children = (
        contract.body.nodes if isinstance(contract.body, SubgraphBody) else [contract]
    )
    total = 0.0
    dominant: tuple[float, str] | None = None
    for child in children:
        entry = assembler.assemble_version(child.id)
        if entry is None:
            continue
        candidate, signals = entry.candidate, entry.signals
        cleared = price_book.clear(
            class_key=candidate.class_key,
            node_class=candidate.node_class,
            ask=candidate.cleared_price,
            cost=candidate.cost,
            substitutes=signals.substitutes,
            commit=False,  # an estimate must not move the market
        )
        total += cleared.cleared
        if dominant is None or cleared.cleared > dominant[0]:
            dominant = (cleared.cleared, candidate.class_key)
    return ContractEstimate(gross=total, goal_class=dominant[1] if dominant else None)


def execute_contract(
    contract: NodeContract,
    compiled: CompiledContract,
    *,
    runner,  # orchestrator.DagRouteRunner (any WorkflowExecutor-compatible)
    assembler: CandidateAssembler,
    price_book: PriceBook,
    attribution,  # metering.AttributionStore
    audit,  # append(event_type, payload) -> the deriver's verified source
    consumer_tenant: str,
    consumer_principal: str,
    run_id: str | None = None,
    trace_store: TraceStore | None = None,
    trace_context: str = "",
    trace_exclude: frozenset[str] = frozenset(),
) -> ContractRunResult:
    """Run an assembled contract with multi-node marketplace binding.

    Every marketplace child clears at a committed price. The run gets ONE
    aggregate :class:`RunBinding` (run bindings key on ``run_id``) whose
    shares merge each node's lineage split weighted by its cleared price —
    so the deriver pays every noder in the chain from one verified event.

    With a ``trace_store`` attached, the run also feeds the growth loop:
    one node-granular trace per run — each top-level child's verdict and
    what it actually cost — recorded under the same ``route:{name}`` keys
    the assembler scores by, so every confirmed run sharpens the next
    assembly's picks. ``trace_context`` buckets the statistics (e.g. per
    tenant), which is what personalizes them.
    """
    run_id = run_id or uuid4().hex
    children = (
        contract.body.nodes if isinstance(contract.body, SubgraphBody) else [contract]
    )
    market_nodes: list[ContractNodeOutcome] = []
    merged: dict[str, float] = {}
    cleared_by_name: dict[str, float] = {}
    gross_total = 0.0
    cost_total = 0.0
    representative: str | None = None
    dominant: tuple[float, str] | None = None
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
        if dominant is None or cleared.cleared > dominant[0]:
            dominant = (cleared.cleared, candidate.class_key)
        cleared_by_name[child.name] = cleared.cleared
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
                version_ids=[n.version_id for n in market_nodes],
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
                # The class the budget layer buckets this spend under.
                goal_class=dominant[1] if dominant else None,
            )
        )

    record = runner.execute(
        RoutePlan(chosen=compiled.blueprint, alternatives=[], total_cost=0.0),
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
    if trace_store is not None:
        _record_contract_trace(
            trace_store,
            contract,
            compiled,
            record,
            cleared_by_name,
            context=trace_context,
            exclude=trace_exclude,
        )
    return ContractRunResult(
        run_id=run_id,
        status=record.status.value,
        error=record.error,
        outcomes=[
            # Evidence rides along so the caller sees what verification
            # actually measured (e.g. a geometry report's numbers), the
            # same way the browser and skills surfaces expose it.
            {"status": o.status.value, "error": o.error, "evidence": o.evidence}
            for o in record.action_outcomes
        ],
        market=ContractMarket(
            gross=gross_total,
            provider_cost=cost_total,
            nodes=market_nodes,
            noders=sorted(merged),
        ),
    )


def _record_contract_trace(
    trace_store: TraceStore,
    contract: NodeContract,
    compiled: CompiledContract,
    record: ExecutionRecord,
    cleared_by_name: dict[str, float],
    *,
    context: str,
    exclude: frozenset[str] = frozenset(),
) -> None:
    """Fold one contract run into the trace statistics, node-granularly.

    A child succeeds only if every action it contributed succeeded; its
    cost is the price the run actually cleared at (marketplace children
    only). Steps are ordered by each child's last completion, so the
    precedence matrix learns the real partial order across runs. Keys are
    ``route:{child name}`` — exactly what the assembler scores by.
    """
    action_of = {
        f"{record.idempotency_key}:{item.action.id}": item.action.id
        for item in compiled.blueprint.actions
    }
    child_ok: dict[str, bool] = {}
    last_done: dict[str, int] = {}
    for index, outcome in enumerate(record.action_outcomes):  # completion order
        action_id = action_of.get(outcome.idempotency_key)
        owner = compiled.owners.get(action_id or "")
        if owner is None or owner == contract.name:
            continue  # unattributable, or the contract's own glue actions
        if owner in exclude:
            # The node forbids using its throughput for auto-development:
            # the run executes and settles normally, but this child leaves
            # no observation behind for the growth loop.
            continue
        ok = outcome.status is ExecutionStatus.SUCCEEDED
        child_ok[owner] = child_ok.get(owner, True) and ok
        last_done[owner] = index
    steps = [
        NodeObservation(
            node_key=route_node_key(name),
            ok=child_ok[name],
            cost=cleared_by_name.get(name),
        )
        for name in sorted(child_ok, key=lambda n: last_done[n])
    ]
    trace_store.record_run(
        goal=contract.name,
        steps=steps,
        success=record.status is ExecutionStatus.SUCCEEDED,
        context=context,
    )
