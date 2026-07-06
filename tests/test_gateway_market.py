"""/v1/market: candidates + quotes assembled from live production records."""

from __future__ import annotations

from test_http_gateway import _app, _req

from oolu.gateway import GatewayApp
from oolu.metering.attribution import AttributionStore
from oolu.metering.deriver import MeteringDeriver
from oolu.metering.models import RunBinding
from oolu.metering.store import MeteringLedger
from oolu.nodeplace import (
    CandidateAssembler,
    LiveVersionStats,
    NodeplaceService,
    PriceBook,
    RatingService,
    RatingStore,
    RegistryStore,
)
from oolu.skills.models import ActionEvent, ReusableSkill, SkillSignature


def _build(tmp_path):
    base, conn, ident = _app(tmp_path)
    registry = RegistryStore(conn)
    metering = MeteringLedger(conn)
    attribution = AttributionStore(conn)
    audit = base._durable.audit  # the orchestrator's own event sink
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
    )
    return app, conn, ident, registry, metering, attribution, audit


def _contribute_and_publish(
    app,
    ident,
    registry,
    *,
    name,
    noder,
    price,
    derived_from=None,
    consumes=None,
    produces=None,
    inputs=None,
    actions=None,
):
    skill = ReusableSkill(
        name=name,
        description=f"{name} cleans invoices",
        signature=SkillSignature(application="cli", adapter="cli"),
        actions=actions
        or [ActionEvent(correlation_id="c", adapter="cli", operation="run")],
    )
    body = {
        "skill": skill.model_dump(mode="json"),
        "semver": "1.0.0",
        "title": name,
        "summary": f"{name} cleans invoices reliably",
        "tags": ["invoice", "class:workflow", "market:invoice_cleaning"],
        "visibility": "public",
        "pricing": {"model": "per_success", "unit_price": price},
    }
    if derived_from is not None:
        body["derived_from"] = derived_from
    if consumes is not None:
        body["consumes"] = consumes
    if produces is not None:
        body["produces"] = produces
    if inputs is not None:
        body["inputs"] = inputs
    created = app.handle(
        _req(
            "POST",
            "/v1/nodeplace",
            token=ident.token(noder, "t1"),
            body=body,
        )
    )
    assert created.status == 201, created.body
    published = app.handle(
        _req(
            "POST",
            f"/v1/listings/{created.body['listing_id']}/publish",
            token=ident.token(noder, "t1"),
        )
    )
    assert published.status == 200
    return created.body["version_id"]


def _record_run(audit, attribution, metering, *, run_id, version_id, ok, principal):
    attribution.bind(
        RunBinding(
            run_id=run_id,
            version_id=version_id,
            consumer_tenant="t2",
            consumer_principal=principal,
            gross=0.20,
            provider_cost=0.02,
        )
    )
    audit.append(
        "workflow.executed",
        {
            "run_id": run_id,
            "status": "succeeded" if ok else "failed",
            "idempotency_key": f"idem:{run_id}",
        },
    )
    MeteringDeriver(audit, metering, attribution).derive()


def _seed(app, ident, registry, metering, attribution, audit):
    """Two competing listings: 'proven' earns real history, 'flaky' fails."""
    proven = _contribute_and_publish(
        app, ident, registry, name="proven cleaner", noder="noder-good", price=0.20
    )
    flaky = _contribute_and_publish(
        app, ident, registry, name="flaky cleaner", noder="noder-flaky", price=0.05
    )
    for index in range(3):
        _record_run(
            audit,
            attribution,
            metering,
            run_id=f"good-{index}",
            version_id=proven,
            ok=True,
            principal="consumer",
        )
    _record_run(
        audit,
        attribution,
        metering,
        run_id="bad-0",
        version_id=flaky,
        ok=False,
        principal="consumer",
    )
    # A verified consumer rates the proven node through the public endpoint.
    rated = app.handle(
        _req(
            "POST",
            f"/v1/versions/{proven}/ratings",
            token=ident.token("consumer", "t2"),
            body={"score": 5, "text": "works"},
        )
    )
    assert rated.status == 201
    return proven, flaky


def test_candidates_are_assembled_from_live_verified_records(tmp_path):
    app, conn, ident, registry, metering, attribution, audit = _build(tmp_path)
    proven, flaky = _seed(app, ident, registry, metering, attribution, audit)

    resp = app.handle(
        _req(
            "GET",
            "/v1/market/candidates",
            token=ident.token("consumer", "t2"),
            query={"q": "invoice"},
        )
    )
    assert resp.status == 200
    items = {item["candidate"]["version_id"]: item for item in resp.body["items"]}
    assert set(items) == {proven, flaky}

    good = items[proven]
    assert good["candidate"]["verified_successes"] == 3
    assert good["candidate"]["reputation"] > 1.0  # the 5-star verified rating
    assert good["candidate"]["class_key"] == "workflow:invoice_cleaning"
    assert good["signals"]["substitutes"] == 1  # the other listing, same market
    # Measured provider cost from metering flows into the cost vector.
    assert good["candidate"]["cost"]["compute"] == 0.02

    bad = items[flaky]
    assert bad["candidate"]["verified_failures"] == 1
    assert bad["candidate"]["verified_successes"] == 0

    # Verified history beats claims: proven ranks first despite a 4x price.
    assert resp.body["items"][0]["candidate"]["version_id"] == proven
    assert good["reward_multiplier"] > bad["reward_multiplier"]
    conn.close()


