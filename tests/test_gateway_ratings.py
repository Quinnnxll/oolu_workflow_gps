from __future__ import annotations

from datetime import UTC, datetime

from test_http_gateway import _app, _req

from workflow_gps.gateway import GatewayApp
from workflow_gps.metering import MeteringEvent, MeteringLedger
from workflow_gps.nodeplace import RatingService, RatingStore


def _build(tmp_path):
    base, conn, ident = _app(tmp_path)
    metering = MeteringLedger(conn)
    app = GatewayApp(
        base._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        ratings=RatingService(RatingStore(conn), verified_run=metering.verified_run),
    )
    return app, conn, ident, metering


def _record_run(metering, *, version_id, principal):
    metering.record(
        MeteringEvent(
            idempotency_key=f"{principal}:{version_id}",
            run_id="r",
            version_id=version_id,
            consumer_principal=principal,
            outcome="succeeded",
            audit_seq=1,
            occurred_at=datetime.now(UTC),
        )
    )


def test_rating_requires_verified_run(tmp_path):
    app, _, ident, _ = _build(tmp_path)
    resp = app.handle(
        _req(
            "POST",
            "/v1/versions/v1/ratings",
            token=ident.token("rater", "t1"),
            body={"score": 5},
        )
    )
    assert resp.status == 403


def test_verified_run_can_rate_and_reputation_is_shown(tmp_path):
    app, _, ident, metering = _build(tmp_path)
    _record_run(metering, version_id="v1", principal="rater")
    rated = app.handle(
        _req(
            "POST",
            "/v1/versions/v1/ratings",
            token=ident.token("rater", "t1"),
            body={"score": 5, "text": "great"},
        )
    )
    assert rated.status == 201
    assert rated.body["verified_run"] is True

    listed = app.handle(
        _req("GET", "/v1/versions/v1/ratings", token=ident.token("rater", "t1"))
    )
    assert listed.status == 200
    assert len(listed.body["items"]) == 1
    assert listed.body["reputation"] == 5.0 / 3.0
