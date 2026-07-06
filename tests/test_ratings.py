from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from oolu.billing import PricingEngine
from oolu.durable import DurableConnection
from oolu.durable.postgres import PostgresDurableConnection
from oolu.metering import MeteringEvent, MeteringLedger, NoderShare
from oolu.nodeplace import (
    RatingError,
    RatingService,
    RatingStore,
    UnverifiedRunError,
    mu_from_ratings,
)

PG_DSN = os.environ.get("OOLU_TEST_PG_DSN") or os.environ.get("DATABASE_URL")


def _new_pg() -> PostgresDurableConnection:
    if not PG_DSN:
        pytest.skip("no PostgreSQL DSN configured")
    try:
        conn = PostgresDurableConnection(PG_DSN)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL unavailable: {exc}")
    with conn.transaction() as db:
        db.execute("DROP TABLE IF EXISTS ratings")
        db.execute("DROP TABLE IF EXISTS metering_events")
    return conn


@pytest.fixture(params=["sqlite", "postgres"])
def conn(request):
    if request.param == "sqlite":
        connection = DurableConnection(":memory:")
    else:
        connection = _new_pg()
    try:
        yield connection
    finally:
        connection.close()


def _record_run(metering: MeteringLedger, *, version_id: str, principal: str) -> None:
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


def _service(conn):
    metering = MeteringLedger(conn)
    return RatingService(
        RatingStore(conn), verified_run=metering.verified_run
    ), metering


# --------------------------------------------------------------------------- #
# Verified-run gating.                                                         #
# --------------------------------------------------------------------------- #
def test_rating_requires_a_verified_run(conn):
    service, _ = _service(conn)
    with pytest.raises(UnverifiedRunError):
        service.rate(rater_principal="rater", version_id="v1", score=5)


def test_verified_run_allows_rating(conn):
    service, metering = _service(conn)
    _record_run(metering, version_id="v1", principal="rater")
    rating = service.rate(
        rater_principal="rater", version_id="v1", score=4, text="good"
    )
    assert rating.verified_run is True
    assert rating.score == 4
    assert [r.rater_principal for r in service.ratings("v1")] == ["rater"]


def test_a_principal_may_rate_a_version_only_once(conn):
    service, metering = _service(conn)
    _record_run(metering, version_id="v1", principal="rater")
    service.rate(rater_principal="rater", version_id="v1", score=5)
    with pytest.raises(RatingError):
        service.rate(rater_principal="rater", version_id="v1", score=1)


def test_score_out_of_range_is_rejected(conn):
    service, metering = _service(conn)
    _record_run(metering, version_id="v1", principal="rater")
    with pytest.raises(RatingError):
        service.rate(rater_principal="rater", version_id="v1", score=6)


def test_another_principals_run_does_not_authorize_this_rater(conn):
    service, metering = _service(conn)
    _record_run(metering, version_id="v1", principal="someone-else")
    with pytest.raises(UnverifiedRunError):
        service.rate(rater_principal="rater", version_id="v1", score=5)


def test_reputation_reflects_ratings(conn):
    service, metering = _service(conn)
    for principal, score in [("a", 5), ("b", 4)]:
        _record_run(metering, version_id="v1", principal=principal)
        service.rate(rater_principal=principal, version_id="v1", score=score)
    assert service.reputation("v1") == pytest.approx(4.5 / 3.0)


# --------------------------------------------------------------------------- #
# Pure reputation -> mu mapping and its effect on pricing.                    #
# --------------------------------------------------------------------------- #
def test_mu_from_ratings_is_neutral_without_ratings():
    assert mu_from_ratings(0.0, 0) == 1.0


def test_mu_from_ratings_scales_and_clamps():
    assert mu_from_ratings(3.0, 5) == pytest.approx(1.0)
    assert mu_from_ratings(1.0, 5) == pytest.approx(1.0 / 3.0)
    assert mu_from_ratings(5.0, 5, mu_max=1.2) == 1.2


def test_reputation_multiplier_shifts_the_earnings_split():
    result = PricingEngine(rho=0.0).price(
        gross=0.30,
        provider_cost=0.0,
        shares=[
            NoderShare(noder_principal="reputable", weight=1.0, multiplier=2.0),
            NoderShare(noder_principal="new", weight=1.0, multiplier=1.0),
        ],
    )
    assert result.noder_micros == {"reputable": 200000, "new": 100000}
    assert result.conserves()