def test_browsing_candidates_never_moves_the_price_book(tmp_path):
    app, conn, ident, registry, metering, attribution, audit = _build(tmp_path)
    _seed(app, ident, registry, metering, attribution, audit)
    for _ in range(2):
        resp = app.handle(
            _req(
                "GET",
                "/v1/market/candidates",
                token=ident.token("consumer", "t2"),
                query={"q": "invoice"},
            )
        )
        assert resp.status == 200
    assert app._price_book.reference("workflow:invoice_cleaning") is None
    conn.close()


def test_market_quote_end_to_end(tmp_path):
    app, conn, ident, registry, metering, attribution, audit = _build(tmp_path)
    proven, _flaky = _seed(app, ident, registry, metering, attribution, audit)

    resp = app.handle(
        _req(
            "POST",
            "/v1/market/quotes",
            token=ident.token("consumer", "t2"),
            body={
                "mode": "certified",
                "steps": [
                    {
                        "name": "Clean invoices",
                        "q": "invoice",
                        "api_calls": 1,
                        "minutes_saved": 20,
                    }
                ],
            },
        )
    )
    assert resp.status == 200, resp.body
    quote = resp.body
    (step,) = quote["steps"]
    assert step["chosen"]["version_id"] == proven  # certified picks the proven node
    assert step["coverage"] == "subscription"
    (line,) = quote["invoice_lines"]
    assert line["amount"] == 0.0  # workflow node is plan-covered
    assert quote["total_user_due_now"] == 0.0
    payees = {p["noder_principal"] for p in quote["payout_previews"]}
    assert payees == {"noder-good"}
    assert all("verified success" in p["reason"] for p in quote["payout_previews"])
    # Quotes are previews by default: the book's reference stayed untouched.
    assert app._price_book.reference("workflow:invoice_cleaning") is None
    conn.close()


def test_market_endpoints_validate_and_fail_loudly(tmp_path):
    app, conn, ident, registry, metering, attribution, audit = _build(tmp_path)
    _seed(app, ident, registry, metering, attribution, audit)
    token = ident.token("consumer", "t2")

    assert app.handle(_req("GET", "/v1/market/candidates")).status == 401
    assert (
        app.handle(
            _req(
                "GET",
                "/v1/market/candidates",
                token=token,
                query={"mode": "luxury"},
            )
        ).status
        == 400
    )
    assert (
        app.handle(
            _req("POST", "/v1/market/quotes", token=token, body={"steps": []})
        ).status
        == 400
    )
    missing = app.handle(
        _req(
            "POST",
            "/v1/market/quotes",
            token=token,
            body={"steps": [{"name": "Teleport", "q": "teleportation"}]},
        )
    )
    assert missing.status == 404
    assert "Teleport" in missing.body["error"]["message"]
    conn.close()


def test_submit_run_binds_marketplace_node_and_accrues_on_verified_success(
    tmp_path,
):
    """The full loop: submit -> priced binding -> verified execution ->
    metering -> billing accrual, with conservation intact."""
    from oolu.billing import BillingService, EarningsLedger

    app, conn, ident, registry, metering, attribution, audit = _build(tmp_path)
    proven, _flaky = _seed(app, ident, registry, metering, attribution, audit)

    resp = app.handle(
        _req(
            "POST",
            "/v1/runs",
            token=ident.token("consumer", "t2"),
            body={"intent": "clean this month's invoices", "node_version_id": proven},
        )
    )
    assert resp.status == 202, resp.body
    market = resp.body["market"]
    assert market["version_id"] == proven
    assert market["gross"] > 0
    assert market["noders"] == ["noder-good"]

    run_id = resp.body["run_id"]
    binding = attribution.get_binding(run_id)
    assert binding is not None
    assert binding.consumer_tenant == "t2"
    assert binding.consumer_principal == "consumer"
    assert binding.gross == market["gross"]
    (share,) = binding.shares
    assert share.noder_principal == "noder-good"
    assert share.multiplier > 1.0  # earned: history + verified 5-star rating

    # The stub scenario executed the run; the audit log holds the verified
    # success. Derive it into metering, then accrue earnings from it.
    events = MeteringDeriver(audit, metering, attribution).derive()
    event = next(e for e in events if e.run_id == run_id)
    assert event.version_id == proven and event.gross == market["gross"]

    billing = BillingService(EarningsLedger(conn))
    entries = billing.price(event, attribution.attributions(event.event_id))
    assert entries.conserves()
    billing.accrue(event, attribution.attributions(event.event_id))
    balance = billing.balance("noder-good")
    assert balance.pending_micros + balance.available_micros == sum(
        entries.noder_micros.values()
    )
    assert sum(entries.noder_micros.values()) > 0
    conn.close()


def test_submit_run_with_unlisted_version_is_refused(tmp_path):
    app, conn, ident, registry, metering, attribution, audit = _build(tmp_path)
    _seed(app, ident, registry, metering, attribution, audit)
    resp = app.handle(
        _req(
            "POST",
            "/v1/runs",
            token=ident.token("consumer", "t2"),
            body={"intent": "do something", "node_version_id": "no-such-version"},
        )
    )
    assert resp.status == 404
    assert "no-such-version" in resp.body["error"]["message"]
    # And a plain run (no marketplace node) still submits untouched.
    plain = app.handle(
        _req(
            "POST",
            "/v1/runs",
            token=ident.token("consumer", "t2"),
            body={"intent": "do something plain"},
        )
    )
    assert plain.status == 202
    assert "market" not in plain.body
    assert attribution.get_binding(plain.body["run_id"]) is None
    conn.close()


def test_market_disabled_returns_404(tmp_path):
    app, conn, ident = _app(tmp_path)
    resp = app.handle(
        _req("GET", "/v1/market/candidates", token=ident.token("u", "t1"))
    )
    assert resp.status == 404
    conn.close()
