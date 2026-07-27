"""Evidence ingress — registered devices in, immutable bundles stored.

The ingress pipeline realizes the Phase 1 exit gates as code: an
unregistered device cannot upload, a revoked credential blocks *new*
uploads without deleting history, a modified chunk fails integrity, and
a resumed upload is idempotent — the same bundle received twice stores
once, while the same bundle id with different content is a named
conflict, never a silent overwrite.

Signing is a seam: ``SignatureVerifier`` is a protocol, and the
reference implementation is stdlib HMAC over the manifest's canonical
bytes — an honest baseline for R1 (per-device shared keys, constant-
time comparison). Device certificates and hardware-backed keys arrive
with the edge work in R4 behind the same seam.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from enum import Enum
from typing import Protocol

from .manifest import RawEvidenceManifest, chunk_hash


class DeviceStatus(str, Enum):
    ACTIVE = "active"
    QUARANTINED = "quarantined"
    REVOKED = "revoked"


class IngressRefusal(str, Enum):
    UNREGISTERED_DEVICE = "unregistered_device"
    CREDENTIAL_REVOKED = "credential_revoked"
    DEVICE_QUARANTINED = "device_quarantined"
    INVALID_SIGNATURE = "invalid_signature"
    MISSING_CHUNK_PAYLOAD = "missing_chunk_payload"
    CHUNK_HASH_MISMATCH = "chunk_hash_mismatch"
    MERKLE_ROOT_MISMATCH = "merkle_root_mismatch"
    CONTENT_CONFLICT = "content_conflict"


class SignatureVerifier(Protocol):
    def verify(self, *, device_id: str, payload: bytes, signature: str) -> bool: ...


class HmacDeviceKeys:
    """Reference signer/verifier: per-device HMAC-SHA256, stdlib only."""

    def __init__(self) -> None:
        self._keys: dict[str, bytes] = {}

    def register_key(self, device_id: str, key: bytes) -> None:
        self._keys[device_id] = key

    def sign(self, *, device_id: str, payload: bytes) -> str:
        key = self._keys.get(device_id)
        if key is None:
            raise KeyError(f"no key registered for device {device_id!r}")
        return hmac.new(key, payload, hashlib.sha256).hexdigest()

    def verify(self, *, device_id: str, payload: bytes, signature: str) -> bool:
        key = self._keys.get(device_id)
        if key is None:
            return False
        expected = hmac.new(key, payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)


class DeviceRegistry:
    """Device identity and credential status. Revocation is a status,
    not a deletion — the device's history remains attributable."""

    def __init__(self) -> None:
        self._devices: dict[str, DeviceStatus] = {}

    def register(self, device_id: str) -> None:
        self._devices[device_id] = DeviceStatus.ACTIVE

    def set_status(self, device_id: str, status: DeviceStatus) -> None:
        if device_id not in self._devices:
            raise KeyError(f"unknown device {device_id!r}")
        self._devices[device_id] = status

    def status(self, device_id: str) -> DeviceStatus | None:
        return self._devices.get(device_id)


class StoredBundle:
    """One accepted bundle: manifest, signature, and receipt time."""

    __slots__ = ("manifest", "device_id", "signature", "received_at")

    def __init__(
        self,
        manifest: RawEvidenceManifest,
        device_id: str,
        signature: str,
        received_at: int,
    ) -> None:
        self.manifest = manifest
        self.device_id = device_id
        self.signature = signature
        self.received_at = received_at


class EvidenceIngress:
    """Verify-then-store. The store has no update and no delete."""

    def __init__(self, devices: DeviceRegistry, verifier: SignatureVerifier) -> None:
        self._devices = devices
        self._verifier = verifier
        self._bundles: dict[str, StoredBundle] = {}

    def get(self, evidence_bundle_id: str) -> StoredBundle | None:
        return self._bundles.get(evidence_bundle_id)

    def receive(
        self,
        manifest: RawEvidenceManifest,
        *,
        device_id: str,
        signature: str,
        chunk_payloads: Mapping[str, bytes],
        now: int,
    ) -> tuple[IngressRefusal, ...]:
        """Accept or refuse one bundle. Empty tuple means stored.

        Refusals accumulate so an operator sees every problem at once;
        nothing is stored unless every check passes.
        """

        refusals: list[IngressRefusal] = []

        status = self._devices.status(device_id)
        if status is None:
            refusals.append(IngressRefusal.UNREGISTERED_DEVICE)
        elif status is DeviceStatus.REVOKED:
            refusals.append(IngressRefusal.CREDENTIAL_REVOKED)
        elif status is DeviceStatus.QUARANTINED:
            refusals.append(IngressRefusal.DEVICE_QUARANTINED)

        if status is DeviceStatus.ACTIVE and not self._verifier.verify(
            device_id=device_id, payload=manifest.signing_bytes(), signature=signature
        ):
            refusals.append(IngressRefusal.INVALID_SIGNATURE)

        for chunk in manifest.chunks:
            payload = chunk_payloads.get(chunk.chunk_id)
            if payload is None:
                refusals.append(IngressRefusal.MISSING_CHUNK_PAYLOAD)
            elif chunk_hash(payload) != chunk.content_hash:
                refusals.append(IngressRefusal.CHUNK_HASH_MISMATCH)

        if manifest.compute_merkle_root() != manifest.merkle_root:
            refusals.append(IngressRefusal.MERKLE_ROOT_MISMATCH)

        existing = self._bundles.get(manifest.evidence_bundle_id)
        if existing is not None:
            if existing.manifest.signing_bytes() != manifest.signing_bytes():
                refusals.append(IngressRefusal.CONTENT_CONFLICT)
            elif not refusals:
                # Idempotent resume: same bundle, same content — already
                # stored, and the original receipt stands.
                return ()

        if refusals:
            return tuple(refusals)

        self._bundles[manifest.evidence_bundle_id] = StoredBundle(
            manifest, device_id, signature, now
        )
        return ()
