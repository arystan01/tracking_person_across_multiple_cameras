"""Detection, tracking, Re-ID and probe processing."""

from .config import CameraSource, PerceptionSettings
from .extraction import (
    ExtractedTrack,
    ExtractionBundle,
    FilesystemExtractionCache,
)


def __getattr__(name: str):
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
