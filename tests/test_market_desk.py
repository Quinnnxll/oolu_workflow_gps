"""The market desk: multimedia on the shelf, the thread as the surface,
the briefing where position meets demand, and the list-out.

Exit gate: a listing carries photos, clips, and sound as drawer refs
(the wall held at the door, true bytes served through the listing media
door once published); the Market agent stands in the roster and its
thread answers the deterministic asks — "list" names everything the
member created on the platform, "brief" names where their position
meets the market's demand; the briefing is a pure function of standing
records (approvals waiting, orders needing the member's next act, open
requests matching what they sell, quotes ripening on their own
requests, supply arriving for them); and the brief arrives proactively
as the Market agent's OWN thread message on the standing pulse — an
empty brief is silence, never invented urgency.
"""

from __future__ import annotations

from base64 import b64encode
from datetime import timedelta
from types import SimpleNamespace

from test_http_gateway import NOW, _app, _req

from oolu.durable.files import UserFileStore
from oolu.marketplace import (
    Listing,
    RequestForQuote,
    briefing_message,
    desk_briefing,
)
from oolu.marketplace.rfq import RfqSpecification
from oolu.social import AssistantHistoryStore

M = 1_000_000


# --------------------------------------------------------------------------- #
# The briefing: a pure function of standing records.                           #
# --------------------------------------------------------------------------- #
def _listing(seller, category, *, status="active", title="Quiet kettle"):
    return Listing(
        listing_id=f"L-{seller}-{category}",
        tenant_id="t1",
        seller_principal=seller,
        seller_id=seller,
        title=title,
        category=category,
        unit_price_micros=10 * M,
        created_at=NOW,
        updated_at=NOW,
        status=status,
    )


def _rfq(buyer, category, rid="r1"):
    return RequestForQuote(
        rfq_id=rid,
        tenant_id="t1",
        buyer_principal=buyer,
        specification=RfqSpecification(category=category),
        created_at=NOW,
        expires_at=NOW + timedelta(days=1),
    )


def _order(buyer, seller, state, oid="o1"):
    # The briefing reads exactly (state, buyer_principal, seller_id,
    # order_id) — the duck stands in for the full order machine.
    return SimpleNamespace(
        state=state,
        record=SimpleNamespace(
            order_id=oid, buyer_principal=buyer, seller_id=seller
        ),
    )


def test_the_briefing_reads_position_against_demand():
    items = desk_briefing(
        principal="alice",
        approvals=[{"intent_id": "i1"}],
        orders=[
            _order("bob", "alice", "confirmed", "o-ship"),  # alice ships
            _order("alice", "carol", "delivered", "o-accept"),  # alice accepts
            _order("alice", "carol", "confirmed", "o-wait"),  # carol's move
        ],
        open_rfqs=[
            _rfq("bob", "kitchen", "r-demand"),  # matches what alice sells
            _rfq("alice", "garden", "r-mine"),  # alice's own, quotes ripening
            _rfq("bob", "vehicles", "r-far"),  # nothing of alice's
        ],
        my_listings=[
            _listing("alice", "kitchen"),
            _listing("alice", "stale", status="draft"),  # drafts never match
        ],
        active_listings=[
            _listing("carol", "garden", title="Garden shears"),  # supply for r-mine
            _listing("alice", "kitchen"),  # her own — never her supply
        ],
        quote_counts={"r-mine": 2},
    )
    kinds = [(i.kind, i.ref) for i in items]
    assert ("approval", "i1") in kinds
    assert ("order", "o-ship") in kinds
    assert ("order", "o-accept") in kinds
    assert ("order", "o-wait") not in kinds  # not her move
    assert ("quote_wanted", "r-demand") in kinds
    assert ("award_wanted", "r-mine") in kinds
    assert ("quote_wanted", "r-far") not in kinds
    assert ("supply", "L-carol-garden") in kinds
    # The words are counts and names, one line per item.
    message = briefing_message(items, skipped=1)
    assert "Market desk brief" in message
    assert "positioned to quote" in message
    assert "1 earlier brief was missed" in message


def test_an_empty_position_briefs_nothing():
    assert (
        desk_briefing(
            principal="alice",
            approvals=[],
            orders=[],
            open_rfqs=[],
            my_listings=[],
            active_listings=[],
            quote_counts={},
        )
        == []
    )


