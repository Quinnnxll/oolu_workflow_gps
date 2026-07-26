"""M4: the open market — other markets' offers, our law, our ledger.

Exit gates (docs/marketplace-build-plan.md §5 M4): an external offer with
a broken or unsigned wire contract never reaches the intent door; a
cross-market purchase is still a typed intent with a digest — changed
terms kill the approval across the federation boundary too; the platform's
share posts on cross-market settlements exactly like local ones; the
compliance deployment gate crosses the boundary (a peer jurisdiction with
no configured module refuses import); suspension blocks a peer
immediately; the sourcing sweep is one normalized, eligibility-marked
comparison across every shelf; and a financing partner's product is a
recurring offer that cannot skip the ladder.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from test_marketplace_escrow import _rig
from test_marketplace_spine import _SAFE_RISK, _offer

from oolu.billing.tax import JurisdictionModule, TaxRegistry
from oolu.marketplace import (
    CatalogService,
    Decision,
    DigestMismatch,
    FederationDesk,
    ListingUnavailable,
    MarketNotFound,
    ProtocolViolation,
    RfqSpecification,
    SimpleInterestFinancing,
    WrongState,
    financing_offer,
    sign_offer,
    verify_offer,
)

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
M = 1_000_000
US = JurisdictionModule(code="US", tax_rate_bps=0)
DE = JurisdictionModule(code="DE", tax_rate_bps=1900)
SECRET = "the-partner-markets-shared-secret"


def _desk(conn, audit, *, jurisdictions=(US, DE), secrets=None):
    return FederationDesk(
        conn,
        audit=audit,
        tax=TaxRegistry(jurisdictions),
        secrets=secrets if secrets is not None else {"partner-market": SECRET},
    )


def _signed(**overrides):
    return sign_offer(
        _offer(**overrides), peer_id="partner-market", secret=SECRET
    )


# --------------------------------------------------------------------------- #
# The wire contract.                                                           #
# --------------------------------------------------------------------------- #
def test_the_signature_binds_every_material_term():
    offer = _signed()
    assert verify_offer(offer, peer_id="partner-market", secret=SECRET)
    repriced = offer.model_copy(update={"subtotal_micros": 90 * M})
    assert not verify_offer(repriced, peer_id="partner-market", secret=SECRET)
    assert not verify_offer(offer, peer_id="someone-else", secret=SECRET)
    assert not verify_offer(offer, peer_id="partner-market", secret="wrong")
    assert not verify_offer(_offer(), peer_id="partner-market", secret=SECRET)


def test_tampered_or_unsigned_offers_never_reach_the_shelf(tmp_path):
    conn, audit, spine, orders, ledger, _ = _rig(tmp_path)
    desk = _desk(conn, audit)
    desk.register_peer(
        peer_id="partner-market", name="Partner", jurisdiction="DE", now=NOW
    )
    with pytest.raises(ProtocolViolation):
        desk.import_offer("partner-market", _offer(), now=NOW)  # unsigned
    tampered = _signed().model_copy(update={"subtotal_micros": 90 * M})
    with pytest.raises(ProtocolViolation):
        desk.import_offer("partner-market", tampered, now=NOW)
    assert desk.imported() == []
    assert any(
        r.event_type == "market.federation.offer_rejected"
        for r in audit.records()
    )
    conn.close()


def test_peers_must_be_known_configured_and_active(tmp_path):
    conn, audit, spine, orders, ledger, _ = _rig(tmp_path)
    desk = _desk(conn, audit, jurisdictions=(US,))  # no DE module
    with pytest.raises(MarketNotFound):
        desk.import_offer("partner-market", _signed(), now=NOW)
    desk.register_peer(
        peer_id="partner-market", name="Partner", jurisdiction="DE", now=NOW
    )
    # The compliance deployment gate crosses the federation boundary.
    with pytest.raises(ListingUnavailable, match="jurisdiction"):
        desk.import_offer("partner-market", _signed(), now=NOW)
    # A peer with no shared secret can announce nothing this host trusts.
    bare = _desk(conn, audit, secrets={})
    bare.register_peer(
        peer_id="partner-market", name="Partner", jurisdiction="US", now=NOW
    )
    with pytest.raises(ProtocolViolation, match="secret"):
        bare.import_offer("partner-market", _signed(), now=NOW)
    # Suspension blocks new imports immediately.
    ready = _desk(conn, audit)
    ready.register_peer(
        peer_id="partner-market", name="Partner", jurisdiction="DE", now=NOW
    )
    ready.set_peer_state("partner-market", state="suspended")
    with pytest.raises(WrongState, match="suspended"):
        ready.import_offer("partner-market", _signed(), now=NOW)
    conn.close()


# --------------------------------------------------------------------------- #
# A cross-market purchase is still a typed intent with a digest.               #
# --------------------------------------------------------------------------- #
def test_a_cross_market_purchase_walks_the_spine_and_pays_the_take(tmp_path):
    conn, audit, spine, orders, ledger, _ = _rig(tmp_path)
    desk = _desk(conn, audit)
    desk.register_peer(
        peer_id="partner-market", name="Partner", jurisdiction="DE", now=NOW
    )
    imported = desk.import_offer(
        "partner-market", _signed(), seller_attested=True, now=NOW
    )
    stored, _ = spine.create_purchase_intent(
        tenant="t1",
        principal="user-1",
        agent="oolu",
        offer=imported.offer,
        idempotency_key="x-market-1",
        now=NOW,
        category="household",
        delivery_destination="home",
        risk_facts={
            **_SAFE_RISK,
            "seller_identity_verified": imported.seller_attested,
        },
    )
    spine.record_approval(
        stored.intent.intent_id,
        tenant="t1",
        approver_id="user-1",
        assurance_level=1,
        approve=True,
        now=NOW,
    )
    # The peer re-signs at a higher price: digest law, across the border.
    repriced = sign_offer(
        _offer(offer_version=2, subtotal_micros=140 * M),
        peer_id="partner-market",
        secret=SECRET,
    )
    with pytest.raises(DigestMismatch):
        orders.place_order(
            intent_id=stored.intent.intent_id,
            tenant="t1",
            now=NOW,
            customer_ref="cus_1",
            payment_method_ref="pm_ok",
            current_offer=repriced,
        )
    # The exact approved terms settle — on OUR ledger, with OUR take.
    order, _ = orders.place_order(
        intent_id=stored.intent.intent_id,
        tenant="t1",
        now=NOW,
        customer_ref="cus_1",
        payment_method_ref="pm_ok",
        current_offer=imported.offer,
    )
    order_id = order.record.order_id
    orders.mark_shipped(order_id, tenant="t1", actor="user-1", now=NOW)
    orders.mark_delivered(
        order_id, tenant="t1", actor="user-1", now=NOW, evidence="customs-doc"
    )
    orders.accept(order_id, tenant="t1", actor="user-1", now=NOW)
    assert ledger.take_micros() == 5 * M  # 500 bps on the 100 subtotal
    assert sum(ledger.trial_balance().values()) == 0
    conn.close()


# --------------------------------------------------------------------------- #
# The sourcing sweep: every shelf, one honest comparison.                      #
# --------------------------------------------------------------------------- #
def test_sourcing_compares_local_and_federated_shelves_with_gaps_named(
    tmp_path,
):
    conn, audit, spine, orders, ledger, inventory = _rig(tmp_path)
    catalog = CatalogService(
        conn,
        audit=audit,
        seller_verified=lambda t, s: True,
        inventory=inventory,
        tax=TaxRegistry((US,)),
        jurisdiction="US",
    )
    listing = catalog.create_draft(
        tenant="t1",
        seller_principal="seller-1",
        seller_id="local-acme",
        title="Steel bottle",
        category="household",
        unit_price_micros=110 * M,
        quantity_available=5,
        now=NOW,
    )
    catalog.publish(listing.listing_id, tenant="t1", seller="seller-1", now=NOW)

    desk = _desk(conn, audit)
    desk.register_peer(
        peer_id="partner-market", name="Partner", jurisdiction="DE", now=NOW
    )
    desk.import_offer(
        "partner-market",
        _signed(offer_id="of-cheap", subtotal_micros=90 * M, tax_estimate_micros=0),
        attributes={"material": "stainless-steel"},
        now=NOW,
    )
    desk.import_offer(
        "partner-market",
        _signed(
            offer_id="of-plastic",
            seller_id="planet-plastics",
            subtotal_micros=50 * M,
            tax_estimate_micros=0,
        ),
        attributes={"material": "plastic"},
        now=NOW,
    )
    rows = desk.source(
        RfqSpecification(
            category="household",
            required_attributes=(("material", "stainless-steel"),),
            quantity=1,
        ),
        catalog=catalog,
        now=NOW,
    )
    # Exactly one row is eligible: the attested steel offer leads, and no
    # ineligible row outranks it on price.
    assert rows[0].origin == "peer:partner-market"
    assert rows[0].eligible
    assert [row.eligible for row in rows[1:]] == [False, False]
    # The local shelf attests no attributes: it is judged by the same bar.
    local = next(r for r in rows if r.origin.startswith("local:"))
    assert any("material" in gap for gap in local.gaps)
    substitute = next(r for r in rows if r.offer.offer_id == "of-plastic")
    assert not substitute.eligible
    assert any("material" in gap for gap in substitute.gaps)
    conn.close()


# --------------------------------------------------------------------------- #
# The live wire: two real hosts trade signed offers in-process.                #
# --------------------------------------------------------------------------- #
class _InProcessWire:
    """A PeerTransport that walks straight into the other host's gateway."""

    def __init__(self, app, token):
        self._app = app
        self._token = token

    def fetch(self, peer, *, self_identity, category=""):
        from test_http_gateway import _req

        response = self._app.handle(
            _req(
                "GET",
                "/v1/commerce/announcements",
                token=self._token,
                query={"peer_id": self_identity, "category": category},
            )
        )
        if response.status != 200:
            raise WrongState(f"announcements answered {response.status}")
        return response.body


