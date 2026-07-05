"""POST /v1/runs/contract: execute an assembled contract with multi-node binding.

The closing seam of the consumer journey: the contract that
``/v1/market/assemble`` returned is posted back and actually runs — every
marketplace node clears at a committed price, one aggregate binding carries
the lineage-weighted shares, and the audit event it appends is the very one
the metering deriver pays from on verified success.
"""

from __future__ import annotations

from test_gateway_market import _contribute_and_publish
from test_http_gateway import _app, _req
from test_market_assemble import RAW, TIDY

from workflow_gps.gateway import GatewayApp
from workflow_gps.metering.attribution import AttributionStore
from workflow_gps.metering.deriver import MeteringDeriver
from workflow_gps.metering.store import MeteringLedger
from workflow_gps.nodeplace import (
    CandidateAssembler,
    LiveVersionStats,
    NodeplaceService,
    PriceBook,
    RatingService,
    RatingStore,
    RegistryStore,
)
from workflow_gps.skills.contract import ActionsBody, NodeContract
from workflow_gps.skills.models import ActionEvent, ExecutionOutcome, ExecutionStatus


class _CliExecutor:
    """Succeeds every 'run' and counts calls, so replays are observable."""

    name = "cli"

    def __init__(self):
        self.calls = 0

    def capabilities(self):
        return frozenset({"run"})

    def execute(self, action, *, idempotency_key):
        self.calls += 1
        return ExecutionOutcome(
            idempotency_key=idempotency_key,
            skill_id=action.correlation_id,
            status=ExecutionStatus.SUCCEEDED,
        )

    def cancel(self, idempotency_key):
        return None


def _build(tmp_path, *, executors=None, trace_store=None, rng=None, wallet_lookup=None):
    base, conn, ident = _app(tmp_path)
    registry = RegistryStore(conn)
    metering = MeteringLedger(conn)
    attribution = AttributionStore(conn)
    audit = base._durable.audit
    ratings = RatingService(RatingStore(conn), verified_run=metering.verified_run)
    assembler = CandidateAssembler(
        registry=registry,
        stats=LiveVersionStats(metering=metering, audit=audit, attribution=attribution),
        ratings=ratings,
    )
    app = GatewayApp(
        base._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        nodeplace=NodeplaceService(registry),
        ratings=ratings,
        market=assembler,
        price_book=PriceBook(tmp_path / "prices.db"),
        attribution=attribution,
        contract_executors=executors,
        trace_store=trace_store,
        rng=rng,
        wallet_lookup=wallet_lookup,
    )
    return app, conn, ident, registry, metering, attribution, audit


def _seed_chain(app, ident, registry):
    exporter = _contribute_and_publish(
        app,
        ident,
        registry,
        name="raw exporter",
        noder="noder-export",
        price=0.10,
        produces=[RAW],
        consumes=[],
    )
    cleaner = _contribute_and_publish(
        app,
        ident,
        registry,
        name="invoice cleaner",
        noder="noder-clean",
        price=0.20,
        consumes=[RAW],
        produces=[TIDY],
    )
    return exporter, cleaner


def _assembled_contract(app, ident):
    resp = app.handle(
        _req(
            "POST",
            "/v1/market/assemble",
            token=ident.token("consumer", "t2"),
            body={"goal": {"name": "clean-the-books", "want": [TIDY]}, "q": "invoice"},
        )
    )
    assert resp.status == 200 and resp.body["complete"], resp.body
    return resp.body["contract"]


