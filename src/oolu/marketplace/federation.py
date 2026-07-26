"""Cross-marketplace sourcing (M4): other markets' offers, our law.

The federation desk is a door, not a bypass. A peer market is registered
by an operator, carries its jurisdiction, and can be suspended in one
move; an offer imported from it must verify against the peer's shared
secret (the A2A protocol) and land inside a jurisdiction this host has a
configured legal/tax module for — the compliance deployment gate crosses
the federation boundary. What survives import is an ordinary typed offer:
buying it walks the spine's intent door, the ladder judges it, the digest
binds it, and the settlement posts on OUR ledger with our take. The open
market changes where offers come from; it changes nothing about what may
happen to them.

Peer secrets are injected at composition (a vault concern) — the durable
records hold metadata only, never a credential.

Sourcing is the sweep: one specification against the local shelf and
every imported offer, normalized into one eligibility-marked comparison —
the RFQ's discipline, widened to the federation.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from ..billing.tax import TaxRegistry, UnconfiguredJurisdiction
from .catalog import CatalogService
from .errors import ListingUnavailable, MarketNotFound, ProtocolViolation, WrongState
from .models import Offer
from .protocol import verify_offer
from .rfq import RfqSpecification, specification_gaps

_PEERS_SCHEMA = """CREATE TABLE IF NOT EXISTS market_peers (
    peer_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL
)"""

_IMPORTS_SCHEMA = """CREATE TABLE IF NOT EXISTS market_imported_offers (
    offer_id TEXT NOT NULL,
    peer_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (offer_id, peer_id)
)"""


class PeerMarket(BaseModel):
    model_config = ConfigDict(frozen=True)

    peer_id: str
    name: str
    base_url: str = ""
    jurisdiction: str
    state: str = "active"  # active | suspended
    registered_at: datetime


class ImportedOffer(BaseModel):
    model_config = ConfigDict(frozen=True)

    peer_id: str
    offer: Offer
    # What the peer asserts about the item — the sourcing sweep's
    # eligibility inputs.
    attributes: tuple[tuple[str, str], ...] = ()
    # Whether the peer attests its seller's verified identity; feeds the
    # ladder's seller_identity_verified fact ONLY for active trusted peers.
    seller_attested: bool = False
    imported_at: datetime


class SourcedOffer(BaseModel):
    """One row of the federation-wide comparison."""

    model_config = ConfigDict(frozen=True)

    origin: str  # "local:<listing_id>" | "peer:<peer_id>"
    offer: Offer
    eligible: bool
    gaps: tuple[str, ...] = ()
    seller_attested: bool = False


class FederationDesk:
    def __init__(
        self,
        conn,
        *,
        audit,
        tax: TaxRegistry | None = None,
        secrets: Mapping[str, str] | None = None,
    ) -> None:
        self._conn = conn
        self._audit = audit
        self._tax = tax
        self._secrets = dict(secrets or {})
        with self._conn.transaction() as db:
            db.execute(_PEERS_SCHEMA)
            db.execute(_IMPORTS_SCHEMA)

    # ------------------------------------------------------------------ #
    # Peers.                                                              #
    # ------------------------------------------------------------------ #
    def register_peer(
        self,
        *,
        peer_id: str,
        name: str,
        jurisdiction: str,
        base_url: str = "",
        now: datetime,
    ) -> PeerMarket:
        record = PeerMarket(
            peer_id=peer_id,
            name=name,
            base_url=base_url,
            jurisdiction=jurisdiction,
            registered_at=now,
        )
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO market_peers (peer_id, state, payload_json)
                   VALUES (?, ?, ?)
                   ON CONFLICT(peer_id) DO UPDATE SET
                     state = excluded.state,
                     payload_json = excluded.payload_json""",
                (peer_id, record.state, record.model_dump_json()),
            )
        self._audit.append(
            "market.federation.peer_registered",
            {"peer_id": peer_id, "name": name, "jurisdiction": jurisdiction},
        )
        return record

    def get_peer(self, peer_id: str) -> PeerMarket | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json FROM market_peers WHERE peer_id = ?",
                (peer_id,),
            ).fetchone()
        return PeerMarket.model_validate_json(row["payload_json"]) if row else None

    def peers(self) -> list[PeerMarket]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT payload_json FROM market_peers"
            ).fetchall()
        records = [
            PeerMarket.model_validate_json(row["payload_json"]) for row in rows
        ]
        records.sort(key=lambda r: (r.registered_at.isoformat(), r.peer_id))
        return records

    def set_peer_state(self, peer_id: str, *, state: str) -> PeerMarket:
        peer = self.get_peer(peer_id)
        if peer is None:
            raise MarketNotFound("no such peer market")
        updated = peer.model_copy(update={"state": state})
        with self._conn.transaction() as db:
            db.execute(
                "UPDATE market_peers SET state = ?, payload_json = ?"
                " WHERE peer_id = ?",
                (state, updated.model_dump_json(), peer_id),
            )
        self._audit.append(
            "market.federation.peer_state",
            {"peer_id": peer_id, "state": state},
        )
        return updated

    # ------------------------------------------------------------------ #
    # Imports: the wire contract, the jurisdiction gate, our shelf.       #
    # ------------------------------------------------------------------ #
    def import_offer(
        self,
        peer_id: str,
        offer: Offer,
        *,
        attributes: dict[str, str] | None = None,
        seller_attested: bool = False,
        now: datetime,
    ) -> ImportedOffer:
        peer = self.get_peer(peer_id)
        if peer is None:
            raise MarketNotFound("no such peer market")
        if peer.state != "active":
            raise WrongState(f"the peer market is {peer.state}")
        if self._tax is not None:
            try:
                self._tax.get(peer.jurisdiction)
            except UnconfiguredJurisdiction as exc:
                raise ListingUnavailable(str(exc)) from exc
        secret = self._secrets.get(peer_id)
        if not secret:
            raise ProtocolViolation(
                f"no shared secret is configured for peer '{peer_id}'; "
                "nothing it announces can be trusted"
            )
        if not verify_offer(offer, peer_id=peer_id, secret=secret):
            self._audit.append(
                "market.federation.offer_rejected",
                {
                    "peer_id": peer_id,
                    "offer_id": offer.offer_id,
                    "reason": "signature verification failed",
                },
            )
            raise ProtocolViolation(
                "the offer's signature does not cover these terms — "
                "tampered, unsigned, or not this peer's"
            )
        record = ImportedOffer(
            peer_id=peer_id,
            offer=offer,
            attributes=tuple(sorted((attributes or {}).items())),
            seller_attested=seller_attested and peer.state == "active",
            imported_at=now,
        )
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO market_imported_offers
                   (offer_id, peer_id, payload_json) VALUES (?, ?, ?)
                   ON CONFLICT(offer_id, peer_id) DO UPDATE SET
                     payload_json = excluded.payload_json""",
                (offer.offer_id, peer_id, record.model_dump_json()),
            )
        self._audit.append(
            "market.federation.offer_imported",
            {
                "peer_id": peer_id,
                "offer_id": offer.offer_id,
                "offer_version": offer.offer_version,
                "total_micros": offer.total_micros,
                "seller_attested": record.seller_attested,
            },
        )
        return record

    def imported(self) -> list[ImportedOffer]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT payload_json FROM market_imported_offers"
            ).fetchall()
        records = [
            ImportedOffer.model_validate_json(row["payload_json"]) for row in rows
        ]
        records.sort(
            key=lambda r: (r.imported_at.isoformat(), r.offer.offer_id)
        )
        return records

    # ------------------------------------------------------------------ #
    # Sourcing: one specification, every shelf, one honest comparison.    #
    # ------------------------------------------------------------------ #
    def source(
        self,
        specification: RfqSpecification,
        *,
        catalog: CatalogService | None = None,
        now: datetime,
    ) -> list[SourcedOffer]:
        rows: list[SourcedOffer] = []
        if catalog is not None:
            for listing in catalog.store.active():
                if (
                    specification.category
                    and listing.category != specification.category
                ):
                    continue
                try:
                    offer = catalog.offer_for(
                        listing.listing_id,
                        quantity=specification.quantity,
                        now=now,
                    )
                except ListingUnavailable as exc:
                    rows.append(
                        SourcedOffer(
                            origin=f"local:{listing.listing_id}",
                            offer=_unavailable_offer(listing),
                            eligible=False,
                            gaps=(str(exc),),
                        )
                    )
                    continue
                # A local listing attests no attributes either: it is judged
                # by the same bar as every quote, never waved through.
                gaps = specification_gaps(
                    specification, attributes={}, offer=offer
                )
                rows.append(
                    SourcedOffer(
                        origin=f"local:{listing.listing_id}",
                        offer=offer,
                        eligible=not gaps,
                        gaps=gaps,
                        seller_attested=True,  # our own KYC gate published it
                    )
                )
        for record in self.imported():
            peer = self.get_peer(record.peer_id)
            if peer is None or peer.state != "active":
                continue
            gaps = specification_gaps(
                specification,
                attributes=dict(record.attributes),
                offer=record.offer,
            )
            rows.append(
                SourcedOffer(
                    origin=f"peer:{record.peer_id}",
                    offer=record.offer,
                    eligible=not gaps,
                    gaps=gaps,
                    seller_attested=record.seller_attested,
                )
            )
        rows.sort(
            key=lambda r: (not r.eligible, r.offer.total_micros, r.origin)
        )
        return rows


def _unavailable_offer(listing) -> Offer:
    """A placeholder row for a shelf that cannot serve at all."""
    return Offer(
        offer_id=f"{listing.listing_id}:unavailable",
        seller_id=listing.seller_id,
        offer_version=listing.version,
        item_id=listing.listing_id,
        quantity=1,
        subtotal_micros=listing.unit_price_micros,
        currency=listing.currency,
    )
