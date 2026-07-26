"""The travel desk (A7): time, people, and budget — constraints with names.

Exit gate (agents-expansion plan, phase A7): free-busy sharing is
opt-in per peer and the wire shape reveals busy/free intervals ONLY;
an itinerary violating a hard constraint is marked infeasible with the
named reason and never outranks a feasible plan; group polls are A3's
polls unchanged (their laws are pinned in test_polls.py); bookings walk
the standing approval path (the declined-approval/reservation-release
discipline is the marketplace's own pinned law) and a confirmed trip
lands as calendar events; and the travel desk composes over typed data
— the import scan shows it touches no conversation transcript.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_http_gateway import _app, _req

from oolu.durable.connection import DurableConnection
from oolu.explorer import TravelConstraints, plan_trip
from oolu.explorer.comparisons import ComparisonRow
from oolu.gateway import GatewayApp
from oolu.identity import LocalAccountService, LocalUserStore
from oolu.records import (
    CalendarStore,
    FreeBusyGrants,
    RecordsError,
    busy_intervals,
    common_free,
)
from oolu.settings_node import SettingsNode, SettingsStore

NOW = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
WEEK = (NOW, NOW + timedelta(days=14))


# --------------------------------------------------------------------------- #
# The calendar record model — the survey's hard wall, closed.                  #
# --------------------------------------------------------------------------- #
def _calendar(tmp_path):
    conn = DurableConnection(tmp_path / "cal.db")
    return conn, CalendarStore(conn, clock=lambda: NOW)


def test_events_are_typed_bounded_and_owner_walled(tmp_path):
    conn, store = _calendar(tmp_path)
    event = store.add(
        tenant="t1", owner="alice", title="Dentist",
        starts_at=NOW + timedelta(days=1),
        ends_at=NOW + timedelta(days=1, hours=1),
    )
    assert event.source == "member"
    with pytest.raises(RecordsError, match="ends after it starts"):
        store.add(tenant="t1", owner="alice", title="Backwards",
                  starts_at=NOW, ends_at=NOW - timedelta(hours=1))
    with pytest.raises(RecordsError, match="needs a title"):
        store.add(tenant="t1", owner="alice", title="  ",
                  starts_at=NOW, ends_at=NOW + timedelta(hours=1))
    # Owner-walled, both directions.
    assert store.between(tenant="t1", owner="bob", start=NOW,
                         end=NOW + timedelta(days=7)) == []
    assert store.delete(event.event_id, tenant="t1", owner="bob") is False
    assert store.delete(event.event_id, tenant="t1", owner="alice") is True
    conn.close()


def test_busy_intervals_merge_clip_and_carry_no_titles(tmp_path):
    conn, store = _calendar(tmp_path)
    store.add(tenant="t1", owner="alice", title="SECRET meeting",
              starts_at=NOW + timedelta(hours=1),
              ends_at=NOW + timedelta(hours=3))
    store.add(tenant="t1", owner="alice", title="SECRET lunch",
              starts_at=NOW + timedelta(hours=2),
              ends_at=NOW + timedelta(hours=4))
    events = store.between(tenant="t1", owner="alice", start=NOW,
                           end=NOW + timedelta(days=1))
    blocks = busy_intervals(events, start=NOW, end=NOW + timedelta(days=1))
    # Overlaps merged into one block...
    assert blocks == [(NOW + timedelta(hours=1), NOW + timedelta(hours=4))]
    # ...and the shape is intervals, nothing else — the privacy law as a
    # type: no dict, no title field, no place for one.
    assert all(isinstance(b, tuple) and len(b) == 2 for b in blocks)
    conn.close()


def test_common_free_finds_the_groups_shared_slots():
    day = timedelta(days=1)
    busy = {
        "alice": [(NOW + 2 * day, NOW + 4 * day)],
        "bob": [(NOW + 6 * day, NOW + 7 * day)],
    }
    slots = common_free(busy, start=NOW, end=NOW + 10 * day, min_length=2 * day)
    assert (NOW, NOW + 2 * day) in slots  # before alice's block
    assert (NOW + 7 * day, NOW + 10 * day) in slots  # after bob's
    # A three-day minimum drops the two-day gap between the blocks.
    tight = common_free(busy, start=NOW, end=NOW + 10 * day, min_length=3 * day)
    assert (NOW + 4 * day, NOW + 6 * day) not in tight


def test_grants_are_per_peer_and_revocable(tmp_path):
    conn = DurableConnection(tmp_path / "grants.db")
    grants = FreeBusyGrants(conn)
    assert grants.allows(tenant="t1", owner="alice", peer="alice") is True
    assert grants.allows(tenant="t1", owner="alice", peer="bob") is False
    grants.grant(tenant="t1", owner="alice", peer="bob")
    assert grants.allows(tenant="t1", owner="alice", peer="bob") is True
    assert grants.allows(tenant="t1", owner="alice", peer="carol") is False
    grants.revoke(tenant="t1", owner="alice", peer="bob")
    assert grants.allows(tenant="t1", owner="alice", peer="bob") is False
    conn.close()


# --------------------------------------------------------------------------- #
# The brief: constraints are walls with names, never weights.                  #
# --------------------------------------------------------------------------- #
def _row(listing_id, *, price, gaps=()):
    neutral = {"count": 0, "mean": None, "factor": 0.5}
    return ComparisonRow(
        listing_id=listing_id,
        title=f"Trip {listing_id}",
        seller="agency",
        price_micros=price,
        currency="USD",
        list_price_micros=None,
        discount_percent=None,
        feedback=neutral,
        trust={"score": 0.5, "basis": "no order history"},
        lab={"count": 0, "mean_score": None, "factor": 0.5},
        eligible=not gaps,
        gaps=tuple(gaps),
    )


def _constraints(*, party=("alice", "bob"), budget=100_000_000):
    return TravelConstraints(
        window_start=WEEK[0],
        window_end=WEEK[1],
        nights=3,
        party=party,
        budget_micros=budget,
    )


def test_infeasible_is_named_and_never_outranks_feasible():
    rows = [
        _row("cheap", price=20_000_000),
        _row("grand", price=90_000_000),  # 2 × 90 = 180M > the 100M budget
    ]
    slots = [(WEEK[0], WEEK[0] + timedelta(days=5))]
    brief = plan_trip(rows, constraints=_constraints(), open_slots=slots)
    assert [c.listing_id for c in brief.feasible] == ["cheap"]
    [grand] = brief.infeasible
    assert grand.feasible is False
    assert any("over budget by 80.00 USD" in v for v in grand.violations)
    # However the grand trip scores, it never ranks among the feasible.
    assert all(c.score is not None for c in brief.feasible)
    # And with no shared window, EVERYTHING is infeasible, by name.
    stuck = plan_trip(rows, constraints=_constraints(), open_slots=[])
    assert stuck.feasible == ()
    assert all(
        any("no common free window of 3 nights" in v for v in c.violations)
        for c in stuck.infeasible
    )


def test_the_brief_is_deterministic():
    rows = [_row("a", price=30_000_000), _row("b", price=20_000_000)]
    slots = [(WEEK[0], WEEK[1])]
    once = plan_trip(rows, constraints=_constraints(), open_slots=slots)
    again = plan_trip(rows, constraints=_constraints(), open_slots=slots)
    assert once == again
    assert once.feasible[0].listing_id == "b"  # cheapest wins balanced+neutral
    with pytest.raises(ValueError, match="unknown mode"):
        plan_trip(rows, constraints=_constraints(), open_slots=slots,
                  mode="vibes")


# --------------------------------------------------------------------------- #
# The gateway: consent by name, the wire shape, the trip on the calendar.      #
# --------------------------------------------------------------------------- #
def _host(tmp_path):
    app, conn, ident = _app(tmp_path)
    users = LocalUserStore(":memory:")
    accounts = LocalAccountService(users, ident.store, ident._signer)
    for name in ("alice", "bob", "shop"):
        accounts.create_user(name, f"{name}-password-1", tenant="t1")
    gateway = GatewayApp(
        app._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        accounts=accounts,
        settings_node=SettingsNode(SettingsStore(conn)),
    )
    gateway._commerce_seller_kyc.apply(
        tenant="t1", principal="shop", legal_name="Shop Ltd",
        company_email="sales@shop-ltd.example",
    )
    gateway._commerce_seller_kyc.decide(
        tenant="t1", principal="shop", reviewer="rev", approved=True
    )
    return gateway, conn, ident


def _publish_trip(gateway, ident, *, title, price):
    shop = ident.token("shop")
    draft = gateway.handle(
        _req("POST", "/v1/commerce/listings", token=shop, body={
            "title": title,
            "unit_price_micros": price,
            "category": "travel",
            "quantity_available": 4,
        })
    )
    assert draft.status == 201
    listing_id = draft.body["listing_id"]
    assert (
        gateway.handle(
            _req("POST", f"/v1/commerce/listings/{listing_id}/publish",
                 token=shop)
        ).status
        == 200
    )
    return listing_id


def test_the_travel_flow_end_to_end(tmp_path):
    gateway, conn, ident = _host(tmp_path)
    alice, bob = ident.token("alice"), ident.token("bob")
    _publish_trip(gateway, ident, title="Coast package", price=20_000_000)
    _publish_trip(gateway, ident, title="Alpine package", price=90_000_000)

    # Bob's calendar has a block in the window.
    busy_start = (NOW + timedelta(days=3)).isoformat()
    busy_end = (NOW + timedelta(days=5)).isoformat()
    assert (
        gateway.handle(
            _req("POST", "/v1/records/calendar", token=bob, body={
                "title": "Bob's SECRET conference",
                "starts_at": busy_start,
                "ends_at": busy_end,
            })
        ).status
        == 201
    )

    # Consent by name: alice cannot plan over bob's time until he grants.
    plan_query = {
        "window_start": NOW.isoformat(),
        "window_end": (NOW + timedelta(days=14)).isoformat(),
        "nights": "3",
        "party": "alice,bob",
        "budget_micros": "100000000",
    }
    refused = gateway.handle(
        _req("GET", "/v1/travel/plan", token=alice, query=plan_query)
    )
    assert refused.status == 403
    assert "bob has not shared" in refused.body["error"]["message"]

    granted = gateway.handle(
        _req("POST", "/v1/records/freebusy/grants", token=bob,
             body={"peer": "alice"})
    )
    assert granted.body["granted_to"] == ["alice"]

    planned = gateway.handle(
        _req("GET", "/v1/travel/plan", token=alice, query=plan_query)
    )
    assert planned.status == 200
    brief = planned.body
    # The privacy wire shape: the open slots are bare interval pairs —
    # bob's block shaped them, but his title crosses nowhere.
    assert brief["open_slots"], "the group still shares free windows"
    assert "SECRET" not in str(brief)
    # Feasible ranks; the over-budget package stands aside, named.
    assert [c["listing_id"] for c in brief["feasible"]]
    assert brief["feasible"][0]["title"] == "Coast package"
    [alpine] = brief["infeasible"]
    assert any("over budget" in v for v in alpine["violations"])
    assert alpine["factors"]  # the breakdown rides even the refused

    # The trip lands on the calendar only from a FINISHED order of YOURS.
    ghost = gateway.handle(
        _req("POST", "/v1/travel/confirm", token=alice, body={
            "order_id": "nope",
            "starts_at": NOW.isoformat(),
            "nights": 3,
        })
    )
    assert ghost.status == 404
    conn.close()


def test_a_finished_booking_lands_as_a_trip_event(tmp_path):
    from oolu.marketplace.models import Offer
    from oolu.marketplace.orders import OrderRecord

    gateway, conn, ident = _host(tmp_path)
    alice = ident.token("alice")
    listing_id = _publish_trip(
        gateway, ident, title="Coast package", price=20_000_000
    )
    # A finished order of alice's, straight into the durable book (the
    # walk TO this state — intent, digest, approval, capture, inventory
    # release on decline — is the marketplace's own pinned machinery).
    record = OrderRecord(
        order_id="order-1",
        tenant_id="t1",
        buyer_principal="alice",
        seller_id="shop",
        seller_principal="shop",
        intent_id="i1",
        authorization_id="a1",
        intent_digest="d1",
        offer=Offer(
            offer_id="of1",
            seller_id="shop",
            offer_version=1,
            item_id=listing_id,
            quantity=2,
            subtotal_micros=40_000_000,
        ),
        idempotency_key="k1",
        take_rate_bps=500,
        created_at=NOW,
    )
    gateway._commerce_orders.orders.add(record, state="accepted")
    confirmed = gateway.handle(
        _req("POST", "/v1/travel/confirm", token=alice, body={
            "order_id": "order-1",
            "starts_at": (NOW + timedelta(days=7)).isoformat(),
            "nights": 3,
        })
    )
    assert confirmed.status == 201, confirmed.body
    assert confirmed.body["source"] == "trip"
    assert confirmed.body["title"] == "Trip: Coast package"
    # And it stands on the member's own calendar records.
    events = gateway.handle(
        _req("GET", "/v1/records/calendar", token=alice)
    )
    assert [e["title"] for e in events.body["items"]] == [
        "Trip: Coast package"
    ]
    # A pending order books no calendar.
    gateway._commerce_orders.orders.add(
        record.model_copy(update={"order_id": "order-2",
                                  "intent_id": "i2",
                                  "idempotency_key": "k2"}),
        state="placed",
    )
    pending = gateway.handle(
        _req("POST", "/v1/travel/confirm", token=alice, body={
            "order_id": "order-2",
            "starts_at": NOW.isoformat(),
            "nights": 2,
        })
    )
    assert pending.status == 400
    assert "once the booking stands" in pending.body["error"]["message"]
    conn.close()


def test_the_travel_desk_touches_no_transcript():
    """Typed batons, never transcripts: the travel module reads
    Explorer rows and calendar intervals — no chat, no social, no
    assistant history, and none of the standing outside walls."""
    src = Path(__file__).resolve().parents[1] / "src" / "oolu"
    forbidden = (
        "chat",
        "social",
        "assistant",
        "adhouse",
        "http",
        "urllib",
        "requests",
        "socket",
        "providers",
    )
    for package in ("explorer", "records"):
        for source in sorted((src / package).glob("*.py")):
            for number, line in enumerate(
                source.read_text().splitlines(), start=1
            ):
                stripped = line.strip()
                if stripped.startswith(("from ", "import ")):
                    for word in forbidden:
                        assert word not in stripped, (
                            f"{package}/{source.name}:{number}: {stripped}"
                        )