def test_contract_run_executes_binds_and_pays_end_to_end(tmp_path):
    """assemble -> run -> aggregate binding -> derived metering -> accrual,
    with money conservation intact across both noders."""
    from workflow_gps.billing import BillingService, EarningsLedger

    executor = _CliExecutor()
    app, conn, ident, registry, metering, attribution, audit = _build(
        tmp_path, executors={"cli": executor}
    )
    exporter, cleaner = _seed_chain(app, ident, registry)
    contract = _assembled_contract(app, ident)

    resp = app.handle(
        _req(
            "POST",
            "/v1/runs/contract",
            token=ident.token("consumer", "t2"),
            body={"contract": contract},
            headers={"Idempotency-Key": "contract-1"},
        )
    )
    assert resp.status == 200, resp.body
    assert resp.body["status"] == "succeeded"
    assert len(resp.body["outcomes"]) == 2  # both chain nodes actually ran
    assert executor.calls == 2

    market = resp.body["market"]
    assert market["gross"] > 0
    assert market["noders"] == ["noder-clean", "noder-export"]
    assert {n["version_id"] for n in market["nodes"]} == {exporter, cleaner}

    # One aggregate binding for the whole run; weights are a proper split.
    run_id = resp.body["run_id"]
    binding = attribution.get_binding(run_id)
    assert binding is not None
    assert binding.gross == market["gross"]
    assert {s.noder_principal for s in binding.shares} == {
        "noder-clean",
        "noder-export",
    }
    assert abs(sum(s.weight for s in binding.shares) - 1.0) < 1e-9

    # A real run commits the market reference (unlike browse/assemble).
    assert app._price_book.reference("workflow:invoice_cleaning") is not None

    # The audit event it appended is the deriver's payment source.
    events = MeteringDeriver(audit, metering, attribution).derive()
    event = next(e for e in events if e.run_id == run_id)
    assert event.gross == market["gross"]

    billing = BillingService(EarningsLedger(conn))
    entries = billing.price(event, attribution.attributions(event.event_id))
    assert entries.conserves()
    assert set(entries.noder_micros) == {"noder-clean", "noder-export"}
    assert all(m > 0 for m in entries.noder_micros.values())

    # Idempotent replay: same key, same run, nothing executes twice.
    again = app.handle(
        _req(
            "POST",
            "/v1/runs/contract",
            token=ident.token("consumer", "t2"),
            body={"contract": contract},
            headers={"Idempotency-Key": "contract-1"},
        )
    )
    assert again.status == 200
    assert again.body["run_id"] == run_id
    assert executor.calls == 2
    conn.close()


def test_contract_run_feeds_the_trace_store(tmp_path):
    """The growth loop: a run records node-granular traces under the same
    route:{name} keys the assembler scores by."""
    from workflow_gps.knowledge.traces import TraceStore, route_node_key

    traces = TraceStore(tmp_path / "traces.db")
    app, conn, ident, registry, *_rest = _build(
        tmp_path, executors={"cli": _CliExecutor()}, trace_store=traces
    )
    _seed_chain(app, ident, registry)
    contract = _assembled_contract(app, ident)

    resp = app.handle(
        _req(
            "POST",
            "/v1/runs/contract",
            token=ident.token("consumer", "t2"),
            body={"contract": contract},
        )
    )
    assert resp.status == 200 and resp.body["status"] == "succeeded"

    # Whole-goal and per-node posteriors grew, in the tenant's bucket.
    goal = traces.posterior(route_node_key("clean-the-books"), "t2")
    assert (goal.successes, goal.failures) == (1, 0)
    for name in ("raw exporter", "invoice cleaner"):
        node = traces.posterior(route_node_key(name), "t2")
        assert (node.successes, node.failures) == (1, 0)

    # The cost EWMA is the price this run actually cleared at.
    cleared = {n["version_id"]: n["cleared"] for n in resp.body["market"]["nodes"]}
    assert cleared  # both nodes committed a price
    by_name = {c["name"]: c["id"] for c in contract["body"]["nodes"]}
    paid = traces.expected_cost(route_node_key("invoice cleaner"), "t2")
    assert paid == cleared[by_name["invoice cleaner"]]

    # The precedence matrix learned the real order: export before clean.
    ab, ba = traces.precedence(
        route_node_key("raw exporter"), route_node_key("invoice cleaner")
    )
    assert (ab, ba) == (1, 0)
    traces.close()
    conn.close()


