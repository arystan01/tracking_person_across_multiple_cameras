from .config import CameraSource, PerceptionSettings, load_settings


def __getattr__(name: str):
    if name == "PerceptionWorker":
        from .worker import PerceptionWorker

        return PerceptionWorker
    if name == "HttpVideoArchive":
        from .archive_api import HttpVideoArchive

        return HttpVideoArchive
    raise AttributeError(name)

__all__ = [
    "CameraSource",
    "HttpVideoArchive",
    "PerceptionSettings",
    "PerceptionWorker",
    "load_settings",
]
