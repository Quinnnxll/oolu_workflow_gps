"""S3/R2 blob storage — the ArtifactStore port, production-shaped.

Exit gates: the S3 store keeps the filesystem store's exact contract —
content-addressed ``sha256:`` refs, idempotent dedup on put, honest
existence, byte-identical get, prune by age — against a scripted
S3-shaped client (put/get/head/delete/list, R2's wire vocabulary); and
``blob_store_from_env`` selects R2 when the bucket is named, the local
filesystem otherwise, so a hosted install moves its bytes with four
environment variables and no code.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from oolu.assembly import blob_store_from_env
from oolu.durable.artifacts import FilesystemArtifactStore
from oolu.durable.artifacts_s3 import S3ArtifactStore


class FakeS3:
    """An in-memory bucket speaking the client verbs the store uses."""

    def __init__(self):
        self.objects: dict[str, dict] = {}
        self.puts = 0

    def put_object(self, *, Bucket, Key, Body, ContentType, Metadata):
        self.puts += 1
        self.objects[Key] = {
            "body": bytes(Body),
            "content_type": ContentType,
            "metadata": dict(Metadata),
            "modified": datetime.now(UTC),
        }

    def head_object(self, *, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)  # boto raises ClientError; any raise = 404
        return {"ContentLength": len(self.objects[Key]["body"])}

    def get_object(self, *, Bucket, Key):
        import io

        return {"Body": io.BytesIO(self.objects[Key]["body"])}

    def delete_object(self, *, Bucket, Key):
        self.objects.pop(Key, None)

    def list_objects_v2(self, *, Bucket, Prefix=None, ContinuationToken=None):
        contents = [
            {"Key": key, "LastModified": entry["modified"]}
            for key, entry in sorted(self.objects.items())
            if Prefix is None or key.startswith(Prefix)
        ]
        return {"Contents": contents, "IsTruncated": False}


def test_the_s3_store_keeps_the_ports_exact_contract():
    fake = FakeS3()
    store = S3ArtifactStore(bucket="blobs", client=fake, prefix="oolu")
    payload = b"step file bytes " * 100

    ref = store.put("cad/bracket.step", payload, media_type="application/step")
    assert ref.startswith("sha256:")
    # Content addressing: identical bytes deduplicate — one upload only.
    assert store.put("cad/again.step", payload, media_type="application/step") == ref
    assert fake.puts == 1
    # The key sharded under the prefix, like the filesystem layout.
    (key,) = fake.objects
    digest = ref.split(":", 1)[1]
    assert key == f"oolu/{digest[:2]}/{digest}"

    assert store.exists(ref)
    assert store.get(ref) == payload
    assert store.delete(ref) is True
    assert store.delete(ref) is False  # honest about what was there
    assert not store.exists(ref)

    with pytest.raises(ValueError):
        store.get("md5:nope")


def test_prune_removes_only_the_old():
    fake = FakeS3()
    store = S3ArtifactStore(bucket="blobs", client=fake)
    old_ref = store.put("old", b"old bytes", media_type="text/plain")
    fresh_ref = store.put("new", b"new bytes", media_type="text/plain")
    old_key = store._key(old_ref.split(":", 1)[1])
    fake.objects[old_key]["modified"] = datetime.now(UTC) - timedelta(days=30)

    assert store.prune(older_than_seconds=7 * 86400) == 1
    assert not store.exists(old_ref)
    assert store.exists(fresh_ref)


def test_the_environment_selects_the_blob_home(tmp_path):
    # No bucket named: the local filesystem, exactly as before.
    local = blob_store_from_env(tmp_path, {})
    assert isinstance(local, FilesystemArtifactStore)
    ref = local.put("a.txt", b"bytes", media_type="text/plain")
    assert (tmp_path / "file-blobs").exists()
    assert local.get(ref) == b"bytes"

    # Bucket named: R2/S3 — the boto3 import is the only thing between
    # the env and the store, and it answers in words when missing.
    env = {
        "OOLU_BLOB_S3_BUCKET": "oolu-blobs",
        "OOLU_BLOB_S3_ENDPOINT": "https://acc.r2.cloudflarestorage.com",
        "OOLU_BLOB_S3_ACCESS_KEY_ID": "k",
        "OOLU_BLOB_S3_SECRET_ACCESS_KEY": "s",
    }
    try:
        selected = blob_store_from_env(tmp_path, env)
    except ImportError as exc:
        assert "boto3" in str(exc) and "s3" in str(exc)
    else:
        assert isinstance(selected, S3ArtifactStore)
