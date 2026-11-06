"""Detection, tracking, Re-ID and probe processing."""

from .config import CameraSource, PerceptionSettings


def __getattr__(name: str):
    if name == "PerceptionWorker":
        from .worker import PerceptionWorker

        return PerceptionWorker
    raise AttributeError(name)


__all__ = ["CameraSource", "PerceptionSettings", "PerceptionWorker"]
