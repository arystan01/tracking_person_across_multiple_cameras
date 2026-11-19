from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from javes_site_config import (
    FilesystemSiteConfigRegistry,
    PublishConflictError,
    preview_params,
    preview_site,
    publish_params,
    publish_site,
)


ROOT = Path(__file__).resolve().parents[1]


def _site_source(root: Path, *, version: str = "v1") -> Path:
    root.mkdir(parents=True)
    (root / "map.png").write_bytes(b"immutable-map-image")
    raw = {
        "site": "warehouse",
        "version": version,
        "map": {
            "floors": [
                {
                    "id": "1F",
                    "image": "map.png",
                    "size_px": [100, 100],
                    "m_per_px": 0.1,
                    "walkable_polygons": [
                        [[0, 0], [100, 0], [100, 100], [0, 100]]
                    ],
                    "blocked_polygons": [
                        [[40, 40], [60, 40], [60, 60], [40, 60]]
                    ],
                }
            ]
        },
        "cameras": {
            "Cam001": {
                "floor": "1F",
                "map_xy": [10, 20],
                "calibration": {
                    "frame_size_px": [100, 100],
                    "homography": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                    "floor_id": "1F",
                    "error_m": 0.1,
                },
            }
        },
        "edges": [],
        "walk_graph": {
            "nodes": {
                "west": {"floor_id": "1F", "map_xy": [10, 20]},
                "east": {"floor_id": "1F", "map_xy": [90, 20]},
            },
            "edges": [
                {
                    "id": "west-east",
                    "from": "west",
                    "to": "east",
                    "floor_id": "1F",
                    "d_m": 8.0,
                    "width_m": 1.0,
                }
            ],
        },
    }
    (root / "site.yaml").write_text(
        yaml.safe_dump(raw, sort_keys=False), encoding="utf-8"
    )
    return root


def _params_source(path: Path, *, version: str = "params-v1") -> Path:
    raw = yaml.safe_load((ROOT / "params.yaml").read_text(encoding="utf-8"))
    raw["version"] = version
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return path


def test_preview_publish_and_read_exact_immutable_versions(tmp_path: Path) -> None:
    source = _site_source(tmp_path / "source")
    registry_root = tmp_path / "registry"
    preview = preview_site(source)
    assert preview["site_id"] == "warehouse"
    assert preview["version"] == "v1"
    assert preview["cameras"] == ["Cam001"]
    assert preview["calibration"] == {"configured": 1, "total_cameras": 1}
    assert preview["validation"]["walk_geometry_valid"] is True

    published = publish_site(source, registry_root)
    assert published == preview
    target = registry_root / "sites/warehouse/v1"
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["format"] == "javes-site-bundle.v1"
    assert set(manifest["files"]) == {"site.yaml", "map.png"}

    registry = FilesystemSiteConfigRegistry(registry_root)
    snapshot = registry.site("warehouse", "v1")
    assert snapshot.checksum == preview["graph_checksum"]
    assert registry.asset("warehouse", "v1", "1F")[0] == b"immutable-map-image"

    params_path = _params_source(tmp_path / "params-v1.yaml")
    params_preview = preview_params(params_path)
    assert params_preview["version"] == "params-v1"
    assert publish_params(params_path, registry_root) == params_preview
    assert registry.params("params-v1").checksum == params_preview["checksum"]


def test_duplicate_publish_conflicts_and_incomplete_staging_is_invisible(
    tmp_path: Path,
) -> None:
    source = _site_source(tmp_path / "source")
    registry_root = tmp_path / "registry"
    publish_site(source, registry_root)
    with pytest.raises(PublishConflictError, match="already exists"):
        publish_site(source, registry_root)

    staging = registry_root / "sites/warehouse/.staging-v2-incomplete"
    staging.mkdir(parents=True)
    (staging / "site.yaml").write_text("site: warehouse\nversion: v2\n")
    with pytest.raises(FileNotFoundError):
        FilesystemSiteConfigRegistry(registry_root).site("warehouse", "v2")

    params_path = _params_source(tmp_path / "params.yaml")
    publish_params(params_path, registry_root)
    with pytest.raises(PublishConflictError, match="already exists"):
        publish_params(params_path, registry_root)


def test_published_bundle_detects_mutation_and_reader_rejects_symlinks(
    tmp_path: Path,
) -> None:
    source = _site_source(tmp_path / "source")
    registry_root = tmp_path / "registry"
    publish_site(source, registry_root)
    target = registry_root / "sites/warehouse/v1"
    (target / "map.png").write_bytes(b"mutated")
    with pytest.raises(ValueError, match="checksum mismatch"):
        FilesystemSiteConfigRegistry(registry_root).site("warehouse", "v1")

    source = _site_source(tmp_path / "symlink-source")
    real_map = source / "real-map.png"
    (source / "map.png").rename(real_map)
    (source / "map.png").symlink_to(real_map.name)
    with pytest.raises(ValueError, match="must not contain symlinks"):
        preview_site(source)


def test_invalid_calibration_and_blocked_walk_edge_never_publish(
    tmp_path: Path,
) -> None:
    invalid_calibration = _site_source(tmp_path / "invalid-calibration")
    raw = yaml.safe_load(
        (invalid_calibration / "site.yaml").read_text(encoding="utf-8")
    )
    raw["cameras"]["Cam001"]["calibration"]["homography"] = [
        [1, 0, 0],
        [1, 0, 0],
        [0, 0, 1],
    ]
    (invalid_calibration / "site.yaml").write_text(
        yaml.safe_dump(raw, sort_keys=False), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="invertible"):
        publish_site(invalid_calibration, tmp_path / "registry-a")

    blocked = _site_source(tmp_path / "blocked")
    raw = yaml.safe_load((blocked / "site.yaml").read_text(encoding="utf-8"))
    raw["walk_graph"]["nodes"]["west"]["map_xy"] = [10, 50]
    raw["walk_graph"]["nodes"]["east"]["map_xy"] = [90, 50]
    (blocked / "site.yaml").write_text(
        yaml.safe_dump(raw, sort_keys=False), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="blocked area"):
        publish_site(blocked, tmp_path / "registry-b")


def test_concurrent_publish_has_one_winner_and_exact_version_rollback(
    tmp_path: Path,
) -> None:
    registry_root = tmp_path / "registry"
    v1 = _site_source(tmp_path / "v1", version="v1")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(publish_site, v1, registry_root) for _ in range(2)]
    outcomes = []
    for future in futures:
        try:
            future.result()
            outcomes.append("published")
        except PublishConflictError:
            outcomes.append("conflict")
    assert sorted(outcomes) == ["conflict", "published"]

    registry = FilesystemSiteConfigRegistry(registry_root)
    pinned_v1 = registry.site("warehouse", "v1")
    v2 = _site_source(tmp_path / "v2", version="v2")
    publish_site(v2, registry_root)
    assert registry.site("warehouse", "v2").version == "v2"
    assert registry.site("warehouse", "v1") == pinned_v1
    with pytest.raises(ValueError, match="invalid site version"):
        registry.site("warehouse", "latest")
