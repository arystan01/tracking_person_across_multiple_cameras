"""CCTV, durable state, pull/lease and CLI adapters for Perception."""


def __getattr__(name: str):
    if name == "HttpVideoArchive":
        from .archive_api import HttpVideoArchive

        return HttpVideoArchive
    if name == "load_settings":
        from .config_loader import load_settings

        return load_settings
    if name == "PerceptionTaskRunner":
        from .runner import PerceptionTaskRunner

        return PerceptionTaskRunner
    if name == "PerceptionStateStore":
        from .state import PerceptionStateStore

        return PerceptionStateStore
    raise AttributeError(name)

__all__ = [
    "HttpVideoArchive",
    "PerceptionStateStore",
    "PerceptionTaskRunner",
    "load_settings",
]
