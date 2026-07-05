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


def _build(tmp_path, *, executors=None):
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
