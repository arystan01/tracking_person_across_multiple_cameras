"""Repository, storage, pull/lease and CLI adapters for Retention."""


def __getattr__(name: str):
    if name == "build_local_retention_worker":
        from .factory import build_local_retention_worker

        return build_local_retention_worker
    if name == "SQLiteRetentionRepository":
        from .repository import SQLiteRetentionRepository

        return SQLiteRetentionRepository
    if name == "RetentionTaskRunner":
        from .runner import RetentionTaskRunner

        return RetentionTaskRunner
    storage_names = {
        "ArtifactStoreDeletionBackend",
        "CallbackDeletionBackend",
        "DeletionPending",
        "DeletionReceipt",
        "DeletionRouter",
        "LocalFileDeletionBackend",
        "StorageLocator",
        "UnsupportedStorageLocator",
    }
    if name in storage_names:
        from . import storage

        return getattr(storage, name)
    raise AttributeError(name)

__all__ = [
    "ArtifactStoreDeletionBackend",
    "CallbackDeletionBackend",
    "DeletionPending",
    "DeletionReceipt",
    "DeletionRouter",
    "LocalFileDeletionBackend",
    "RetentionTaskRunner",
    "SQLiteRetentionRepository",
    "StorageLocator",
    "UnsupportedStorageLocator",
    "build_local_retention_worker",
]