# --------------------------------------------------------------------------- #
# The gateway: media on the shelf, the desk doors, the thread.                 #
# --------------------------------------------------------------------------- #
def _verified_seller(app, ident, name="user-1"):
    seller = ident.token(name)
    assert (
        app.handle(
            _req(
                "POST",
                "/v1/commerce/seller/kyc",
                token=seller,
                body={
                    "legal_name": "Acme GmbH",
                    "company_email": "kyc@acme.example",
                },
            )
        ).status
        == 201
    )
    assert (
        app.handle(
            _req(
                "POST",
                "/v1/commerce/seller/kyc/decide",
                token=ident.token("approver-1"),
                body={"principal": name, "approved": True},
            )
        ).status
        == 200
    )
    return seller


def test_listing_multimedia_rides_and_the_door_serves_true_bytes(tmp_path):
    app, conn, ident = _app(tmp_path)
    app._files = UserFileStore(conn)
    seller = _verified_seller(app, ident)
    buyer = ident.token("user-2")

    photo = b64encode(b"kettle-jpeg-bytes").decode()
    created = app.handle(
        _req(
            "POST",
            "/v1/files",
            token=seller,
            body={
                "name": "kettle.jpg",
                "content": f"data:image/jpeg;base64,{photo}",
                "media_type": "image/jpeg",
            },
        )
    )
    file_id = created.body["file_id"]
    # Another member's file cannot ride my listing — the wall holds NOW.
    walled = app.handle(
        _req(
            "POST",
            "/v1/commerce/listings",
            token=buyer,
            body={
                "title": "Not my photo",
                "unit_price_micros": M,
                "quantity_available": 1,
                "file_ids": [file_id],
            },
        )
    )
    assert walled.status == 404

    draft = app.handle(
        _req(
            "POST",
            "/v1/commerce/listings",
            token=seller,
            body={
                "title": "Quiet kettle",
                "unit_price_micros": 100 * M,
                "quantity_available": 3,
                "category": "kitchen",
                "file_ids": [file_id],
            },
        )
    )
    assert draft.status == 201
    listing_id = draft.body["listing_id"]
    assert draft.body["media"] == [
        {
            "file_id": file_id,
            "blob_ref": "",
            "media_type": "image/jpeg",
            "name": "kettle.jpg",
        }
    ]
    # A draft's media is the seller's own only; publication opens it.
    assert (
        app.handle(
            _req(
                "GET",
                f"/v1/commerce/listings/{listing_id}/media/0",
                token=buyer,
            )
        ).status
        == 404
    )
    assert (
        app.handle(
            _req(
                "POST", f"/v1/commerce/listings/{listing_id}/publish",
                token=seller,
            )
        ).status
        == 200
    )
    served = app.handle(
        _req(
            "GET", f"/v1/commerce/listings/{listing_id}/media/0", token=buyer
        )
    )
    assert served.status == 200
    assert served.body == b"kettle-jpeg-bytes"  # true bytes, decoded
    assert (
        app.handle(
            _req(
                "GET",
                f"/v1/commerce/listings/{listing_id}/media/9",
                token=buyer,
            )
        ).status
        == 404
    )
    conn.close()


def test_the_desk_and_the_thread_speak_position_list_and_brief(tmp_path):
    app, conn, ident = _app(tmp_path)
    app._files = UserFileStore(conn)
    app._assistant_history = AssistantHistoryStore(conn)
    seller = _verified_seller(app, ident)
    buyer = ident.token("user-2")

    draft = app.handle(
        _req(
            "POST",
            "/v1/commerce/listings",
            token=seller,
            body={
                "title": "Quiet kettle",
                "unit_price_micros": 100 * M,
                "quantity_available": 3,
                "category": "kitchen",
            },
        )
    )
    app.handle(
        _req(
            "POST",
            f"/v1/commerce/listings/{draft.body['listing_id']}/publish",
            token=seller,
        )
    )
    opened = app.handle(
        _req(
            "POST",
            "/v1/commerce/rfqs",
            token=buyer,
            body={"category": "kitchen", "quantity": 1, "required_attributes": {}},
        )
    )
    assert opened.status in (200, 201)

    # The seller's desk: demand matches their position.
    desk = app.handle(_req("GET", "/v1/commerce/desk", token=seller))
    assert desk.status == 200
    assert [i["kind"] for i in desk.body["items"]] == ["quote_wanted"]
    # The buyer's desk: supply stands for their request.
    desk2 = app.handle(_req("GET", "/v1/commerce/desk", token=buyer))
    assert [i["kind"] for i in desk2.body["items"]] == ["supply"]
    # A stranger's desk is honestly quiet.
    quiet = app.handle(
        _req("GET", "/v1/commerce/desk", token=ident.token("user-3"))
    )
    assert quiet.body["items"] == []

    # The list-out door: everything the member created, grouped.
    mine = app.handle(_req("GET", "/v1/commerce/mine", token=seller))
    assert len(mine.body["listings"]) == 1 and mine.body["requests"] == []
    mine2 = app.handle(_req("GET", "/v1/commerce/mine", token=buyer))
    assert mine2.body["listings"] == [] and len(mine2.body["requests"]) == 1

    # The thread speaks the same, deterministically — no model needed.
    listed = app.handle(
        _req(
            "POST",
            "/v1/chat",
            token=seller,
            body={"message": "list", "agent": "market"},
        )
    )
    assert listed.body["source"] == "desk"
    assert "Created on this platform" in listed.body["reply"]
    assert "Listings (1)" in listed.body["reply"]
    brief = app.handle(
        _req(
            "POST",
            "/v1/chat",
            token=seller,
            body={"message": "brief", "agent": "market"},
        )
    )
    assert "positioned to quote" in brief.body["reply"]
    # Ordinary conversation reaches the seat (model-less: the card).
    chat = app.handle(
        _req(
            "POST",
            "/v1/chat",
            token=seller,
            body={"message": "how do refunds work?", "agent": "market"},
        )
    )
    assert chat.body["source"] == "card"
    conn.close()


