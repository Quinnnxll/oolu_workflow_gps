"""The fixed-price catalog (M1): listings by verified sellers, offers minted
from them.

A listing is the seller's standing terms; an :class:`~.models.Offer` is the
exact, versioned instance of those terms a buyer's intent binds to. The
listing's ``version`` bumps on every term change and rides into the offer's
``offer_version`` — so the digest law reaches all the way back to the shelf:
change the price and every approval bound to the old terms dies on its own.

Publication is gated on seller verification (an MVP boundary: unverified
sellers cannot list). The check is a port — the gateway wires it to the
standing KYC seam; tests inject their own — and the catalog refuses rather
than trusts when no verifier answers.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .errors import ListingUnavailable, MarketNotFound, SellerUnverified
from .models import Offer

_SCHEMA = """CREATE TABLE IF NOT EXISTS market_listings (
    listing_id TEXT PRIMARY KEY,
    tenant TEXT NOT NULL,
    seller TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL
)"""


class Listing(BaseModel):
    """One seller's standing fixed-price terms, versioned."""

    model_config = ConfigDict(frozen=True)

    listing_id: str
    tenant_id: str
    seller_principal: str
    # The commercial identity a buyer's offer names; defaults to the
    # seller principal.
    seller_id: str
    title: str
    category: str = ""
    description: str = ""
    unit_price_micros: int = Field(ge=0)
    # The seller's stated regular price, when one exists — the ONLY thing
    # a displayed discount may derive from (agents-expansion A6: a "was"
    # price is a fact or it is nothing; policy ceilings never render).
    list_price_micros: int | None = Field(default=None, ge=0)
    currency: str = "USD"
    quantity_available: int = Field(default=0, ge=0)
    refund_terms: str = "30-day returns"
    fulfillment_terms: str = "standard shipping"
    refundable: bool = True
    status: str = "draft"  # draft | active | suspended
    version: int = Field(default=1, ge=1)
    created_at: datetime
    updated_at: datetime