def test_two_hosts_trade_signed_offers_over_the_wire(tmp_path):
    from test_http_gateway import _app, _req

    from oolu.gateway import GatewayApp

    secret = "the-agreed-pairing-secret"
    # Host A: the selling market. It announces as "host-a" and holds the
    # secret it shares with host-b.
    bare_a, conn_a, ident = _app(tmp_path, path=tmp_path / "host-a.db")
    host_a = GatewayApp(
        bare_a._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        commerce_peer_identity="host-a",
        commerce_peer_secrets={"host-b": secret},
    )
    seller = ident.token("user-1")
    host_a.handle(
        _req(
            "POST",
            "/v1/commerce/seller/kyc",
            token=seller,
            body={"legal_name": "Acme GmbH", "company_email": "kyc@acme.example"},
        )
    )
    host_a.handle(
        _req(
            "POST",
            "/v1/commerce/seller/kyc/decide",
            token=ident.token("approver-1"),
            body={"principal": "user-1", "approved": True},
        )
    )
    draft = host_a.handle(
        _req(
            "POST",
            "/v1/commerce/listings",
            token=seller,
            body={
                "title": "Steel bottle",
                "category": "household",
                "unit_price_micros": 100 * M,
                "quantity_available": 3,
            },
        )
    )
    host_a.handle(
        _req(
            "POST",
            f"/v1/commerce/listings/{draft.body['listing_id']}/publish",
            token=seller,
        )
    )

    # Host B: the buying market. It knows host-a under that identity, the
    # same shared secret, and reaches it through the in-process wire.
    bare_b, conn_b, _ = _app(tmp_path, path=tmp_path / "host-b.db", ident=ident)
    host_b = GatewayApp(
        bare_b._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        commerce_peer_identity="host-b",
        commerce_peer_secrets={"host-a": secret},
        commerce_peer_transport=_InProcessWire(host_a, seller),
    )
    buyer = ident.token("user-2")
    host_b.handle(
        _req(
            "POST",
            "/v1/commerce/peers",
            token=ident.token("admin-1"),
            body={"peer_id": "host-a", "name": "Host A", "jurisdiction": "LOCAL"},
        )
    )
    fetched = host_b.handle(
        _req("POST", "/v1/commerce/peers/host-a/fetch", token=buyer, body={})
    )
    assert fetched.status == 200, fetched.body
    assert fetched.body["rejected"] == 0
    assert len(fetched.body["imported"]) == 1
    offer = fetched.body["imported"][0]["offer"]
    assert offer["signature"].startswith("a2a-v1:host-a:")
    # The fetched offer is on host B's sourcing sweep, attested.
    sourced = host_b.handle(
        _req(
            "GET",
            "/v1/commerce/source",
            token=buyer,
            query={"category": "household", "quantity": "1"},
        )
    )
    assert [row["origin"] for row in sourced.body["items"]] == ["peer:host-a"]
    assert sourced.body["items"][0]["seller_attested"] is True
    conn_a.close()
    conn_b.close()


