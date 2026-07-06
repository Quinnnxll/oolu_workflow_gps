from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from oolu.billing import (
    BalanceProjection,
    ChargingService,
    DefaultFraudSignals,
    EarningsLedger,
    FakePayoutAdapter,
    FraudSignals,
)
from oolu.durable.idempotency import IdempotencyLedger
from oolu.durable.postgres import PostgresDurableConnection
from oolu.identity import ProviderConfig
from oolu.metering import AttributionRecord, MeteringEvent, NoderShare
from oolu.nodeplace import is_duplicate_hash, is_plagiarism, similarity

PG_DSN = os.environ.get("OOLU_TEST_PG_DSN") or os.environ.get("DATABASE_URL")


class _AsymmetricVerifier:
    algorithm = "RS256"

    def verify(self, signing_input, signature, header) -> bool:
        return True


_ASYMMETRIC = [
    ProviderConfig(
        issuer="https://idp",
        audiences=frozenset({"oolu"}),
        verifier=_AsymmetricVerifier(),
    )
]


# --------------------------------------------------------------------------- #
# DefaultFraudSignals unit behaviour.                                         #
# --------------------------------------------------------------------------- #
def test_conforms_to_the_port():
    assert isinstance(DefaultFraudSignals(), FraudSignals)


def test_self_dealing_share_is_excluded():
    verdict = DefaultFraudSignals().assess(
        idempotency_key="k1",
        consumer_principal="alice",
        shares=[
            NoderShare(noder_principal="alice", weight=1.0),
            NoderShare(noder_principal="bob", weight=1.0),
        ],
    )
    assert verdict.allowed is True
    assert [s.noder_principal for s in verdict.shares] == ["bob"]
    assert any("self_dealing:alice" in r for r in verdict.reasons)


def test_replayed_success_is_rejected():
    fraud = DefaultFraudSignals()
    first = fraud.assess(idempotency_key="k1", consumer_principal="c", shares=[])
    second = fraud.assess(idempotency_key="k1", consumer_principal="c", shares=[])
    assert first.allowed is True
    assert second.allowed is False
    assert second.reasons == ["replayed_success"]


def test_durable_seen_predicate_detects_replay():
    fraud = DefaultFraudSignals(seen=lambda key: key == "already-metered")
    verdict = fraud.assess(
        idempotency_key="already-metered", consumer_principal="c", shares=[]
    )
    assert verdict.allowed is False


def test_velocity_throttle():
    fraud = DefaultFraudSignals(velocity_limit=2)
    keys = ["k1", "k2", "k3"]
    verdicts = [
        fraud.assess(idempotency_key=k, consumer_principal="spammer", shares=[])
        for k in keys
    ]
    assert [v.allowed for v in verdicts] == [True, True, False]
    assert verdicts[2].reasons == ["velocity_exceeded"]


# --------------------------------------------------------------------------- #
# Plagiarism / similarity helpers.                                            #
# --------------------------------------------------------------------------- #
def test_exact_content_hash_duplicate():
    assert is_duplicate_hash("abc", {"abc", "def"}) is True
    assert is_duplicate_hash("xyz", {"abc"}) is False


def test_similarity_and_plagiarism_threshold():
    assert similarity("deploy the app to prod", "deploy the app to prod") == 1.0
    assert similarity("deploy the app", "totally different words here") < 0.2
    corpus = {"v1": "deploy the app to production now"}
    assert (
        is_plagiarism("deploy the app to production now", corpus, threshold=0.9) == "v1"
    )
    assert is_plagiarism("a wholly unrelated workflow", corpus, threshold=0.9) is None


# --------------------------------------------------------------------------- #
# Self-dealing exclusion end-to-end through ChargingService (live PostgreSQL). #
# --------------------------------------------------------------------------- #
def _pg():
    if not PG_DSN:
        pytest.skip("no PostgreSQL DSN configured")
    try:
        conn = PostgresDurableConnection(PG_DSN)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL unavailable: {exc}")
    with conn.transaction() as db:
        db.execute("DROP TABLE IF EXISTS earnings_entries")
        db.execute("DELETE FROM idempotency")
    return conn


@pytest.mark.needs_postgres
def test_self_dealer_earns_nothing_but_others_are_paid():
    conn = _pg()
    try:
        ledger = EarningsLedger(conn)
        service = ChargingService(
            ledger=ledger,
            payout=FakePayoutAdapter(),
            durable=conn,
            providers=_ASYMMETRIC,
            idempotency=IdempotencyLedger(conn),
            fraud=DefaultFraudSignals(),
        )
        event = MeteringEvent(
            idempotency_key="r1:exec:1",
            run_id="r1",
            version_id="v1",
            consumer_principal="alice",
            outcome="succeeded",
            gross=0.50,
            provider_cost=0.08,
            audit_seq=1,
            occurred_at=datetime(2030, 1, 1, tzinfo=UTC),
        )
        attributions = [
            AttributionRecord(
                event_id=event.event_id, noder_principal="alice", weight=1.0
            ),
            AttributionRecord(
                event_id=event.event_id, noder_principal="bob", weight=1.0
            ),
        ]
        out = service.charge_and_accrue(event, attributions, consumer_ref="cus_alice")
        assert out["charged"] is True
        assert "alice" not in out["noder_accruals"]  # self-dealer excluded
        assert out["noder_accruals"] == {"bob": 294000}  # bob gets the whole pool
        assert any("self_dealing:alice" in r for r in out["excluded"])
        after = BalanceProjection(ledger).balance(
            "bob", now=datetime(2030, 2, 1, tzinfo=UTC)
        )
        assert after.available_micros == 294000
    finally:
        conn.close()
