"""S3 / MinIO artifact fetcher."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Data model

@dataclass
class S3Artifact:
    """A retrieved S3 object with its raw content and metadata."""

    key: str
    bucket: str
    content: bytes
    content_type: str
    size: int
    url: Optional[str] = None

    def as_text(self, encoding: str = "utf-8") -> str:
        """Decode object content as UTF-8 text (with replacement on errors)."""
        return self.content.decode(encoding, errors="replace")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable summary (no raw bytes)."""
        return {
            "key": self.key,
            "bucket": self.bucket,
            "content_type": self.content_type,
            "size": self.size,
            "url": self.url,
        }


# Fetcher

class S3ArtifactFetcher:
    """Fetch build artifacts from S3-compatible storage."""

    def __init__(
        self,
        bucket: str = "",
        endpoint_url: Optional[str] = None,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        region: str = "",
    ) -> None:
        from src.config import settings

        cfg = settings.s3
        self._bucket = bucket or cfg.bucket
        self._endpoint_url = endpoint_url or cfg.endpoint_url or None
        self._access_key = access_key or cfg.access_key
        self._secret_key = secret_key or cfg.secret_key
        self._region = region or cfg.region
        self._prefix = cfg.artifacts_prefix
        self._client: Any = None

        if self._bucket and self._access_key and self._secret_key:
            self._client = self._init_boto3()

    # Initialisation

    def _init_boto3(self) -> Any:
        """Attempt to create a boto3 S3 client; return ``None`` on failure."""
        try:
            import boto3  # type: ignore[import]

            kwargs: dict[str, Any] = {
                "aws_access_key_id": self._access_key,
                "aws_secret_access_key": self._secret_key,
                "region_name": self._region,
            }
            if self._endpoint_url:
                kwargs["endpoint_url"] = self._endpoint_url

            client = boto3.client("s3", **kwargs)
            logger.info(
                "S3ArtifactFetcher ready (bucket=%s, endpoint=%s)",
                self._bucket,
                self._endpoint_url or "AWS",
            )
            return client
        except ImportError:
            logger.warning(
                "boto3 not installed — direct S3 access disabled "
                "(pre-signed URL fetch still works)"
            )
        except Exception as exc:
            logger.warning("S3 client init failed: %s", exc)
        return None

    # Properties

    @property
    def enabled(self) -> bool:
        """``True`` when a boto3 client is available for direct bucket access."""
        return self._client is not None

    # Public API

    def fetch_by_presigned_url(self, url: str, timeout: int = 30) -> bytes:
        """Download an object via a pre-signed URL."""
        import requests

        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.content

    def fetch_object(self, key: str, bucket: Optional[str] = None) -> S3Artifact:
        """Fetch a single S3 object by key."""
        if not self._client:
            raise RuntimeError(
                "S3 client not configured — set S3_BUCKET, S3_ACCESS_KEY, S3_SECRET_KEY"
            )
        target_bucket = bucket or self._bucket
        resp = self._client.get_object(Bucket=target_bucket, Key=key)
        content: bytes = resp["Body"].read()
        return S3Artifact(
            key=key,
            bucket=target_bucket,
            content=content,
            content_type=resp.get("ContentType", "application/octet-stream"),
            size=resp.get("ContentLength", len(content)),
        )

    def list_objects(
        self,
        prefix: str = "",
        bucket: Optional[str] = None,
        max_keys: int = 100,
    ) -> list[dict[str, Any]]:
        """List objects in the bucket with optional prefix filter."""
        if not self._client:
            raise RuntimeError("S3 client not configured")

        resp = self._client.list_objects_v2(
            Bucket=bucket or self._bucket,
            Prefix=prefix or self._prefix,
            MaxKeys=max_keys,
        )
        return [
            {
                "key": obj["Key"],
                "size": obj["Size"],
                "last_modified": obj["LastModified"].isoformat(),
                "etag": obj.get("ETag", "").strip('"'),
            }
            for obj in resp.get("Contents", [])
        ]

    def generate_presigned_url(
        self,
        key: str,
        bucket: Optional[str] = None,
        expires_in: int = 3600,
    ) -> str:
        """Generate a pre-signed download URL for an object."""
        if not self._client:
            raise RuntimeError("S3 client not configured")
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket or self._bucket, "Key": key},
            ExpiresIn=expires_in,
        )

    def fetch_log_artifact(self, key: str) -> str:
        """Convenience wrapper — fetch a log file and return as UTF-8 text."""
        artifact = self.fetch_object(key)
        return artifact.as_text()

    def fetch_artifacts_for_build(
        self, build_id: str, max_items: int = 10
    ) -> list[S3Artifact]:
        """Fetch all artifacts stored under a given build-ID prefix."""
        prefix = f"{self._prefix}{build_id}/"
        try:
            items = self.list_objects(prefix=prefix, max_keys=max_items)
        except Exception as exc:
            logger.warning(
                "Failed to list S3 artifacts for build %s: %s", build_id, exc
            )
            return []

        results: list[S3Artifact] = []
        for item in items:
            try:
                artifact = self.fetch_object(item["key"])
                results.append(artifact)
            except Exception as exc:
                logger.warning(
                    "Failed to fetch S3 artifact %s: %s", item["key"], exc
                )
        logger.info(
            "Fetched %d S3 artifact(s) for build %s", len(results), build_id
        )
        return results