def test_the_wire_rejects_what_the_door_would_reject(tmp_path):
    conn, audit, spine, orders, ledger, _ = _rig(tmp_path)
    desk = _desk(conn, audit)
    desk.register_peer(
        peer_id="partner-market", name="Partner", jurisdiction="DE", now=NOW
    )

    from oolu.marketplace import fetch_from_peer
    from oolu.marketplace.protocol import PROTOCOL_VERSION

    class _LyingWire:
        def fetch(self, peer, *, self_identity, category=""):
            good = _signed()
            tampered = _signed(offer_id="of-2").model_copy(
                update={"subtotal_micros": 1}
            )
            return {
                "protocol": PROTOCOL_VERSION,
                "peer_id": "partner-market",
                "items": [
                    {"offer": good.model_dump(mode="json"), "seller_attested": True},
                    {"offer": tampered.model_dump(mode="json")},
                    {"offer": "not-an-offer"},
                ],
            }

    imported, rejected = fetch_from_peer(
        desk,
        "partner-market",
        transport=_LyingWire(),
        self_identity="us",
        now=NOW,
    )
    assert len(imported) == 1 and rejected == 2

    class _WrongProtocol:
        def fetch(self, peer, *, self_identity, category=""):
            return {"protocol": "b2b-v9", "items": []}

    with pytest.raises(ProtocolViolation, match="b2b-v9"):
        fetch_from_peer(
            desk,
            "partner-market",
            transport=_WrongProtocol(),
            self_identity="us",
            now=NOW,
        )
    conn.close()


