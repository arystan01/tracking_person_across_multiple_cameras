"""Main Infra storage, site, and Routing adapters."""

__all__ = [
    "FileArtifactStore",
    "InMemoryArtifactStore",
    "InMemoryLiveFeedStore",
    "InMemorySiteConfig",
    "LocalRoutingAdapter",
    "HttpRoutingAdapter",
]


def __getattr__(name: str):
    if name in {"FileArtifactStore", "InMemoryArtifactStore", "InMemoryLiveFeedStore"}:
        from . import artifacts

        return getattr(artifacts, name)
    if name == "InMemorySiteConfig":
        from .site_config import InMemorySiteConfig

        return InMemorySiteConfig
    if name == "LocalRoutingAdapter":
        from .routing import LocalRoutingAdapter

        return LocalRoutingAdapter
    if name == "HttpRoutingAdapter":
        from .http_routing import HttpRoutingAdapter

        return HttpRoutingAdapter
    raise AttributeError(name)
