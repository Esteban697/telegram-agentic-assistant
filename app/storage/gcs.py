"""Original documents in Cloud Storage (PLAN.md §6.1 stage 5, §13).

Layer 1. Two implementations behind one small port:

* **`GcsObjectStore`** — the real bucket, private, addressed by our own object naming
  (`u/{user_id}/documents/{document_id}/{sanitized}`), so no path is ever built from user input.
* **`InMemoryObjectStore`** — used by the credential-free suites. The bucket is a user decision
  (§13.6) that has not been made, and Phase 4 must not create cloud resources, so the real path
  is exercised only by the live-marked test.

Deletion is verified rather than assumed: `delete` returns whether the object is *gone*
afterwards, because spec §9 requires not claiming a complete deletion before checking.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from app.errors import AssistantError

_logger = logging.getLogger(__name__)


class ObjectStoreUnavailable(AssistantError):
    code = "object_store_unavailable"
    message_key = "error.capability_unavailable"


@runtime_checkable
class ObjectStore(Protocol):
    """Where the original bytes live."""

    async def put(self, *, key: str, data: bytes, content_type: str) -> str:
        """Store the object and return its URI."""

    async def get(self, *, key: str) -> bytes | None:
        ...

    async def exists(self, *, key: str) -> bool:
        ...

    async def delete(self, *, key: str) -> bool:
        """Delete, returning True only when the object is verifiably gone."""


def object_key(*, user_id: int, document_id: Any, sanitized_filename: str) -> str:
    """Our naming scheme. The user's filename is a leaf, never a path."""

    return f"u/{user_id}/documents/{document_id}/{sanitized_filename}"


class InMemoryObjectStore:
    """A dictionary with the same contract. Test double; never wired in production."""

    def __init__(self, bucket: str = "in-memory-bucket") -> None:
        self.bucket = bucket
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self.puts = 0

    async def put(self, *, key: str, data: bytes, content_type: str) -> str:
        self.objects[key] = data
        self.content_types[key] = content_type
        self.puts += 1
        return f"gs://{self.bucket}/{key}"

    async def get(self, *, key: str) -> bytes | None:
        return self.objects.get(key)

    async def exists(self, *, key: str) -> bool:
        return key in self.objects

    async def delete(self, *, key: str) -> bool:
        self.objects.pop(key, None)
        return key not in self.objects


class GcsObjectStore:
    """The real bucket through `google-cloud-storage`, using the service identity (no key file)."""

    def __init__(self, *, bucket: str, client: Any | None = None) -> None:
        if not bucket:
            raise ObjectStoreUnavailable(
                "GCS_BUCKET is not set, so originals cannot be stored",
                context={"remedy": "create a private bucket and set GCS_BUCKET (§13.6)"},
            )
        self.bucket = bucket
        if client is None:
            try:
                from google.cloud import storage
            except ImportError as exc:  # pragma: no cover - dependency is pinned
                raise ObjectStoreUnavailable("google-cloud-storage is not installed") from exc
            client = storage.Client()
        self._client = client

    def _blob(self, key: str) -> Any:
        return self._client.bucket(self.bucket).blob(key)

    async def put(self, *, key: str, data: bytes, content_type: str) -> str:
        # The client is synchronous; the call is small and bounded by MAX_UPLOAD_MB, and running
        # it in a thread would add a dependency on an executor for no benefit at this size.
        blob = self._blob(key)
        blob.upload_from_string(data, content_type=content_type)
        return f"gs://{self.bucket}/{key}"

    async def get(self, *, key: str) -> bytes | None:
        blob = self._blob(key)
        if not blob.exists():
            return None
        return bytes(blob.download_as_bytes())

    async def exists(self, *, key: str) -> bool:
        return bool(self._blob(key).exists())

    async def delete(self, *, key: str) -> bool:
        blob = self._blob(key)
        if not blob.exists():
            return True  # already gone: the postcondition holds
        blob.delete()
        return not blob.exists()


def build_object_store(settings: Any, *, client: Any | None = None) -> ObjectStore:
    """Choose the store from configuration. `GCS_BUCKET` empty means the in-memory double."""

    if not settings.gcs_bucket:
        if settings.is_production:  # pragma: no cover - refused by validation
            raise ObjectStoreUnavailable("GCS_BUCKET is required in production")
        _logger.warning(
            "GCS_BUCKET is empty: originals are kept in memory and will not survive a restart",
            extra={"error_code": "object_store_in_memory"},
        )
        return InMemoryObjectStore()
    return GcsObjectStore(bucket=settings.gcs_bucket, client=client)


__all__ = [
    "GcsObjectStore",
    "InMemoryObjectStore",
    "ObjectStore",
    "ObjectStoreUnavailable",
    "build_object_store",
    "object_key",
]