def test_the_http_peer_transport_speaks_the_announcements_door():
    from test_chat_model_router import FakeTransport

    from oolu.marketplace import HttpPeerTransport, PeerMarket

    fake = FakeTransport()
    fake.script("peer.example", 200, {"protocol": "a2a-v1", "items": []})
    wire = HttpPeerTransport(fake)
    peer = PeerMarket(
        peer_id="partner-market",
        name="Partner",
        base_url="https://peer.example",
        jurisdiction="DE",
        registered_at=NOW,
    )
    payload = wire.fetch(peer, self_identity="host-b", category="household")
    assert payload["protocol"] == "a2a-v1"
    url = fake.requests[-1]["url"]
    assert url.startswith("https://peer.example/v1/commerce/announcements")
    assert "peer_id=host-b" in url and "category=household" in url
    fake.script("peer.example", 503, {})
    with pytest.raises(WrongState, match="503"):
        wire.fetch(peer, self_identity="host-b")


# --------------------------------------------------------------------------- #
# HTTP partner adapters: real quotes over the provider seam.                   #
# --------------------------------------------------------------------------- #
def test_the_http_financing_partner_speaks_the_documented_wire():
    from test_chat_model_router import FakeTransport

    from oolu.marketplace import HttpFinancingPartner
    from oolu.marketplace.partnerwire import PartnerError
    from oolu.providers.vault import SecretVault

    vault = SecretVault()
    ref = vault.put("pk_lend_secret", kind="partner")
    fake = FakeTransport()
    fake.script(
        "lend.example",
        200,
        {
            "principal_micros": 1_200 * M,
            "installments": 12,
            "period_days": 30,
            "installment_micros": 112 * M,
            "total_repay_micros": 1_344 * M,
            "currency": "USD",
        },
    )
    partner = HttpFinancingPartner(
        partner_id="lend-co",
        vault=vault,
        transport=fake,
        api_key_ref=ref,
        base_url="https://lend.example",
    )
    quote = partner.quote(
        principal_micros=1_200 * M, installments=12, period_days=30
    )
    assert quote.partner_id == "lend-co"
    assert quote.installment_micros == 112 * M
    request = fake.requests[-1]
    assert request["url"] == "https://lend.example/quotes"
    assert request["headers"]["Authorization"] == "Bearer pk_lend_secret"
    assert "pk_lend_secret" not in repr(vars(partner))
    # A quote for a different principal than asked is refused.
    fake.script(
        "lend.example",
        200,
        {
            "principal_micros": 999 * M,
            "installments": 12,
            "period_days": 30,
            "installment_micros": 100 * M,
            "total_repay_micros": 1_200 * M,
        },
    )
    with pytest.raises(PartnerError, match="different principal"):
        partner.quote(
            principal_micros=1_200 * M, installments=12, period_days=30
        )
    # Nonsense is an error, never a guess; refusals surface plainly.
    fake.script("lend.example", 200, {"hello": "world"})
    with pytest.raises(PartnerError, match="not a financing quote"):
        partner.quote(
            principal_micros=1_200 * M, installments=12, period_days=30
        )
    fake.script("lend.example", 500, {})
    with pytest.raises(PartnerError, match="500"):
        partner.quote(
            principal_micros=1_200 * M, installments=12, period_days=30
        )


