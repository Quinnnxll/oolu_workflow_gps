"""Comparisons — one normalized matrix, like with like (A6).

The RFQ lesson generalized: every candidate becomes one row carrying the
SAME fields, ineligible rows stay in the set with their gaps named
(an absent competitor tells the reader less than a named one), and the
discount is a FACT or it is nothing — computed only from a stated list
price above the current price, never from a policy ceiling.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


def discount_fact(
    unit_price_micros: int, list_price_micros: int | None
) -> int | None:
    """The displayed discount, percent — or None. A 'was' price exists
    only when the seller stated a list price genuinely above the current
    one; anything else renders nothing (no manufactured markdowns)."""
    if list_price_micros is None:
        return None
    if list_price_micros <= 0 or unit_price_micros <= 0:
        return None
    if list_price_micros <= unit_price_micros:
        return None
    return round(
        100 * (list_price_micros - unit_price_micros) / list_price_micros
    )


class ComparisonRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    listing_id: str
    title: str
    seller: str
    price_micros: int
    currency: str
    list_price_micros: int | None
    discount_percent: int | None  # the fact, or nothing
    feedback: dict  # {count, mean, factor} — verified voices only
    trust: dict  # {score, finished, refunded, disputed, basis}
    lab: dict  # {count, mean_score, factor} — member-measured
    eligible: bool
    gaps: tuple[str, ...]  # why not, named — the RFQ discipline


class ComparisonSet(BaseModel):
    model_config = ConfigDict(frozen=True)

    category: str
    rows: tuple[ComparisonRow, ...]


def build_rows(
    listings,  # list[marketplace.catalog.Listing]
    *,
    feedback_for,  # Callable[[listing_id], dict]
    trust_for,  # Callable[[seller_principal], dict]
    lab_for,  # Callable[[listing_id], dict]
) -> list[ComparisonRow]:
    rows: list[ComparisonRow] = []
    for listing in listings:
        gaps: list[str] = []
        if listing.status != "active":
            gaps.append(f"not on sale (status: {listing.status})")
        if listing.quantity_available <= 0:
            gaps.append("out of stock")
        rows.append(
            ComparisonRow(
                listing_id=listing.listing_id,
                title=listing.title,
                seller=listing.seller_principal,
                price_micros=listing.unit_price_micros,
                currency=listing.currency,
                list_price_micros=listing.list_price_micros,
                discount_percent=discount_fact(
                    listing.unit_price_micros, listing.list_price_micros
                ),
                feedback=feedback_for(listing.listing_id),
                trust=trust_for(listing.seller_principal),
                lab=lab_for(listing.listing_id),
                eligible=not gaps,
                gaps=tuple(gaps),
            )
        )
    # Deterministic set order before any scoring touches it.
    rows.sort(key=lambda r: r.listing_id)
    return rows
