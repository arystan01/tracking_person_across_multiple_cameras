from __future__ import annotations

import os
import tempfile
from pathlib import Path


class InMemoryArtifactStore:
    def __init__(self) -> None:
        self._items: dict[str, tuple[bytes, str]] = {}

    def put(self, key: str, data: bytes, mime_type: str) -> str:
        value = (bytes(data), mime_type)
        previous = self._items.get(key)
        if previous is not None and previous != value:
            raise ValueError(f"artifact key {key!r} already has different content")
        self._items[key] = value
        return key

    def get(self, key: str) -> tuple[bytes, str]:
        try:
            data, mime_type = self._items[key]
        except KeyError as exc:
            raise KeyError(f"unknown artifact {key!r}") from exc
        return bytes(data), mime_type

    def keys(self) -> list[str]:
        return sorted(self._items)

    def delete(self, key: str) -> bool:
        """Delete one exact key; absence is an idempotent success."""

        return self._items.pop(key, None) is not None


class InMemoryLiveFeedStore:
    """Short-lived byte source used by the fake Perception/live API path."""

    def __init__(self) -> None:
        self._items: dict[str, tuple[bytes, str]] = {}

    def put(self, stream_ref: str, data: bytes, mime_type: str = "image/jpeg") -> None:
        if not stream_ref:
            raise ValueError("stream_ref must not be empty")
        self._items[stream_ref] = (bytes(data), mime_type)

    def get(self, stream_ref: str) -> tuple[bytes, str]:
        try:
            data, mime_type = self._items[stream_ref]
        except KeyError as exc:
            raise KeyError(f"unknown live stream {stream_ref!r}") from exc
        return bytes(data), mime_type

    def delete(self, stream_ref: str) -> bool:
        return self._items.pop(stream_ref, None) is not None


class FileArtifactStore:
    """Private filesystem artifact store with traversal protection and atomic put."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._mime_path = self.root / ".mime"
        self._mime_path.mkdir(exist_ok=True)

    def _path(self, key: str) -> Path:
        if not key or key.startswith("/"):
            raise ValueError("artifact key must be a non-empty relative path")
        path = (self.root / key).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("artifact key escapes store root") from exc
        if path == self.root:
            raise ValueError("artifact key must identify a file")
        return path

    def _mime_file(self, key: str) -> Path:
        import hashlib

        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self._mime_path / digest

    def put(self, key: str, data: bytes, mime_type: str) -> str:
        path = self._path(key)
        payload = bytes(data)
        if path.exists():
            previous = path.read_bytes()
            previous_mime = self._mime_file(key).read_text(encoding="utf-8")
            if previous != payload or previous_mime != mime_type:
                raise ValueError(f"artifact key {key!r} already has different content")
            return key
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=".javes-", dir=path.parent)
        mime_path = self._mime_file(key)
        mime_fd, temporary_mime = tempfile.mkstemp(prefix=".javes-mime-", dir=self._mime_path)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            with os.fdopen(mime_fd, "w", encoding="utf-8") as stream:
                stream.write(mime_type)
                stream.flush()
                os.fsync(stream.fileno())
            # Publish metadata first and the data path last. Readers either see
            # no artifact or a complete artifact with MIME metadata.
            os.replace(temporary_mime, mime_path)
            os.replace(temporary_name, path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
            if os.path.exists(temporary_mime):
                os.unlink(temporary_mime)
        return key

    def get(self, key: str) -> tuple[bytes, str]:
        path = self._path(key)
        if not path.is_file():
            raise KeyError(f"unknown artifact {key!r}")
        mime_path = self._mime_file(key)
        if not mime_path.is_file():
            raise RuntimeError(f"artifact {key!r} has no MIME metadata")
        return path.read_bytes(), mime_path.read_text(encoding="utf-8")

    def delete(self, key: str) -> bool:
        """Idempotently delete data and its MIME sidecar under the store root."""

        path = self._path(key)
        mime_path = self._mime_file(key)
        existed = False
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise ValueError("artifact key must identify one regular file")
            path.unlink()
            existed = True
        if mime_path.exists():
            if mime_path.is_symlink() or not mime_path.is_file():
                raise ValueError("artifact MIME metadata must be a regular file")
            mime_path.unlink()
            existed = True
        # Remove only empty key directories, never the configured root or .mime.
        parent = path.parent
        while parent != self.root and parent != self._mime_path:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
        return existed
