"""Main Infra package with test composition helpers loaded lazily.

Production entrypoints do not import ``javes_fakes``.  The legacy in-memory
assembly remains available for isolated tests through lazy attributes.
"""

from .application.case_manager import CaseManager

__all__ = [
    "ApplicationSystem",
    "CaseManager",
    "InMemorySystem",
    "assemble_system",
    "build_in_memory_system",
]


def __getattr__(name: str):
    if name in {
        "ApplicationSystem",
        "InMemorySystem",
        "assemble_system",
        "build_in_memory_system",
    }:
        from . import bootstrap

        return getattr(bootstrap, name)
    raise AttributeError(name)
