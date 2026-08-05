"""The node books and the vitality law (V6).

Exit gate (node-vitality-plan, phase V6): the books answer
cost/income/net for any node from durable records; the sweep retires a
net-negative node with notice and an auditable reason; vitality
measurably and boundedly shifts assembly choice; and no payout pool
changes size.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from test_http_gateway import _autonomous, _Identity, _req

from oolu.billing import BillingService, EarningsLedger
from oolu.billing.model_usage import ModelUsageStore
from oolu.billing.models import EarningsEntry
from oolu.durable import DurableConnection, DurableWorkflowService
from oolu.gateway import GatewayApp
from oolu.metering.attribution import AttributionStore
from oolu.metering.compute import COMPUTE_RATE_PER_HOUR, ComputeMeterStore
from oolu.metering.models import MeteringEvent, RunBinding
from oolu.metering.store import MeteringLedger
from oolu.nodeplace import (
    CandidateAssembler,
    Listing,
    ListingStatus,
    Node,
    NodeAccountStore,
    NodeplaceService,
    NodeVersion,
    RegistryStore,
    Visibility,
    WorkDesk,
)
from oolu.nodeplace.books import (
    VITALITY_MAX,
    VITALITY_MIN,
    BooksReader,
    vitality_multiplier,
)
from oolu.orchestrator import (
    ActionExecutorRouteRunner,
    BoundedRetryRecovery,
    CapabilityGrounder,
    CollectingFeedbackSink,
    LeastCostRouteOptimizer,
    RiskBasedHumanControl,
    StaticIntaker,
    StatusOutcomeMonitor,
    WorkflowOrchestrator,
)
from oolu.reminders import ReminderStore
from oolu.social import AssistantHistoryStore

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)

_SKILL_JSON = json.dumps(
    {
        "name": "Old Costly",
        "description": "does costly things",
        "signature": {"application": "script", "adapter": "script"},
        "actions": [
            {
                "correlation_id": "c",
                "adapter": "script",
                "operation": "run",
                "parameters": {"script": "x"},
            }
        ],
    }
)


def _mint_node(
    registry,
    *,
    skill_id="fn-old",
    owner="user-1",
    tenant="t1",
    age_days=200,
    title="Old Costly",
):
    node = Node(
        noder_principal=owner,
        tenant_id=tenant,
        skill_id=skill_id,
        visibility=Visibility.PUBLIC,
        created_at=NOW - timedelta(days=age_days),
    )
    registry.add_node(node)
    version = NodeVersion(
        node_id=node.node_id,
        semver="1.0.0",
        content_hash=f"h-{skill_id}",
        sanitized_skill_json=_SKILL_JSON.replace("Old Costly", title),
        published_at=NOW - timedelta(days=age_days),
    )
    registry.add_version(version)
    registry.add_listing(
        Listing(
            version_id=version.version_id,
            title=title,
            summary="does costly things",
            status=ListingStatus.ACTIVE,
        )
    )
    return node, version


def _rig(tmp_path):
    ident = _Identity(tmp_path)
    brief, blueprint, executor, grounding = _autonomous()

    def build(events):
        return WorkflowOrchestrator(
            intaker=StaticIntaker(brief),
            grounder=CapabilityGrounder(grounding),
            optimizer=LeastCostRouteOptimizer([blueprint]),
            human_control=RiskBasedHumanControl(),
            executor=ActionExecutorRouteRunner({"test": executor}),
            monitor=StatusOutcomeMonitor(),
            recovery=BoundedRetryRecovery(),
            feedback=CollectingFeedbackSink(),
            events=events,
        )

    conn = DurableConnection(tmp_path / "durable.db")
    durable = DurableWorkflowService(conn, build)
    registry = RegistryStore(conn)
    metering = MeteringLedger(conn)
    attribution = AttributionStore(conn)
    ledger = EarningsLedger(conn)
    model_usage = ModelUsageStore(conn, clock=lambda: NOW)
    compute = ComputeMeterStore(conn)
    desk = WorkDesk(
        registry=registry,
        accounts=NodeAccountStore(conn),
        billing=BillingService(ledger),
        metering=metering,
        attribution=attribution,
        audit=durable.audit,
    )
    books = BooksReader(
        registry=registry,
        desk=desk,
        model_usage=model_usage,
        compute=compute,
        ledger=ledger,
        clock=lambda: NOW,
    )
    app = GatewayApp(
        durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        nodeplace=NodeplaceService(registry),
        desk=desk,
        metering=metering,
        attribution=attribution,
        model_usage=model_usage,
        compute_meter=compute,
        books=books,
        assistant_history=AssistantHistoryStore(conn),
        reminders=ReminderStore(conn, clock=lambda: NOW),
        clock=lambda: NOW,
    )
    return SimpleNamespace(
        app=app,
        conn=conn,
        ident=ident,
        registry=registry,
        metering=metering,
        attribution=attribution,
        ledger=ledger,
        model_usage=model_usage,
        compute=compute,
        desk=desk,
        books=books,
    )


# --------------------------------------------------------------------------- #
# The prerequisite, driven: metering events materialize on the tick.           #
# --------------------------------------------------------------------------- #
def test_the_deriver_is_driven_on_the_standing_tick(tmp_path):
    rig = _rig(tmp_path)
    try:
        _node, version = _mint_node(rig.registry)
        rig.attribution.bind(
            RunBinding(
                run_id="run-1",
                version_id=version.version_id,
                consumer_tenant="t1",
                consumer_principal="consumer-1",
                gross=1.0,
                provider_cost=0.1,
            )
        )
        rig.app._durable.audit.append(
            "workflow.executed",
            {
                "run_id": "run-1",
                "status": "succeeded",
                "idempotency_key": "exec:run-1",
            },
        )
        assert rig.metering.events_for_version(version.version_id) == []
        # The tick derives — no manual derive() call anywhere.
        rig.app._metering_gate = 0.0
        rig.app._economics_gate = float("inf")  # this test drives metering
        rig.app._maybe_scheduled_sweep(SimpleNamespace(now=NOW))
        (event,) = rig.metering.events_for_version(version.version_id)
        assert event.gross == 1.0
        # And the high-water mark advanced: a second tick re-reads nothing
        # (idempotency would hide it; the mark makes it cheap too).
        assert rig.app._metering_seq >= event.audit_seq
    finally:
        rig.conn.close()


# --------------------------------------------------------------------------- #
# Measured costs: the sandbox wall clock and the build's meter window.         #
# --------------------------------------------------------------------------- #
def test_compute_is_metered_at_the_declared_rate(tmp_path):
    conn = DurableConnection(tmp_path / "c.db")
    try:
        compute = ComputeMeterStore(conn)
        compute.record("node:fn-old", 7200.0, run_id="r1")  # two hours
        assert compute.node_cost("node:fn-old") == 2 * COMPUTE_RATE_PER_HOUR
        assert compute.mean_cost("node:fn-old") == 2 * COMPUTE_RATE_PER_HOUR
        assert compute.mean_cost("node:unknown") is None
    finally:
        conn.close()


def test_the_executor_meters_execute_runs(tmp_path):
    from oolu.cache.store import LocalScriptCache
    from oolu.models import ExecutionResult, Phase
    from oolu.runtime.script_node import NodeScriptRunner
    from oolu.skills.models import ActionEvent

    class _Backend:
        def run(self, request):
            return ExecutionResult(
                phase=Phase.EXECUTE,
                exit_code=0,
                stdout='{"status": "ok", "payload": {"result": "done"}}',
                duration_s=3.5,
                contract_ok=True,
            )

    metered: list[tuple[str, float, str]] = []
    runner = NodeScriptRunner(
        _Backend(),
        LocalScriptCache(":memory:"),
        compute_meter=lambda key, secs, run: metered.append((key, secs, run)),
    )
    outcome = runner.execute(
        ActionEvent(
            correlation_id="c",
            adapter="script",
            operation="run",
            parameters={
                "goal": "g",
                "script": "from _oolu_runtime import emit_result\n"
                "emit_result('done')",
                "node_key": "node:fn-metered",
            },
        ),
        idempotency_key="run-x",
    )
    assert outcome.status.value == "succeeded"
    assert metered and metered[0][0] == "node:fn-metered"
    assert metered[0][1] == 3.5


def test_model_cost_lands_on_the_nodes_own_line(tmp_path):
    conn = DurableConnection(tmp_path / "m.db")
    try:
        usage = ModelUsageStore(conn, clock=lambda: NOW)
        usage.record_node(
            "t1", "node-1", source="node.build", cost=6.0, prompt_tokens=100
        )
        usage.record_node("t1", "node-1", source="node.repair", cost=1.5)
        assert usage.node_cost("t1", "node-1") == 7.5
        assert usage.node_cost("t1", "node-2") == 0.0
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The books: cost, income (binding-weighted), net — from durable records.      #
# --------------------------------------------------------------------------- #
def test_income_splits_by_the_bindings_own_weights(tmp_path):
    rig = _rig(tmp_path)
    try:
        node_a, version_a = _mint_node(rig.registry, skill_id="fn-a")
        node_b, version_b = _mint_node(
            rig.registry, skill_id="fn-b", title="Cheap Sibling"
        )
        rig.attribution.bind(
            RunBinding(
                run_id="run-w",
                version_id=version_a.version_id,
                version_ids=[version_a.version_id, version_b.version_id],
                version_weights={
                    version_a.version_id: 3.0,
                    version_b.version_id: 1.0,
                },
                consumer_tenant="t1",
                gross=4.0,
            )
        )
        event = MeteringEvent(
            idempotency_key="exec:run-w",
            run_id="run-w",
            version_id=version_a.version_id,
            outcome="succeeded",
            gross=4.0,
            audit_seq=1,
            occurred_at=NOW,
        )
        rig.metering.record(event)
        rig.ledger.append(
            EarningsEntry(
                noder_principal="user-1",
                event_id=event.event_id,
                amount_micros=4_000_000,
            )
        )
        # The dear node earned three quarters; the cheap sibling one.
        assert rig.desk.node_income_micros(node_a.node_id) == 3_000_000
        assert rig.desk.node_income_micros(node_b.node_id) == 1_000_000
        books = rig.books.books(node_a.node_id)
        assert books.income_micros == 3_000_000
        assert books.net_usd == 3.0  # no costs booked yet
    finally:
        rig.conn.close()


def test_the_books_door_answers_from_durable_records(tmp_path):
    rig = _rig(tmp_path)
    try:
        node, _version = _mint_node(rig.registry)
        rig.model_usage.record_node(
            "t1", node.node_id, source="node.build", cost=2.0
        )
        rig.compute.record("node:fn-old", 3600.0)  # one hour
        resp = rig.app.handle(
            _req(
                "GET",
                f"/v1/work/nodes/{node.node_id}/books",
                token=rig.ident.token("user-1", "t1"),
            )
        )
        assert resp.status == 200, resp.body
        assert resp.body["model_cost_usd"] == 2.0
        assert resp.body["compute_cost_usd"] == COMPUTE_RATE_PER_HOUR
        assert resp.body["net_usd"] == -(2.0 + COMPUTE_RATE_PER_HOUR)
        assert VITALITY_MIN <= resp.body["vitality"] <= VITALITY_MAX
    finally:
        rig.conn.close()


# --------------------------------------------------------------------------- #
# The vitality law: retire with notice, reason on the chain, history intact.   #
# --------------------------------------------------------------------------- #
def test_the_sweep_retires_a_net_negative_node_with_notice(tmp_path):
    rig = _rig(tmp_path)
    try:
        node, _version = _mint_node(rig.registry, age_days=200)
        rig.model_usage.record_node(
            "t1", node.node_id, source="node.build", cost=6.0
        )
        rig.app._economics_sweep_tick(NOW)
        # Retired through the standing revocation — history intact.
        assert rig.registry.get_node(node.node_id).revoked_at is not None
        assert rig.registry.list_versions(node.node_id)  # nothing erased
        retired = [
            r
            for r in rig.app._durable.audit.records()
            if r.event_type == "node.retired"
        ]
        assert retired and retired[0].payload["node_id"] == node.node_id
        assert "vitality law" in retired[0].payload["reason"]
        assert retired[0].payload["books"]["model_cost_usd"] == 6.0
        # The owner heard it FROM the platform, books attached.
        turns = rig.app._assistant_history.history(
            tenant="t1", principal="user-1"
        )
        assert turns and "retired by the vitality law" in turns[-1]["body"]
        assert "$6.00" in turns[-1]["body"]
        rows = rig.app._reminders.upcoming(tenant="t1", principal="user-1")
        assert rows and "retired" in rows[0].text
    finally:
        rig.conn.close()


def test_grace_age_and_the_floor_both_protect(tmp_path):
    rig = _rig(tmp_path)
    try:
        young, _v1 = _mint_node(rig.registry, skill_id="fn-young", age_days=10)
        modest, _v2 = _mint_node(
            rig.registry, skill_id="fn-modest", age_days=200
        )
        rig.model_usage.record_node(
            "t1", young.node_id, source="node.build", cost=50.0
        )
        rig.model_usage.record_node(
            "t1", modest.node_id, source="node.build", cost=4.0
        )
        rig.app._economics_sweep_tick(NOW)
        # A newborn is not reaped for being new; −$4 is above the floor.
        assert rig.registry.get_node(young.node_id).revoked_at is None
        assert rig.registry.get_node(modest.node_id).revoked_at is None
    finally:
        rig.conn.close()


def test_the_sweep_claims_its_schedule_once(tmp_path):
    rig = _rig(tmp_path)
    try:
        _mint_node(rig.registry, age_days=200)
        rig.app._economics_sweep_tick(NOW)
        view = rig.app._economics_schedule.view()
        assert view["enabled"] and view["granted_by"] == "platform"
        assert view["last_summary"] is not None
        # A second tick inside the same day finds nothing due.
        before = view["last_finished_at"]
        rig.app._economics_sweep_tick(NOW + timedelta(minutes=5))
        assert (
            rig.app._economics_schedule.view()["last_finished_at"] == before
        )
    finally:
        rig.conn.close()


# --------------------------------------------------------------------------- #
# Gravity: bounded, selection-shifting, pool-conserving.                       #
# --------------------------------------------------------------------------- #
def test_vitality_is_bounded_and_reads_the_books(tmp_path):
    assert (
        vitality_multiplier(
            net_usd=10_000.0, health_score=1.0, trust=5.0, days_stale=0.0
        )
        == VITALITY_MAX
    )
    assert (
        vitality_multiplier(
            net_usd=-10_000.0, health_score=0.0, trust=1.0, days_stale=999.0
        )
        == VITALITY_MIN
    )
    neutral = vitality_multiplier(
        net_usd=0.0, health_score=None, trust=1.0, days_stale=0.0
    )
    assert 0.95 <= neutral <= 1.05


def test_vitality_shifts_the_assembly_pick_boundedly(tmp_path):
    conn = DurableConnection(tmp_path / "v.db")
    try:
        registry = RegistryStore(conn)
        _mint_node(registry, skill_id="fn-thriving", title="Thriving")
        _mint_node(registry, skill_id="fn-plain", title="Plain Twin")

        class _Stats:
            def version_stats(self, version_id):
                return SimpleNamespace(
                    successes=0,
                    failures=0,
                    provider_cost_mean=None,
                    latency_mean=None,
                )

        def market(vitality_of):
            return CandidateAssembler(
                registry=registry,
                stats=_Stats(),
                vitality=vitality_of,
            )

        titles = {}
        for entry in market(None).assemble(""):
            titles[entry.title] = entry.candidate
        # With no hand, twins tie at vitality 1.0.
        assert titles["Thriving"].vitality == 1.0

        def thriving_wins(node_id):
            node = registry.get_node(node_id)
            return 1.25 if node.skill_id == "fn-thriving" else 1.0

        tilted = {
            entry.title: entry.candidate
            for entry in market(thriving_wins).assemble("")
        }
        assert tilted["Thriving"].vitality == 1.25
        assert tilted["Thriving"].reputation > tilted["Plain Twin"].reputation
        # The assembler's posterior feels it as bounded pseudo-counts …
        contracts = {
            c.contract.name: c.contract
            for c in market(thriving_wins).contracts("")
        }
        assert contracts["Thriving"].stats.successes == 1.0  # 4 × 0.25
        assert contracts["Plain Twin"].stats.successes == 0.0
        # … and a runaway reader is clamped at the bound.
        clamped = {
            entry.title: entry.candidate
            for entry in market(lambda _n: 99.0).assemble("")
        }
        assert clamped["Thriving"].vitality == 1.25
    finally:
        conn.close()


def test_gravity_never_grows_any_pool(tmp_path):
    from oolu.billing.pricing import PricingEngine
    from oolu.nodeplace import NodeClass
    from oolu.nodeplace.rewards import (
        LineageLink,
        RewardSignals,
        lineage_shares,
        reward_multiplier,
    )

    # The tilted node's slice grows, the pool does not: the vitality-
    # scaled reputation rides the SAME bounded multiplier and the split
    # still conserves to the micro.
    tilted = reward_multiplier(
        RewardSignals(
            node_class=NodeClass.WORKFLOW,
            reputation=1.25,
            verified_successes=5,
        )
    ).multiplier
    plain = reward_multiplier(
        RewardSignals(
            node_class=NodeClass.WORKFLOW,
            reputation=1.0,
            verified_successes=5,
        )
    ).multiplier
    assert tilted > plain
    shares = [
        share.model_copy(update={"multiplier": multiplier})
        for share, multiplier in zip(
            lineage_shares(
                "alice", [LineageLink(noder_principal="bob", level=1)]
            ),
            [tilted, plain],  # principals sort alphabetically: alice, bob
        )
    ]
    result = PricingEngine(rho=0.2).price(
        gross=10.0, provider_cost=1.0, shares=shares
    )
    assert result.conserves()  # gravity redistributes, never inflates
    assert result.noder_micros["alice"] > result.noder_micros["bob"]
