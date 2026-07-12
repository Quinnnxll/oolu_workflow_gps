"""S3-compatible object storage — the ArtifactStore port, in the cloud.

The production sibling of :class:`FilesystemArtifactStore`: the same
``sha256:...`` self-verifying references, the same idempotent
content-addressed ``put``, against any S3-compatible bucket —
Cloudflare R2 (region ``auto``), AWS S3, MinIO. The blob layer under
the file drawer and the CAD hand becomes durable object storage with
one set of environment variables; nothing above this port changes.

The client is injectable (tests bring a fake; production brings boto3
via the ``s3`` extra). Keys shard by the digest's first two hex chars,
mirroring the filesystem layout, under an optional prefix so one
bucket can serve several installs.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any


def _boto3_client(
    *, endpoint_url: str, access_key_id: str, secret_access_key: str, region: str
):
    try:
        import boto3  # noqa: PLC0415 - optional heavy dependency
    except ImportError as exc:  # pragma: no cover - import guard
        raise ImportError(
            "S3 blob storage needs boto3 — install the 's3' extra"
        ) from exc
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        region_name=region or "auto",
    )


class S3ArtifactStore:
    def __init__(
        self,
        *,
        bucket: str,
        client: Any = None,
        endpoint_url: str = "",
        access_key_id: str = "",
        secret_access_key: str = "",
        region: str = "auto",
        prefix: str = "",
    ) -> None:
        if client is None:
            client = _boto3_client(
                endpoint_url=endpoint_url,
                access_key_id=access_key_id,
                secret_access_key=secret_access_key,
                region=region,
            )
        self._client = client
        self._bucket = bucket
        self._prefix = prefix.strip("/")

    # ------------------------------------------------------------------ #
    def put(self, artifact_id: str, content: bytes, *, media_type: str) -> str:
        digest = hashlib.sha256(content).hexdigest()
        key = self._key(digest)
        if not self._head(key):  # content-addressed: identical bytes dedupe
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=content,
                ContentType=media_type or "application/octet-stream",
                Metadata={"artifact-id": artifact_id[:512]},
            )
        return f"sha256:{digest}"

    def get(self, artifact_ref: str) -> bytes:
        response = self._client.get_object(
            Bucket=self._bucket, Key=self._key(self._digest(artifact_ref))
        )
        return response["Body"].read()

    def exists(self, artifact_ref: str) -> bool:
        return self._head(self._key(self._digest(artifact_ref)))

    def delete(self, artifact_ref: str) -> bool:
        key = self._key(self._digest(artifact_ref))
        existed = self._head(key)
        if existed:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        return existed

    def prune(self, *, older_than_seconds: float) -> int:
        """Delete blobs last modified before the cutoff — same contract
        as the filesystem store, one page of listings at a time."""
        cutoff = datetime.now(UTC).timestamp() - older_than_seconds
        removed = 0
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self._bucket}
            if self._prefix:
                kwargs["Prefix"] = self._prefix + "/"
            if token:
                kwargs["ContinuationToken"] = token
            page = self._client.list_objects_v2(**kwargs)
            for entry in page.get("Contents") or []:
                modified = entry["LastModified"]
                stamp = (
                    modified.timestamp()
                    if hasattr(modified, "timestamp")
                    else float(modified)
                )
                if stamp < cutoff:
                    self._client.delete_object(
                        Bucket=self._bucket, Key=entry["Key"]
                    )
                    removed += 1
            if not page.get("IsTruncated"):
                return removed
            token = page.get("NextContinuationToken")

    # ------------------------------------------------------------------ #
    def _head(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except Exception:  # noqa: BLE001 - boto raises ClientError on 404;
            # any other failure reads as absent and put/get speak for real.
            return False

    def _key(self, digest: str) -> str:
        sharded = f"{digest[:2]}/{digest}"
        return f"{self._prefix}/{sharded}" if self._prefix else sharded

    @staticmethod
    def _digest(artifact_ref: str) -> str:
        if not artifact_ref.startswith("sha256:"):
            raise ValueError(f"unsupported artifact reference: {artifact_ref}")
        return artifact_ref.split(":", 1)[1]