class CatalogStore:
    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def save(self, listing: Listing) -> Listing:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO market_listings
                   (listing_id, tenant, seller, status, payload_json)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(listing_id) DO UPDATE SET
                     status = excluded.status,
                     payload_json = excluded.payload_json""",
                (
                    listing.listing_id,
                    listing.tenant_id,
                    listing.seller_principal,
                    listing.status,
                    listing.model_dump_json(),
                ),
            )
        return listing

    def get(self, listing_id: str) -> Listing | None:
        """Unscoped read: an active listing is the marketplace's public
        shelf. Management paths re-check ownership themselves."""
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json FROM market_listings WHERE listing_id = ?",
                (listing_id,),
            ).fetchone()
        return Listing.model_validate_json(row["payload_json"]) if row else None

    def active(self) -> list[Listing]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT payload_json FROM market_listings WHERE status = 'active'"
            ).fetchall()
        listings = [Listing.model_validate_json(r["payload_json"]) for r in rows]
        listings.sort(key=lambda r: (r.created_at.isoformat(), r.listing_id))
        return listings

    def for_seller(self, *, tenant: str, seller: str) -> list[Listing]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT payload_json FROM market_listings"
                " WHERE tenant = ? AND seller = ?",
                (tenant, seller),
            ).fetchall()
        listings = [Listing.model_validate_json(r["payload_json"]) for r in rows]
        listings.sort(key=lambda r: (r.created_at.isoformat(), r.listing_id))
        return listings


class CatalogService:
    """Drafting, publication (KYC-gated), and offer minting."""

    def __init__(
        self,
        conn,
        *,
        audit,
        seller_verified: Callable[[str, str], bool] | None = None,
        inventory=None,  # marketplace.InventoryService: publication seeds it
        tax=None,  # billing.tax.TaxRegistry: offers carry real estimates
        jurisdiction: str = "",  # the host's operating jurisdiction code
    ) -> None:
        self.store = CatalogStore(conn)
        self._audit = audit
        # No verifier wired = nobody publishes. Refusal is the default.
        self._seller_verified = seller_verified or (lambda tenant, seller: False)
        self._inventory = inventory
        self._tax = tax
        self._jurisdiction = jurisdiction

    def create_draft(
        self,
        *,
        tenant: str,
        seller_principal: str,
        title: str,
        unit_price_micros: int,
        now: datetime,
        seller_id: str | None = None,
        category: str = "",
        description: str = "",
        currency: str = "USD",
        quantity_available: int = 0,
        refund_terms: str = "30-day returns",
        fulfillment_terms: str = "standard shipping",
        refundable: bool = True,
        list_price_micros: int | None = None,
    ) -> Listing:
        listing = Listing(
            listing_id=uuid4().hex,
            tenant_id=tenant,
            seller_principal=seller_principal,
            seller_id=seller_id or seller_principal,
            title=title,
            category=category,
            description=description,
            unit_price_micros=unit_price_micros,
            list_price_micros=list_price_micros,
            currency=currency,
            quantity_available=quantity_available,
            refund_terms=refund_terms,
            fulfillment_terms=fulfillment_terms,
            refundable=refundable,
            created_at=now,
            updated_at=now,
        )
        self.store.save(listing)
        self._audit.append(
            "market.listing.drafted",
            {
                "tenant": tenant,
                "seller": seller_principal,
                "listing_id": listing.listing_id,
                "title": title,
                "unit_price_micros": unit_price_micros,
            },
        )
        return listing

    def _own(self, listing_id: str, *, tenant: str, seller: str) -> Listing:
        listing = self.store.get(listing_id)
        if (
            listing is None
            or listing.tenant_id != tenant
            or listing.seller_principal != seller
        ):
            raise MarketNotFound("no such listing")
        return listing

    def publish(
        self, listing_id: str, *, tenant: str, seller: str, now: datetime
    ) -> Listing:
        listing = self._own(listing_id, tenant=tenant, seller=seller)
        if not self._seller_verified(tenant, seller):
            raise SellerUnverified(
                "publishing requires a verified seller identity"
            )
        published = listing.model_copy(
            update={"status": "active", "updated_at": now}
        )
        self.store.save(published)
        if self._inventory is not None:
            # Publication puts the stated stock on the tracked shelf; from
            # here on, reservations — not the listing row — are the truth.
            self._inventory.seed(listing_id, published.quantity_available)
        self._audit.append(
            "market.listing.published",
            {
                "tenant": tenant,
                "seller": seller,
                "listing_id": listing_id,
                "version": published.version,
            },
        )
        return published

    def suspend(
        self, listing_id: str, *, tenant: str, seller: str, now: datetime
    ) -> Listing:
        listing = self._own(listing_id, tenant=tenant, seller=seller)
        suspended = listing.model_copy(
            update={"status": "suspended", "updated_at": now}
        )
        self.store.save(suspended)
        self._audit.append(
            "market.listing.suspended",
            {"tenant": tenant, "seller": seller, "listing_id": listing_id},
        )
        return suspended

    def update_terms(
        self,
        listing_id: str,
        *,
        tenant: str,
        seller: str,
        now: datetime,
        **changes,
    ) -> Listing:
        """Change terms; the version bumps, so old offers (and any approval
        bound to them) die by the digest law, not by cleanup."""
        listing = self._own(listing_id, tenant=tenant, seller=seller)
        allowed = {
            "title",
            "category",
            "description",
            "unit_price_micros",
            "currency",
            "quantity_available",
            "refund_terms",
            "fulfillment_terms",
            "refundable",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unknown listing terms: {sorted(unknown)}")
        updated = listing.model_copy(
            update={**changes, "version": listing.version + 1, "updated_at": now}
        )
        self.store.save(updated)
        self._audit.append(
            "market.listing.terms_changed",
            {
                "tenant": tenant,
                "seller": seller,
                "listing_id": listing_id,
                "version": updated.version,
                "changed": sorted(changes),
            },
        )
        return updated

    def offer_for(self, listing_id: str, *, quantity: int, now: datetime) -> Offer:
        """Mint the exact versioned offer a buyer's intent binds to.

        Availability is read from LIVE stock when the listing is
        inventory-tracked (ripe holds swept first), and the tax estimate
        comes from the configured jurisdiction module — a host whose
        jurisdiction has no module refuses to transact at all, here,
        before any intent exists (the compliance deployment rule).
        """
        listing = self.store.get(listing_id)
        if listing is None:
            raise MarketNotFound("no such listing")
        if listing.status != "active":
            raise ListingUnavailable("the listing is not active")
        available = listing.quantity_available
        if self._inventory is not None and self._inventory.tracked(listing_id):
            live = self._inventory.available(listing_id, now=now)
            available = live if live is not None else available
        if quantity < 1 or quantity > available:
            raise ListingUnavailable(
                f"the listing cannot cover quantity {quantity}"
            )
        subtotal = listing.unit_price_micros * quantity
        tax_estimate = 0
        if self._tax is not None:
            try:
                tax_estimate = self._tax.tax_micros(self._jurisdiction, subtotal)
            except Exception as exc:
                raise ListingUnavailable(str(exc)) from exc
        return Offer(
            offer_id=f"{listing.listing_id}:v{listing.version}",
            seller_id=listing.seller_id,
            offer_version=listing.version,
            item_id=listing.listing_id,
            quantity=quantity,
            subtotal_micros=subtotal,
            currency=listing.currency,
            tax_estimate_micros=tax_estimate,
            fees_micros=0,
            fulfillment_terms=listing.fulfillment_terms,
            refund_terms=listing.refund_terms,
            refundable=listing.refundable,
        )
