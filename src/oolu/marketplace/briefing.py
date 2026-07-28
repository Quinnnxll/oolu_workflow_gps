"""The market desk's briefing — where the member's position meets demand.

The Market agent does not wait to be asked: like the News edition, the
desk brief arrives as the agent's OWN thread message on the member's
standing pulse schedule, and the same items head the Market thread's
panel. Everything here is a deterministic read of standing records —
nothing invents demand, prices, or interest:

- **Approvals** waiting on the member's decision (the inbox they hold).
- **Orders** where the member acts next: a seller ships what is
  confirmed; a buyer accepts what is delivered.
- **Demand matching their position**: an OPEN request by someone else
  whose category matches one of the member's ACTIVE listings — they
  are positioned to quote.
- **Their own requests ripening**: an open request of theirs holding
  quotes — comparison and award wait on them.
- **Supply for their requests**: someone else's active listing whose
  category matches their open request.

The desk speaks counts and names, never invented urgency; an empty
brief is silence (no message fires), the reminder discipline.
"""

from __future__ import annotations

from dataclasses import dataclass

# The pulse sentinel: a schedule with this goal fires the member's desk
# brief instead of a workflow run (the EDITION_PULSE_GOAL pattern).
MARKET_PULSE_GOAL = "market.desk_brief"
MARKET_BRIEF_LABEL = "Market desk brief"

# The desk's own words are bounded — a brief, not a ledger dump.
MAX_ITEMS_PER_KIND = 5


@dataclass(frozen=True)
class DeskItem:
    """One thing the desk sees for the member: what it is, the record
    it points at, and the desk's own sentence."""

    kind: str  # approval | order | quote_wanted | award_wanted | supply
    ref: str  # the id the client can act on
    text: str


def desk_briefing(
    *,
    principal: str,
    approvals: list[dict],
    orders: list,  # orders.StoredOrder
    open_rfqs: list,  # rfq.RequestForQuote (every live request, tenant-wide)
    my_listings: list,  # catalog.Listing (the member's own, any status)
    active_listings: list,  # catalog.Listing (the public shelf)
    quote_counts: dict[str, int],  # rfq_id -> quotes on the book
) -> list[DeskItem]:
    """The member's brief, in a stable order: what waits on them first,
    then where their position meets the market's demand."""
    items: list[DeskItem] = []

    for summary in approvals[:MAX_ITEMS_PER_KIND]:
        ref = str(summary.get("intent_id") or "")
        items.append(
            DeskItem(
                kind="approval",
                ref=ref,
                text="An approval waits on your decision.",
            )
        )

    ship = [
        o
        for o in orders
        if o.state == "confirmed" and o.record.seller_id == principal
    ]
    accept = [
        o
        for o in orders
        if o.state == "delivered" and o.record.buyer_principal == principal
    ]
    for order in ship[:MAX_ITEMS_PER_KIND]:
        items.append(
            DeskItem(
                kind="order",
                ref=order.record.order_id,
                text="An order you sold is confirmed — it waits on your shipment.",
            )
        )
    for order in accept[:MAX_ITEMS_PER_KIND]:
        items.append(
            DeskItem(
                kind="order",
                ref=order.record.order_id,
                text="An order was delivered to you — accepting releases the money.",
            )
        )

    # Their own requests ripening: quotes on the book, award waiting.
    mine = [r for r in open_rfqs if r.buyer_principal == principal]
    for rfq in mine[:MAX_ITEMS_PER_KIND]:
        count = quote_counts.get(rfq.rfq_id, 0)
        if count > 0:
            items.append(
                DeskItem(
                    kind="award_wanted",
                    ref=rfq.rfq_id,
                    text=(
                        f"Your request for {rfq.specification.category} holds "
                        f"{count} quote{'s' if count != 1 else ''} — compare "
                        "and award when ready."
                    ),
                )
            )

    # Demand matching their position: they sell in this category; someone
    # is asking for it right now.
    my_categories = {
        listing.category
        for listing in my_listings
        if listing.status == "active" and listing.category
    }
    theirs = [r for r in open_rfqs if r.buyer_principal != principal]
    matched = [
        r for r in theirs if r.specification.category in my_categories
    ]
    for rfq in matched[:MAX_ITEMS_PER_KIND]:
        items.append(
            DeskItem(
                kind="quote_wanted",
                ref=rfq.rfq_id,
                text=(
                    f"A request for {rfq.specification.category} matches "
                    "what you sell — you are positioned to quote."
                ),
            )
        )

    # Supply for their requests: the shelf now carries their category.
    wanted = {
        r.specification.category for r in mine if r.specification.category
    }
    supply = [
        listing
        for listing in active_listings
        if listing.category in wanted and listing.seller_principal != principal
    ]
    for listing in supply[:MAX_ITEMS_PER_KIND]:
        items.append(
            DeskItem(
                kind="supply",
                ref=listing.listing_id,
                text=(
                    f"Supply arrived for your request: “{listing.title}” "
                    f"({listing.category}) is on the shelf."
                ),
            )
        )
    return items


def briefing_message(items: list[DeskItem], *, skipped: int = 0) -> str:
    """The Market thread's own words — counts and names, or honest
    silence handled by the caller (no items = no message fires)."""
    lines = ["Market desk brief — the market sees you:"]
    for i, item in enumerate(items, start=1):
        lines.append(f"{i}. {item.text}")
    if skipped:
        lines.append(
            f"(Catching up: {skipped} earlier brief"
            f"{'s were' if skipped != 1 else ' was'} missed while this "
            "host was asleep.)"
        )
    return "\n".join(lines)