# --------------------------------------------------------------------------- #
# Supply orchestration: sourcing that survives a no.                           #
# --------------------------------------------------------------------------- #
def test_procurement_picks_the_best_source_and_falls_back_on_failure(tmp_path):
    from oolu.marketplace import SupplyOrchestrator

    conn, audit, spine, orders, ledger, inventory = _rig(tmp_path)
    catalog = CatalogService(
        conn,
        audit=audit,
        seller_verified=lambda t, s: True,
        inventory=inventory,
        tax=TaxRegistry((US,)),
        jurisdiction="US",
    )
    listing = catalog.create_draft(
        tenant="t1",
        seller_principal="seller-1",
        seller_id="local-acme",
        title="Steel bottle",
        category="household",
        unit_price_micros=100 * M,
        quantity_available=1,  # one unit: the shelf will empty
        now=NOW,
    )
    catalog.publish(listing.listing_id, tenant="t1", seller="seller-1", now=NOW)
    desk = _desk(conn, audit)
    desk.register_peer(
        peer_id="partner-market", name="Partner", jurisdiction="DE", now=NOW
    )
    desk.import_offer(
        "partner-market",
        _signed(offer_id="of-backup", subtotal_micros=120 * M, tax_estimate_micros=0),
        seller_attested=True,
        now=NOW,
    )
    orchestrator = SupplyOrchestrator(
        federation=desk, spine=spine, audit=audit, catalog=catalog
    )
    specification = RfqSpecification(category="household", quantity=1)
    risk = {
        "first_time_counterparty": False,
        "new_delivery_destination": False,
        "price_benchmark_micros": 100 * M,
    }
    first, intent_one = orchestrator.procure(
        specification,
        tenant="t1",
        principal="user-1",
        agent="oolu",
        idempotency_key="proc-1",
        now=NOW,
        delivery_destination="home",
        risk_facts=dict(risk),
    )
    assert first.origin == f"local:{listing.listing_id}"  # cheapest eligible
    # The last unit vanishes (another buyer took it): the shelf empties.
    inventory.reserve(listing.listing_id, quantity=1, holder="rival", now=NOW)
    fallback, intent_two = orchestrator.procure(
        specification,
        tenant="t1",
        principal="user-1",
        agent="oolu",
        idempotency_key="proc-2",
        now=NOW,
        delivery_destination="home",
        risk_facts=dict(risk),
        exclude_origins=frozenset({first.origin}),
    )
    assert fallback.origin == "peer:partner-market"
    assert intent_two.intent.offer_snapshot.offer_id == "of-backup"
    with pytest.raises(WrongState, match="no eligible source"):
        orchestrator.procure(
            specification,
            tenant="t1",
            principal="user-1",
            agent="oolu",
            idempotency_key="proc-3",
            now=NOW,
            exclude_origins=frozenset({first.origin, "peer:partner-market"}),
        )
    assert any(
        r.event_type == "market.supply.exhausted" for r in audit.records()
    )
    conn.close()


# --------------------------------------------------------------------------- #
# Partners: a financing plan is a recurring offer — the ladder stands.         #
# --------------------------------------------------------------------------- #
def test_financing_enters_as_a_recurring_offer_and_cannot_skip_the_ladder(
    tmp_path,
):
    partner = SimpleInterestFinancing(partner_id="lend-co", rate_bps=1200)
    quote = partner.quote(
        principal_micros=1_200 * M, installments=12, period_days=30
    )
    assert quote.total_repay_micros >= 1_344 * M  # 12% flat, ceil rounding
    offer = financing_offer(quote)
    assert offer.recurring_terms
    assert not offer.refundable

    conn, audit, spine, orders, ledger, _ = _rig(tmp_path)
    stored, _ = spine.create_purchase_intent(
        tenant="t1",
        principal="user-1",
        agent="oolu",
        offer=offer,
        idempotency_key="fin-1",
        now=NOW,
        category="financing",
        delivery_destination="home",
        risk_facts={**_SAFE_RISK, "seller_identity_verified": True},
    )
    assert stored.state == "approval_pending"
    assert stored.verdict.decision is Decision.REQUIRE_APPROVAL
    assert any("recurring" in r for r in stored.verdict.reasons)
    assert any("non-refundable" in r for r in stored.verdict.reasons)
    conn.close()
