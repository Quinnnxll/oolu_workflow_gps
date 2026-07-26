"""M2: RFQ, quotes, and negotiation — discovery wide, boundaries signed.

Exit gates (docs/marketplace-build-plan.md §5 M2): a substitute outside the
required specification is marked ineligible BEFORE policy evaluation and
can never be awarded; a quote below the seller's absolute floor is refused
without model discretion; a negotiation agent cannot accept terms outside
its signed bounds, and the round budget exhausts deterministically.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from test_marketplace_spine import _offer

from oolu.durable import DurableConnection
from oolu.durable.audit import DurableAuditLog
from oolu.marketplace import (
    NegotiationBounds,
    NegotiationLedger,
    OutsideBounds,
    QuoteRefused,
    RfqService,
    RfqSpecification,
    SalesPolicy,
    WrongState,
    proposal_violations,
)

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
M = 1_000_000


def _rig(tmp_path):
    conn = DurableConnection(tmp_path / "durable.db")
    audit = DurableAuditLog(conn)
    return RfqService(conn, audit=audit), NegotiationLedger(conn, audit=audit), conn


def _spec(**overrides) -> RfqSpecification:
    base = dict(
        category="household",
        required_attributes=(("material", "stainless-steel"), ("volume", "1l")),
        quantity=2,
    )
    base.update(overrides)
    return RfqSpecification(**base)


def test_substitutes_are_marked_ineligible_before_anything_else(tmp_path):
    rfq_service, _, conn = _rig(tmp_path)
    rfq = rfq_service.open(
        tenant="t1", buyer="user-1", specification=_spec(), now=NOW
    )
    fitting = rfq_service.submit_quote(
        rfq.rfq_id,
        tenant="t1",
        seller_principal="seller-1",
        offer=_offer(quantity=2, subtotal_micros=180 * M),
        attributes={"material": "stainless-steel", "volume": "1l"},
        now=NOW,
    )
    substitute = rfq_service.submit_quote(
        rfq.rfq_id,
        tenant="t1",
        seller_principal="seller-2",
        # Cheaper — but plastic is not what the specification requires.
        offer=_offer(
            offer_id="of-2", seller_id="planet-plastics", quantity=2,
            subtotal_micros=90 * M,
        ),
        attributes={"material": "plastic", "volume": "1l"},
        now=NOW,
    )
    assert fitting.eligible
    assert not substitute.eligible
    assert any("material" in reason for reason in substitute.ineligible_reasons)

    # Normalized comparison: eligible quotes first, despite price.
    ranked = rfq_service.quotes(rfq.rfq_id, tenant="t1")
    assert [q.quote_id for q in ranked] == [fitting.quote_id, substitute.quote_id]

    # The ineligible quote can never be awarded.
    with pytest.raises(WrongState):
        rfq_service.award(
            rfq.rfq_id, substitute.quote_id, tenant="t1", buyer="user-1", now=NOW
        )
    awarded = rfq_service.award(
        rfq.rfq_id, fitting.quote_id, tenant="t1", buyer="user-1", now=NOW
    )
    assert awarded.subtotal_micros == 180 * M
    assert rfq_service.get(rfq.rfq_id, tenant="t1").state == "awarded"
    conn.close()


def test_a_quote_below_the_absolute_floor_is_refused_without_discretion(tmp_path):
    rfq_service, _, conn = _rig(tmp_path)
    rfq = rfq_service.open(
        tenant="t1",
        buyer="user-1",
        specification=_spec(required_attributes=()),
        now=NOW,
    )
    policy = SalesPolicy(absolute_floor_micros=50 * M, price_floor_micros=80 * M)
    with pytest.raises(QuoteRefused):
        rfq_service.submit_quote(
            rfq.rfq_id,
            tenant="t1",
            seller_principal="seller-1",
            offer=_offer(quantity=2, subtotal_micros=60 * M),  # 30/unit < 50
            sales_policy=policy,
            now=NOW,
        )
    assert rfq_service.quotes(rfq.rfq_id, tenant="t1") == []
    conn.close()


def test_an_expired_rfq_takes_no_quotes(tmp_path):
    rfq_service, _, conn = _rig(tmp_path)
    rfq = rfq_service.open(
        tenant="t1",
        buyer="user-1",
        specification=_spec(required_attributes=()),
        now=NOW,
        expires_in_minutes=60,
    )
    with pytest.raises(WrongState):
        rfq_service.submit_quote(
            rfq.rfq_id,
            tenant="t1",
            seller_principal="seller-1",
            offer=_offer(quantity=2),
            now=NOW + timedelta(hours=2),
        )
    assert rfq_service.get(rfq.rfq_id, tenant="t1").state == "expired"
    conn.close()


# --------------------------------------------------------------------------- #
# Negotiation: the signed boundary is the whole authority.                     #
# --------------------------------------------------------------------------- #
def test_proposals_outside_the_signed_bounds_are_refused():
    buy = NegotiationBounds(side="buy", limit_price_micros=100 * M)
    assert proposal_violations(buy, price_micros=90 * M, round_number=1) == ()
    assert proposal_violations(buy, price_micros=110 * M, round_number=1)
    sell = NegotiationBounds(side="sell", limit_price_micros=80 * M)
    assert proposal_violations(sell, price_micros=70 * M, round_number=1)
    assert proposal_violations(
        sell, price_micros=90 * M, round_number=1, varied_terms=("warranty",)
    )  # varying warranty was never delegated


def test_a_session_exhausts_its_rounds_and_agreement_rechecks_bounds(tmp_path):
    _, negotiations, conn = _rig(tmp_path)
    session = negotiations.open(
        tenant="t1",
        principal="user-1",
        counterparty="acme",
        bounds=NegotiationBounds(
            side="buy", limit_price_micros=100 * M, maximum_rounds=2
        ),
        now=NOW,
    )
    # A probe outside the boundary costs the round AND is refused.
    with pytest.raises(OutsideBounds):
        negotiations.counter(
            session.session_id, tenant="t1", price_micros=150 * M
        )
    refused = negotiations.get(session.session_id, tenant="t1")
    assert refused.state == "refused" and refused.rounds_used == 1
    # An agent cannot COMMIT outside the bounds either, ever.
    with pytest.raises(OutsideBounds):
        negotiations.agree(
            session.session_id, tenant="t1", price_micros=150 * M
        )

    fresh = negotiations.open(
        tenant="t1",
        principal="user-1",
        counterparty="acme",
        bounds=NegotiationBounds(
            side="buy", limit_price_micros=100 * M, maximum_rounds=1
        ),
        now=NOW,
    )
    exhausted = negotiations.counter(
        fresh.session_id, tenant="t1", price_micros=95 * M
    )
    assert exhausted.state == "exhausted"
    with pytest.raises(OutsideBounds):
        negotiations.counter(
            fresh.session_id, tenant="t1", price_micros=94 * M
        )
    # An in-bounds agreement on the exhausted session still stands — the
    # rounds bounded the HAGGLING, not the signature.
    agreed = negotiations.agree(
        fresh.session_id, tenant="t1", price_micros=95 * M
    )
    assert agreed.state == "agreed"
    assert agreed.agreed_price_micros == 95 * M
    conn.close()
