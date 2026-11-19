from __future__ import annotations

from pathlib import Path

from javes_site_config.nvidia import (
    CAMERAS,
    PARAMS_VERSION,
    SITE_ID,
    SITE_VERSION,
    canonical_site,
    write_site_registry,
)
from javes_site_config.registry import FilesystemSiteConfigRegistry


ROOT = Path(__file__).resolve().parents[1]


def test_nvidia_site_bootstrap_is_complete_and_registry_readable(tmp_path) -> None:
    registry_root, checksums = write_site_registry(tmp_path, ROOT)
    registry = FilesystemSiteConfigRegistry(registry_root)
    snapshot = registry.site(SITE_ID, SITE_VERSION)
    params = registry.params(PARAMS_VERSION)
    assert set(snapshot.graph.cameras) == set(CAMERAS)
    assert set(checksums) == set(CAMERAS)
    assert params.version == PARAMS_VERSION
    assert (tmp_path / "sites" / SITE_ID / SITE_VERSION / "map.png").is_file()


def test_nvidia_site_generation_is_deterministic() -> None:
    assert canonical_site(ROOT) == canonical_site(ROOT)
