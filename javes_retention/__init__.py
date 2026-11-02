"""Durable, policy-driven Retention worker.

The package implements the existing :class:`javes_main.ports.RetentionPort`
boundary without depending on Main Infra.  Main Infra may run it in-process for
the modular-monolith deployment or expose the same two operations over a
transport later.
"""

from .policy import RetentionPolicy
from .factory import build_local_retention_worker
from .repository import SQLiteRetentionRepository
from .storage import (
    ArtifactStoreDeletionBackend,
    CallbackDeletionBackend,
    DeletionPending,
    DeletionReceipt,
    DeletionRouter,
    LocalFileDeletionBackend,
    StorageLocator,
    UnsupportedStorageLocator,
)
from .worker import RetentionDeletionError, RetentionWorker

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
