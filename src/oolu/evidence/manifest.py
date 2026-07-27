"""Raw evidence manifests — content-hashed chunks under a Merkle root.

The manifest is the immutable record of *what was captured*: which
channels, when, how many samples (and how many dropped — losses are
declared, not hidden), each chunk's content hash, and the Merkle root
binding them all. The manifest's canonical bytes are what a device
signs, reusing the R0 canonicalization so the two planes can never
disagree about byte production.

Merkle hashing is domain-separated at both levels (leaf vs node), which
closes the classic second-preimage confusion between a chunk hash and
an interior node.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from ..actuation.digests import canonical_json, sha256_hex

CHUNK_DOMAIN = b"oolu-evidence-chunk/v1\n"
MERKLE_LEAF_DOMAIN = b"oolu-evidence-leaf/v1\n"
MERKLE_NODE_DOMAIN = b"oolu-evidence-node/v1\n"
MANIFEST_DOMAIN = b"oolu-evidence-manifest/v1\n"


def chunk_hash(payload: bytes) -> str:
    return sha256_hex(CHUNK_DOMAIN + payload)


def merkle_root(content_hashes: Sequence[str]) -> str:
    """Root over chunk content hashes; odd nodes promote unpaired."""

    if not content_hashes:
        raise ValueError("a manifest with no chunks has no Merkle root")
    level = [
        sha256_hex(MERKLE_LEAF_DOMAIN + value.encode("ascii"))
        for value in content_hashes
    ]
    while len(level) > 1:
        paired: list[str] = []
        for index in range(0, len(level) - 1, 2):
            combined = (level[index] + level[index + 1]).encode("ascii")
            paired.append(sha256_hex(MERKLE_NODE_DOMAIN + combined))
        if len(level) % 2:
            paired.append(level[-1])
        level = paired
    return level[0]


class RawChunk(BaseModel):
    """One immutable segment of a captured stream."""

    model_config = ConfigDict(frozen=True)

    chunk_id: str
    channel_ids: tuple[str, ...]
    start_time: str
    end_time: str
    sample_count: int
    dropped_sample_count: int
    content_hash: str


class RawEvidenceManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    evidence_bundle_id: str
    test_job_id: str
    authorization_digest: str
    machine_instance_id: str
    sensor_instance_ids: tuple[str, ...]
    specimen_instance_ids: tuple[str, ...]
    method_id: str
    method_version: str
    started_at: str
    completed_at: str
    clock_source: str
    firmware_digest: str
    chunks: tuple[RawChunk, ...]
    merkle_root: str

    def compute_merkle_root(self) -> str:
        return merkle_root([chunk.content_hash for chunk in self.chunks])

    def signing_bytes(self) -> bytes:
        """The canonical bytes a device signs — the whole manifest."""

        return MANIFEST_DOMAIN + canonical_json(self.model_dump(mode="json"))
