"""Pluggable physical-deletion backends used by Retention."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import unquote, urlparse

from javes_contracts import ArtifactRef, RetentionResource


@dataclass(frozen=True, order=True)
class StorageLocator:
    kind: str
    value: str


@dataclass(frozen=True)
class DeletionReceipt:
    locator: StorageLocator
    deleted: bool
    already_absent: bool
    final: bool = True
    backend: str = "unknown"
    backup_expires_at: datetime | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["locator"] = asdict(self.locator)
        if self.backup_expires_at is not None:
            value["backup_expires_at"] = self.backup_expires_at.astimezone(
                timezone.utc
            ).isoformat()
        return value


class DeletionBackend(Protocol):
    def delete(self, locator: StorageLocator) -> DeletionReceipt: ...


class UnsupportedStorageLocator(RuntimeError):
    pass


class DeletionPending(RuntimeError):
    """A backend accepted deletion but cannot yet prove final erasure."""

    def __init__(self, receipt: DeletionReceipt) -> None:
        super().__init__(
            f"deletion for {receipt.locator.kind}:{receipt.locator.value} is not final"
        )
        self.receipt = receipt


def artifact_locators(ref: ArtifactRef | None) -> list[StorageLocator]:
    if ref is None:
        return []
    values: list[StorageLocator] = []
    if ref.object_key:
        values.append(StorageLocator("object_key", ref.object_key))
    if ref.archive_id:
        raise UnsupportedStorageLocator(
            "archive_id belongs to the external CCTV archive and is read-only"
        )
    if ref.uri:
        parsed = urlparse(ref.uri)
        if not parsed.scheme:
            raise UnsupportedStorageLocator(
                "artifact URI requires an explicit scheme"
            )
        values.append(StorageLocator(f"uri:{parsed.scheme.lower()}", ref.uri))
    return sorted(values)


class DeletionRouter:
    """Deletes every locator on a resource, including declared replicas."""

    def __init__(
        self,
        backends: dict[str, DeletionBackend] | None = None,
    ) -> None:
        self.backends = dict(backends or {})

    def add_backend(self, locator_kind: str, backend: DeletionBackend) -> None:
        self.backends[locator_kind] = backend

    def delete(self, resource: RetentionResource) -> list[DeletionReceipt]:
        locators = artifact_locators(resource.artifact_ref)
        if not locators:
            return [
                DeletionReceipt(
                    locator=StorageLocator("metadata", resource.resource_id),
                    deleted=True,
                    already_absent=False,
                    backend="metadata",
                    detail="logical resource; Main Infra redacts its stored payload",
                )
            ]
        receipts: list[DeletionReceipt] = []
        for locator in locators:
            backend = self.backends.get(locator.kind)
            if backend is None:
                raise UnsupportedStorageLocator(
                    f"no deletion backend configured for {locator.kind!r}"
                )
            receipt = backend.delete(locator)
            if receipt.locator != locator:
                raise ValueError("deletion backend returned a receipt for another locator")
            receipts.append(receipt)
            if not receipt.final:
                raise DeletionPending(receipt)
        return receipts


class ArtifactStoreDeletionBackend:
    """Adapter for Main Infra artifact stores exposing idempotent ``delete``."""

    def __init__(self, store: object, *, name: str = "artifact-store") -> None:
        delete = getattr(store, "delete", None)
        if not callable(delete):
            raise TypeError("artifact store must provide delete(key) -> bool")
        self.store = store
        self.name = name

    def delete(self, locator: StorageLocator) -> DeletionReceipt:
        if locator.kind != "object_key":
            raise UnsupportedStorageLocator(locator.kind)
        existed = bool(self.store.delete(locator.value))  # type: ignore[attr-defined]
        return DeletionReceipt(
            locator=locator,
            deleted=existed,
            already_absent=not existed,
            backend=self.name,
        )


class CallbackDeletionBackend:
    """Small adapter for gallery/live or other product-owned storage."""

    def __init__(
        self,
        callback: Callable[[StorageLocator], bool | DeletionReceipt],
        *,
        name: str,
    ) -> None:
        self.callback = callback
        self.name = name

    def delete(self, locator: StorageLocator) -> DeletionReceipt:
        result = self.callback(locator)
        if isinstance(result, DeletionReceipt):
            return result
        existed = bool(result)
        return DeletionReceipt(
            locator=locator,
            deleted=existed,
            already_absent=not existed,
            backend=self.name,
        )


class LocalFileDeletionBackend:
    """Deletes a single ``file://`` artifact under explicit allow-listed roots.

    Directories and symlinks are refused; Retention never performs recursive
    deletion.  This makes a bad resource registration fail closed instead of
    turning into a broad filesystem deletion.
    """

    def __init__(self, roots: list[str | Path]) -> None:
        if not roots:
            raise ValueError("at least one allowed file root is required")
        self.roots = tuple(Path(root).resolve() for root in roots)

    def _safe_path(self, locator: StorageLocator) -> Path:
        if locator.kind != "uri:file":
            raise UnsupportedStorageLocator(locator.kind)
        parsed = urlparse(locator.value)
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            raise ValueError("only local file:// URIs are accepted")
        path = Path(unquote(parsed.path))
        if not path.is_absolute():
            raise ValueError("file URI must contain an absolute path")
        if path.is_symlink():
            raise ValueError("symlink deletion is not allowed")
        resolved = path.resolve(strict=False)
        if not any(
            resolved != root and resolved.is_relative_to(root) for root in self.roots
        ):
            raise ValueError("file URI is outside configured retention roots")
        return resolved

    def delete(self, locator: StorageLocator) -> DeletionReceipt:
        path = self._safe_path(locator)
        if not path.exists():
            return DeletionReceipt(
                locator=locator,
                deleted=False,
                already_absent=True,
                backend="local-file",
            )
        if not path.is_file():
            raise ValueError("retention file locator must identify one regular file")
        path.unlink()
        try:
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            # Directory fsync is unavailable on some development filesystems;
            # unlink still completed and a later retry verifies absence.
            pass
        return DeletionReceipt(
            locator=locator,
            deleted=True,
            already_absent=False,
            backend="local-file",
        )