def test_the_brief_arrives_on_the_pulse_as_markets_own_message(tmp_path):
    app, conn, ident = _app(tmp_path)
    app._files = UserFileStore(conn)
    app._assistant_history = AssistantHistoryStore(conn)
    seller = _verified_seller(app, ident)
    buyer = ident.token("user-2")

    draft = app.handle(
        _req(
            "POST",
            "/v1/commerce/listings",
            token=seller,
            body={
                "title": "Quiet kettle",
                "unit_price_micros": 100 * M,
                "quantity_available": 3,
                "category": "kitchen",
            },
        )
    )
    app.handle(
        _req(
            "POST",
            f"/v1/commerce/listings/{draft.body['listing_id']}/publish",
            token=seller,
        )
    )
    app.handle(
        _req(
            "POST",
            "/v1/commerce/rfqs",
            token=buyer,
            body={"category": "kitchen", "quantity": 1, "required_attributes": {}},
        )
    )

    # One standing schedule through the one door; the desk door names it.
    scheduled = app.handle(
        _req(
            "POST",
            "/v1/commerce/desk/schedule",
            token=seller,
            body={"at_minute": 9 * 60},
        )
    )
    assert scheduled.status == 200
    assert scheduled.body["brief_schedule"] is not None
    named = app.handle(_req("GET", "/v1/commerce/desk", token=seller))
    assert named.body["brief_schedule"] is not None

    # The pulse fires on the host's clock — pinned so the seeded RFQ is
    # still live at the firing moment (the tests' one honest clock).
    app._clock = lambda: NOW + timedelta(minutes=5)
    session = SimpleNamespace(tenant_id="t1", principal_id="user-1")
    schedule = app._market_brief_schedule_row(session)
    app._fire_market_brief(schedule, NOW.isoformat(), 0)
    thread = app.handle(
        _req("GET", "/v1/chat/history", token=seller, query={"agent": "market"})
    )
    bodies = [t["body"] for t in thread.body["items"]]
    assert any("Market desk brief" in b for b in bodies)
    assert any("positioned to quote" in b for b in bodies)

    # An empty brief is silence: the buyer with nothing waiting gets no
    # message pushed, however many times the pulse fires.
    off = app.handle(
        _req(
            "POST",
            "/v1/commerce/desk/schedule",
            token=ident.token("user-3"),
            body={"at_minute": 9 * 60},
        )
    )
    assert off.status == 200
    quiet_session = SimpleNamespace(tenant_id="t1", principal_id="user-3")
    quiet_schedule = app._market_brief_schedule_row(quiet_session)
    app._fire_market_brief(quiet_schedule, NOW.isoformat(), 0)
    silent = app.handle(
        _req(
            "GET",
            "/v1/chat/history",
            token=ident.token("user-3"),
            query={"agent": "market"},
        )
    )
    assert silent.body["items"] == []
    # Turning it off removes the standing schedule.
    disabled = app.handle(
        _req(
            "POST",
            "/v1/commerce/desk/schedule",
            token=seller,
            body={"enabled": False},
        )
    )
    assert disabled.body["brief_schedule"] is None
    conn.close()
