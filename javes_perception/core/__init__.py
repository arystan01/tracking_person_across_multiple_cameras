"""Detection, tracking, Re-ID and probe processing."""

from .config import CameraSource, PerceptionSettings


def __getattr__(name: str):
    if name in {"ExtractedTrack", "ExtractionBundle", "FilesystemExtractionCache"}:
        from . import extraction

        return getattr(extraction, name)
    if name == "PerceptionWorker":
        from .worker import PerceptionWorker

        return PerceptionWorker
    raise AttributeError(name)


__all__ = [
    "CameraSource",
    "ExtractedTrack",
    "ExtractionBundle",
    "FilesystemExtractionCache",
    "PerceptionSettings",
    "PerceptionWorker",
]
