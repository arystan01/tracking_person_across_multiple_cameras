"""Composition helpers for the local modular-monolith deployment."""

from __future__ import annotations

from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

from .policy import RetentionPolicy
from .repository import SQLiteRetentionRepository
from .storage import (
    ArtifactStoreDeletionBackend,
    CallbackDeletionBackend,
    DeletionBackend,
    DeletionRouter,
    LocalFileDeletionBackend,
    StorageLocator,
)
from .worker import RetentionWorker


def build_local_retention_worker(
    *,
    database_path: str | Path,
    artifact_store: object,
    live_feed_store: object | None = None,
    allowed_file_roots: list[str | Path] | None = None,
    external_backends: dict[str, DeletionBackend] | None = None,
    policy: RetentionPolicy | None = None,
    clock: Callable | None = None,
) -> RetentionWorker:
    """Build the real local worker with explicit storage ownership.

    ``external_backends`` is where product-owned Perception/gallery/live
    storage attaches handlers such as ``uri:gallery``. CCTV ``archive_id``
    references are read-only external inputs and are never deleted here.
    Unknown locators deliberately fail instead of being reported as deleted.
    """

    backends: dict[str, DeletionBackend] = {
        "object_key": ArtifactStoreDeletionBackend(artifact_store),
        **(external_backends or {}),
    }
    if allowed_file_roots:
        backends["uri:file"] = LocalFileDeletionBackend(allowed_file_roots)
    if live_feed_store is not None:
        delete = getattr(live_feed_store, "delete", None)
        if not callable(delete):
            raise TypeError("live_feed_store must provide delete(stream_ref) -> bool")

        def delete_live(locator: StorageLocator) -> bool:
            parsed = urlparse(locator.value)
            stream_ref = unquote(parsed.path)
            if not stream_ref:
                raise ValueError("live locator has no stream reference")
            return bool(delete(stream_ref))

        backends["uri:live"] = CallbackDeletionBackend(
            delete_live, name="live-feed-store"
        )
    kwargs = {}
    if clock is not None:
        kwargs["clock"] = clock
    return RetentionWorker(
        SQLiteRetentionRepository(database_path),
        DeletionRouter(backends),
        policy=policy,
        **kwargs,
    )
