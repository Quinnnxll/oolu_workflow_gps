"""The live peer wire (M4 closer): announcements out, fetches in.

Pairing is a handshake made of three agreed strings: each side registers
the other under an identity, and both hold one shared secret. Our
announcements door signs our public shelf AS our identity WITH the secret
we hold for the asking peer; their fetch verifies each offer against the
identity they registered us under and the same secret. Symmetric, boring,
and entirely inside the A2A contract — the wire carries nothing the import
door wouldn't refuse on its own.

The transport is a port, as always: :class:`HttpPeerTransport` rides the
providers' HTTP seam in production; tests drive two real gateways
in-process and let them trade signed offers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import ValidationError

from .errors import ProtocolViolation, WrongState
from .federation import FederationDesk, ImportedOffer, PeerMarket
from .models import Offer
from .protocol import PROTOCOL_VERSION


@runtime_checkable
class PeerTransport(Protocol):
    def fetch(
        self, peer: PeerMarket, *, self_identity: str, category: str = ""
    ) -> dict: ...


class HttpPeerTransport:
    """The production wire: GET the peer's announcements door."""

    def __init__(self, transport) -> None:
        # providers.HttpTransport-shaped: .request(method, url, ...) with
        # .status / .json on the response.
        self._transport = transport

    def fetch(
        self, peer: PeerMarket, *, self_identity: str, category: str = ""
    ) -> dict:
        url = (
            f"{peer.base_url.rstrip('/')}/v1/commerce/announcements"
            f"?peer_id={self_identity}"
        )
        if category:
            url += f"&category={category}"
        response = self._transport.request("GET", url)
        if response.status != 200:
            raise WrongState(
                f"peer '{peer.peer_id}' answered {response.status} to the "
                "announcements fetch"
            )
        return response.json


def fetch_from_peer(
    desk: FederationDesk,
    peer_id: str,
    *,
    transport: PeerTransport,
    self_identity: str,
    now: datetime,
    category: str = "",
) -> tuple[list[ImportedOffer], int]:
    """Pull a peer's announcements through the import door, one by one.

    Every item walks ``import_offer`` — signature, jurisdiction gate,
    suspension check — so the wire can add nothing the door would refuse.
    Malformed or refused items are counted and audited, never imported;
    one bad announcement does not poison the batch.
    """
    peer = desk.get_peer(peer_id)
    if peer is None:
        raise WrongState("no such peer market")
    payload = transport.fetch(peer, self_identity=self_identity, category=category)
    if payload.get("protocol") != PROTOCOL_VERSION:
        raise ProtocolViolation(
            f"peer '{peer_id}' speaks '{payload.get('protocol')}', "
            f"not {PROTOCOL_VERSION}"
        )
    imported: list[ImportedOffer] = []
    rejected = 0
    for item in payload.get("items") or []:
        try:
            offer = Offer.model_validate(_as_dict(item.get("offer")))
            attributes = {
                str(k): str(v) for k, v in _as_dict(item.get("attributes")).items()
            }
            record = desk.import_offer(
                peer_id,
                offer,
                attributes=attributes,
                seller_attested=bool(item.get("seller_attested", False)),
                now=now,
            )
            imported.append(record)
        except (ValidationError, ProtocolViolation, TypeError):
            # import_offer audited signature refusals already; malformed
            # items are simply not offers.
            rejected += 1
    return imported, rejected


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}
