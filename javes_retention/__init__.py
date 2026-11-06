"""Durable, policy-driven Retention worker.

The package implements the existing :class:`javes_main.ports.RetentionPort`
boundary without depending on Main Infra.  Main Infra may run it in-process for
the modular-monolith deployment or expose the same two operations over a
transport later.
"""

from .core.policy import RetentionPolicy
from .core.worker import RetentionDeletionError, RetentionWorker
from .runtime.factory import build_local_retention_worker
from .runtime.repository import SQLiteRetentionRepository
from .runtime.storage import (
    ArtifactStoreDeletionBackend,
    CallbackDeletionBackend,
    DeletionPending,
    DeletionReceipt,
    DeletionRouter,
    LocalFileDeletionBackend,
    StorageLocator,
    UnsupportedStorageLocator,
)

__all__ = [
    "ArtifactStoreDeletionBackend",
    "build_local_retention_worker",
    "CallbackDeletionBackend",
    "DeletionPending",
    "DeletionReceipt",
    "DeletionRouter",
    "LocalFileDeletionBackend",
    "RetentionDeletionError",
    "RetentionPolicy",
    "RetentionWorker",
    "SQLiteRetentionRepository",
    "StorageLocator",
    "UnsupportedStorageLocator",
]