def test_personal_history_flips_the_assembly_pick(tmp_path):
    """Two equivalent producers: the tenant's own confirmed-run failures
    push assembly onto the alternative — personalization by construction."""
    from workflow_gps.knowledge.traces import (
        NodeObservation,
        TraceStore,
        route_node_key,
    )

    traces = TraceStore(tmp_path / "traces.db")
    app, conn, ident, registry, *_rest = _build(tmp_path, trace_store=traces)
    _seed_chain(app, ident, registry)
    # A second cleaner, identical vocabulary; alphabetically first, so with
    # no history the deterministic tie-break picks it.
    _contribute_and_publish(
        app,
        ident,
        registry,
        name="backup cleaner",
        noder="noder-backup",
        price=0.20,
        consumes=[RAW],
        produces=[TIDY],
    )

    def selected():
        resp = app.handle(
            _req(
                "POST",
                "/v1/market/assemble",
                token=ident.token("consumer", "t2"),
                body={
                    "goal": {"name": "clean-the-books", "want": [TIDY]},
                    "q": "invoice",
                },
            )
        )
        assert resp.status == 200, resp.body
        return set(resp.body["selected"])

    first_pick = selected()
    assert "backup cleaner" in first_pick

    # This tenant's own runs keep failing on the backup cleaner.
    for run in range(3):
        traces.record_run(
            goal="clean-the-books",
            steps=[
                NodeObservation(node_key=route_node_key("backup cleaner"), ok=False)
            ],
            success=False,
            context="t2",
        )
    assert "invoice cleaner" in selected()  # the pick flipped

    # Another tenant shares no history: their pick is unchanged.
    other = app.handle(
        _req(
            "POST",
            "/v1/market/assemble",
            token=ident.token("someone-else", "t1"),
            body={
                "goal": {"name": "clean-the-books", "want": [TIDY]},
                "q": "invoice",
            },
        )
    )
    assert "backup cleaner" in set(other.body["selected"])
    traces.close()
    conn.close()


def test_explore_thompson_samples_assembly_picks(tmp_path):
    """explore=true samples producer picks from the posteriors: with no
    history both alternatives get tried; with history the winner dominates
    — and without the flag, picks stay deterministic."""
    import random

    from workflow_gps.knowledge.traces import (
        NodeObservation,
        TraceStore,
        route_node_key,
    )

    traces = TraceStore(tmp_path / "traces.db")
    app, conn, ident, registry, *_rest = _build(
        tmp_path, trace_store=traces, rng=random.Random(7)
    )
    _seed_chain(app, ident, registry)
    _contribute_and_publish(
        app,
        ident,
        registry,
        name="backup cleaner",
        noder="noder-backup",
        price=0.20,
        consumes=[RAW],
        produces=[TIDY],
    )
    cleaners = {"invoice cleaner", "backup cleaner"}

    def pick(explore):
        resp = app.handle(
            _req(
                "POST",
                "/v1/market/assemble",
                token=ident.token("consumer", "t2"),
                body={
                    "goal": {"name": "clean-the-books", "want": [TIDY]},
                    "q": "invoice",
                    "explore": explore,
                },
            )
        )
        assert resp.status == 200, resp.body
        (chosen,) = cleaners & set(resp.body["selected"])
        return chosen

    # Deterministic by default: the same pick, every time.
    assert len({pick(explore=False) for _ in range(5)}) == 1

    # No history yet: exploration gives both alternatives real chances.
    unproven_picks = [pick(explore=True) for _ in range(20)]
    assert set(unproven_picks) == cleaners

    # History accumulates (as confirmed runs would record it): exploration
    # collapses onto the proven producer.
    for run in range(8):
        traces.record_run(
            goal="clean-the-books",
            steps=[
                NodeObservation(node_key=route_node_key("invoice cleaner"), ok=True),
            ],
            success=True,
            context="t2",
        )
        traces.record_run(
            goal="clean-the-books",
            steps=[
                NodeObservation(node_key=route_node_key("backup cleaner"), ok=False),
            ],
            success=False,
            context="t2",
        )
    proven_picks = [pick(explore=True) for _ in range(20)]
    assert proven_picks.count("invoice cleaner") >= 18
    traces.close()
    conn.close()


