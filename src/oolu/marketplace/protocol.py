"""The agent-to-agent commercial protocol (M4): typed offers on the wire.

Two marketplaces speak commerce to each other in exactly one vocabulary —
the typed records the spine already trusts. An external offer arrives as an
:class:`~.models.Offer` whose ``signature`` binds every material field
under the announcing peer's shared secret; verification is arithmetic, and
a tampered or unsigned offer is refused before any intent exists. There is
deliberately no "protocol negotiation" beyond the version tag: the wire
contract IS the domain contract, so everything downstream — the ladder, the
digest, the approvals, the ledger — applies to a federated offer unchanged.

Pure functions only; peers, transports, and storage live in
``federation.py``.
"""

from __future__ import annotations

import hashlib
import hmac
import json

from .models import Offer

PROTOCOL_VERSION = "a2a-v1"


def offer_material(offer: Offer) -> dict:
    """Every material field of the offer — the signature binds exactly
    what the digest will later hash, plus identity and expiry."""
    material = offer.model_dump(exclude={"signature"})
    if material.get("expires_at") is not None:
        material["expires_at"] = offer.expires_at.isoformat()  # type: ignore[union-attr]
    material["milestones"] = [list(m) for m in offer.milestones]
    return material


def _mac(material: dict, *, secret: str) -> str:
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hmac.new(
        secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def sign_offer(offer: Offer, *, peer_id: str, secret: str) -> Offer:
    """The announcing peer's stamp: version, identity, and the MAC over
    every material field."""
    mac = _mac(offer_material(offer), secret=secret)
    return offer.model_copy(
        update={"signature": f"{PROTOCOL_VERSION}:{peer_id}:{mac}"}
    )


def verify_offer(offer: Offer, *, peer_id: str, secret: str) -> bool:
    """True iff the signature is this peer's MAC over these exact terms."""
    parts = offer.signature.split(":", 2)
    if len(parts) != 3:
        return False
    version, signer, mac = parts
    if version != PROTOCOL_VERSION or signer != peer_id:
        return False
    expected = _mac(offer_material(offer), secret=secret)
    return hmac.compare_digest(mac, expected)
