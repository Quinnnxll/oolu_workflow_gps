"""The explorer desk (A6.1): closest products, followable categories,
inferred lenses, and comparisons that end.

Exit gate: a listing's id is unique and one search door serves every
surface (OoLu, shop, request, Explorer) — an exact id hits exactly,
any words find the CLOSEST existing products; the categories to follow
are the ones real listings and open requests actually carry, presented
as a message block whose tap speaks; the compare lens is never a menu —
the member's words weigh first, their last decided comparison second,
"balanced" third; and a comparison is a runtime with a deadline (the
1–3 day band): one open per member, superseding, lapsing on its own.
"""

from __future__ import annotations

from datetime import timedelta

from test_http_gateway import NOW, _app, _req

from oolu.durable.connection import DurableConnection
from oolu.explorer import DECISION_TTL_HOURS, ComparisonStore, infer_mode
from oolu.social import AssistantHistoryStore

M = 1_000_000


def test_the_lens_is_read_never_asked():
    assert infer_mode("find me the cheapest kettle") == "value"
    assert infer_mode("something proven with reviews") == "proven"
    assert infer_mode("show me the lab numbers") == "measured"
    assert infer_mode("a kettle", last_mode="measured") == "measured"
    assert infer_mode("a kettle") == "balanced"
    # The member's words outrank their history.
    assert infer_mode("cheapest one", last_mode="measured") == "value"


def test_a_comparison_is_a_runtime_with_a_deadline(tmp_path):
    conn = DurableConnection(tmp_path / "cmp.db")
    tick = {"now": NOW}
    store = ComparisonStore(conn, clock=lambda: tick["now"])
    opened = store.open(
        tenant="t1", principal="a", query="kettle", mode="value",
        listing_ids=["L1"], ttl_hours=200,  # clamped into the band
    )
    assert opened["expires_at"] == (NOW + timedelta(hours=72)).isoformat()
    # A new comparison supersedes — one open per member, never a pile.
    store.open(
        tenant="t1", principal="a", query="shears", mode="proven",
        listing_ids=["L2"],
    )
    standing = store.standing(tenant="t1", principal="a")
    assert standing["query"] == "shears" and standing["state"] == "open"
    # The deadline holds by itself: past it, the comparison lapses.
    tick["now"] = NOW + timedelta(hours=DECISION_TTL_HOURS + 1)
    assert store.standing(tenant="t1", principal="a")["state"] == "expired"
    assert store.close(tenant="t1", principal="a") is False  # already over
    assert store.last_decided_mode(tenant="t1", principal="a") is None
    # A decided comparison remembers its lens for the next default.
    tick["now"] = NOW
    store.open(
        tenant="t1", principal="a", query="kettle", mode="measured",
        listing_ids=["L1"],
    )
    assert store.close(tenant="t1", principal="a") is True
    assert store.last_decided_mode(tenant="t1", principal="a") == "measured"
    conn.close()


def _verified_seller(app, ident, name="user-1"):
    seller = ident.token(name)
    app.handle(
        _req(
            "POST",
            "/v1/commerce/seller/kyc",
            token=seller,
            body={"legal_name": "Acme GmbH", "company_email": "kyc@acme.example"},
        )
    )
    app.handle(
        _req(
            "POST",
            "/v1/commerce/seller/kyc/decide",
            token=ident.token("approver-1"),
            body={"principal": name, "approved": True},
        )
    )
    return seller


def test_one_listing_one_id_one_search_for_every_surface(tmp_path):
    app, conn, ident = _app(tmp_path)
    app._assistant_history = AssistantHistoryStore(conn)
    seller = _verified_seller(app, ident)
    buyer = ident.token("user-2")

    ids = set()
    for title, category in (
        ("Quiet steel kettle", "kitchen"),
        ("Garden shears", "garden"),
    ):
        draft = app.handle(
            _req(
                "POST",
                "/v1/commerce/listings",
                token=seller,
                body={
                    "title": title,
                    "unit_price_micros": 100 * M,
                    "quantity_available": 3,
                    "category": category,
                },
            )
        )
        ids.add(draft.body["listing_id"])
        app.handle(
            _req(
                "POST",
                f"/v1/commerce/listings/{draft.body['listing_id']}/publish",
                token=seller,
            )
        )
    assert len(ids) == 2  # unique, every time
    kettle_id = next(iter(ids))

    # The one search door, from any surface's token: an exact id hits
    # exactly; close words find the closest products; junk finds none.
    by_id = app.handle(
        _req("GET", "/v1/commerce/search", token=buyer, query={"q": kettle_id})
    )
    assert [x["listing_id"] for x in by_id.body["items"]] == [kettle_id]
    close = app.handle(
        _req(
            "GET",
            "/v1/commerce/search",
            token=buyer,
            query={"q": "a quiet kettle for the kitchen"},
        )
    )
    assert close.body["items"][0]["title"] == "Quiet steel kettle"
    none = app.handle(
        _req("GET", "/v1/commerce/search", token=buyer, query={"q": "zzz qqq"})
    )
    assert none.body["items"] == []

    # The categories that EXIST are the followable ones.
    categories = app.handle(
        _req("GET", "/v1/explorer/categories", token=buyer)
    )
    assert [c["category"] for c in categories.body["items"]] == [
        "garden",
        "kitchen",
    ]

    # The Explorer thread: the block whose tap speaks, the follow, the
    # search with an inferred lens and a real deadline, the decide.
    chips = app.handle(
        _req(
            "POST",
            "/v1/chat",
            token=buyer,
            body={"message": "categories", "agent": "explorer"},
        )
    )
    assert chips.body["block"]["kind"] == "categories"
    followed = app.handle(
        _req(
            "POST",
            "/v1/chat",
            token=buyer,
            body={"message": "follow kitchen", "agent": "explorer"},
        )
    )
    assert "Following “kitchen”" in followed.body["reply"]
    again = app.handle(
        _req("GET", "/v1/explorer/categories", token=buyer)
    )
    assert {c["category"]: c["followed"] for c in again.body["items"]} == {
        "garden": False,
        "kitchen": True,
    }
    unknown = app.handle(
        _req(
            "POST",
            "/v1/chat",
            token=buyer,
            body={"message": "follow spaceships", "agent": "explorer"},
        )
    )
    assert "No listing or request carries" in unknown.body["reply"]

    searched = app.handle(
        _req(
            "POST",
            "/v1/chat",
            token=buyer,
            body={"message": "cheapest quiet kettle", "agent": "explorer"},
        )
    )
    assert searched.body["block"]["kind"] == "products"
    assert searched.body["block"]["mode"] == "value"  # the words weighed
    assert searched.body["block"]["expires_at"]
    assert f"{DECISION_TTL_HOURS} hours" in searched.body["reply"]
    decided = app.handle(
        _req(
            "POST",
            "/v1/chat",
            token=buyer,
            body={"message": "decide", "agent": "explorer"},
        )
    )
    assert "Marked decided" in decided.body["reply"]
    # The decided lens becomes the next default — preference data, not
    # a menu.
    silent = app.handle(
        _req(
            "POST",
            "/v1/chat",
            token=buyer,
            body={"message": "quiet kettle again", "agent": "explorer"},
        )
    )
    assert silent.body["block"]["mode"] == "value"
    # Words the shelf cannot answer stay a conversation (the card).
    chat = app.handle(
        _req(
            "POST",
            "/v1/chat",
            token=buyer,
            body={"message": "zzz qqq zzz", "agent": "explorer"},
        )
    )
    assert chat.body["source"] == "card"
    conn.close()