def test_learned_order_rides_the_assembled_contract(tmp_path):
    """Two slot-independent nodes are parallel by default — until the
    caller's own runs consistently order them. Then the assembled contract
    carries a learned edge the compiler turns into a real dependency."""
    from workflow_gps.knowledge.traces import (
        NodeObservation,
        TraceStore,
        route_node_key,
    )
    from workflow_gps.orchestrator import compile_with_owners

    alpha_out = {"name": "alpha_out", "value_type": "path"}
    beta_out = {"name": "beta_out", "value_type": "path"}
    traces = TraceStore(tmp_path / "traces.db")
    app, conn, ident, registry, *_rest = _build(tmp_path, trace_store=traces)
    for name, noder, produces in (
        ("alpha step", "noder-a", [alpha_out]),
        ("beta step", "noder-b", [beta_out]),
    ):
        _contribute_and_publish(
            app,
            ident,
            registry,
            name=name,
            noder=noder,
            price=0.10,
            consumes=[],
            produces=produces,
        )

    def assemble():
        resp = app.handle(
            _req(
                "POST",
                "/v1/market/assemble",
                token=ident.token("consumer", "t2"),
                body={"goal": {"name": "both-things", "want": [alpha_out, beta_out]}},
            )
        )
        assert resp.status == 200, resp.body
        return resp.body

    # No slot relation and no history: parallel, nothing learned.
    body = assemble()
    assert body["learned_order"] == []
    assert body["contract"]["body"]["edges"] == []

    # The caller's runs consistently finish alpha before beta.
    for run in range(3):
        traces.record_run(
            goal="both-things",
            steps=[
                NodeObservation(node_key=route_node_key("alpha step"), ok=True),
                NodeObservation(node_key=route_node_key("beta step"), ok=True),
            ],
            success=True,
            context="t2",
        )

    body = assemble()
    assert body["learned_order"] == [{"first": "alpha step", "then": "beta step"}]
    (edge,) = body["contract"]["body"]["edges"]
    assert edge["provenance"] == "learned" and edge["relation"] == "before"

    # The learned edge becomes a real dependency in the compiled DAG.
    from workflow_gps.skills.contract import NodeContract

    blueprint, owners = compile_with_owners(
        NodeContract.model_validate(body["contract"])
    )
    ordered = {
        (owners[e.source], owners[e.target])
        for e in blueprint.edges
        if e.relation == "before"
    }
    assert ("alpha step", "beta step") in ordered
    traces.close()
    conn.close()


def test_contradicting_traces_never_override_data_flow(tmp_path):
    """Typed slots outrank statistics: a trace order opposite to a data
    edge is dropped, not stamped — no learned cycles, ever."""
    from workflow_gps.knowledge.traces import (
        NodeObservation,
        TraceStore,
        route_node_key,
    )

    traces = TraceStore(tmp_path / "traces.db")
    app, conn, ident, registry, *_rest = _build(tmp_path, trace_store=traces)
    _seed_chain(app, ident, registry)  # data edge: exporter -> cleaner
    # Corrupt history claims the cleaner finished first, consistently.
    for run in range(3):
        traces.record_run(
            goal="clean-the-books",
            steps=[
                NodeObservation(node_key=route_node_key("invoice cleaner"), ok=True),
                NodeObservation(node_key=route_node_key("raw exporter"), ok=True),
            ],
            success=True,
            context="t2",
        )

    contract = _assembled_contract(app, ident)
    assert contract["body"]["edges"] == []  # data flow stands; nothing learned
    traces.close()
    conn.close()


def test_contract_run_refuses_reserved_actions(tmp_path):
    """Human control survives the direct path: irreversible verbs are 403."""
    app, conn, ident, *_rest = _build(tmp_path, executors={"cli": _CliExecutor()})
    destructive = NodeContract(
        name="wipe it",
        body=ActionsBody(
            actions=[
                ActionEvent(correlation_id="c", adapter="cli", operation="delete_files")
            ]
        ),
    )
    resp = app.handle(
        _req(
            "POST",
            "/v1/runs/contract",
            token=ident.token("consumer", "t2"),
            body={"contract": destructive.model_dump(mode="json")},
        )
    )
    assert resp.status == 403
    assert "reserved" in resp.body["error"]["message"]
    conn.close()


def test_contract_run_requires_auth(tmp_path):
    app, conn, *_rest = _build(tmp_path, executors={"cli": _CliExecutor()})
    assert app.handle(_req("POST", "/v1/runs/contract", body={})).status == 401
    conn.close()


def test_contract_run_requires_a_valid_contract(tmp_path):
    app, conn, ident, *_rest = _build(tmp_path, executors={"cli": _CliExecutor()})
    token = ident.token("consumer", "t2")

    missing = app.handle(_req("POST", "/v1/runs/contract", token=token, body={}))
    assert missing.status == 400

    malformed = app.handle(
        _req(
            "POST",
            "/v1/runs/contract",
            token=token,
            body={"contract": {"name": "broken", "body": {"kind": "no-such-kind"}}},
        )
    )
    assert malformed.status == 400
    conn.close()


def test_contract_run_disabled_without_executors(tmp_path):
    app, conn, ident, *_rest = _build(tmp_path)  # no contract_executors
    resp = app.handle(
        _req(
            "POST",
            "/v1/runs/contract",
            token=ident.token("consumer", "t2"),
            body={"contract": {"name": "x", "body": {"kind": "script", "goal": "g"}}},
        )
    )
    assert resp.status == 404
    conn.close()
