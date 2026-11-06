from .core.config import CameraSource, PerceptionSettings
from .runtime.config_loader import load_settings


def __getattr__(name: str):
    if name == "PerceptionWorker":
        from .core.worker import PerceptionWorker

        return PerceptionWorker
    if name == "HttpVideoArchive":
        from .runtime.archive_api import HttpVideoArchive

        return HttpVideoArchive
    raise AttributeError(name)

__all__ = [
    "CameraSource",
    "HttpVideoArchive",
    "PerceptionSettings",
    "PerceptionWorker",
    "load_settings",
]
