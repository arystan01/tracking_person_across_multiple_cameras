"""Canonical immutable NVIDIA 12-camera Site Config used by real-CV tests."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import yaml

from tests.support.real_cv_e2e import (
    nvidia_pixel_to_map_homographies,
    project_pixel,
)


SITE_ID = "nvidia-mtmc-12cam"
SITE_VERSION = "nvidia-12cam-v1"
PARAMS_VERSION = "nvidia-eval-params-v1"
FLOOR_ID = "warehouse"
MAP_SIZE = (1130, 1080)
M_PER_PX = 0.3048 / 34.282
CAMERAS = tuple(f"Cam{index:03d}" for index in range(1, 13))


def _clip(point: tuple[float, float]) -> list[float]:
    return [
        round(min(max(point[0], 0.0), MAP_SIZE[0]), 4),
        round(min(max(point[1], 0.0), MAP_SIZE[1]), 4),
    ]


def _edge(
    edge_id: str,
    source: str,
    target: str,
    source_xy: tuple[float, float],
    target_xy: tuple[float, float],
) -> dict:
    distance = max(
        ((target_xy[0] - source_xy[0]) ** 2 + (target_xy[1] - source_xy[1]) ** 2)
        ** 0.5
        * M_PER_PX,
        0.2,
    )
    typical = max(distance / 1.3, 0.2)
    return {
        "id": edge_id,
        "from": source,
        "to": target,
        "t_min": round(distance / 2.0, 4),
        "t_typ": round(typical, 4),
        "t_max": round(max(typical * 2.5, distance / 2.0), 4),
        "d_m": round(distance, 4),
        "floor": FLOOR_ID,
    }


def canonical_site(repository_root: Path) -> dict:
    fixture = repository_root / "data/nvidia_mtmc/mtmc_12cam"
    homographies = nvidia_pixel_to_map_homographies(fixture, list(CAMERAS))
    cameras = {}
    for camera_id in CAMERAS:
        homography = homographies[camera_id]
        map_xy = _clip(project_pixel(homography, 960, 1080))
        coverage = [
            _clip(project_pixel(homography, x, y))
            for x, y in ((0, 1080), (1920, 1080), (1920, 600), (0, 600))
        ]
        cameras[camera_id] = {
            "zone": camera_id.lower(),
            "floor": FLOOR_ID,
            "map_xy": map_xy,
            "coverage_polygon": coverage,
            "calibration": {
                "frame_size_px": [1920, 1080],
                "homography": homography,
                "floor_id": FLOOR_ID,
                "error_m": 0.5,
            },
        }

    walk_nodes: dict[str, dict] = {}
    verticals = (140, 310, 480, 650, 820, 990)
    for y, label in ((140, "north"), (850, "south"), (950, "exit")):
        for x in verticals:
            walk_nodes[f"{label}-{x}"] = {
                "floor_id": FLOOR_ID,
                "map_xy": [x, y],
                "kind": "ENTRANCE" if label == "exit" else "WAYPOINT",
            }
    walk_nodes.update(
        {
            "east-upper-820": {"floor_id": FLOOR_ID, "map_xy": [820, 540]},
            "east-lower-820": {"floor_id": FLOOR_ID, "map_xy": [820, 800]},
            "far-east-upper": {"floor_id": FLOOR_ID, "map_xy": [990, 620]},
            "far-east-lower": {"floor_id": FLOOR_ID, "map_xy": [990, 800]},
        }
    )
    walk_edges = []

    def walk(edge_id: str, source: str, target: str, *, prior: float | None = None):
        item = {
            "id": edge_id,
            "from": source,
            "to": target,
            "floor_id": FLOOR_ID,
            "width_m": 0.6,
            "bidirectional": True,
        }
        if prior is not None:
            item["branch_prior"] = prior
        walk_edges.append(item)

    for label in ("north", "south", "exit"):
        for left, right in zip(verticals, verticals[1:]):
            walk(f"walk-{label}-{left}-{right}", f"{label}-{left}", f"{label}-{right}")
    for x in (140, 310, 480, 650):
        walk(f"walk-aisle-{x}", f"north-{x}", f"south-{x}")
    walk("walk-aisle-820-upper", "north-820", "east-upper-820")
    walk("walk-aisle-820-lower", "east-lower-820", "south-820")
    walk("walk-aisle-990-upper", "north-990", "far-east-upper")
    walk("walk-aisle-990-lower", "far-east-lower", "south-990")
    for x in verticals:
        walk(f"walk-exit-{x}", f"south-{x}", f"exit-{x}")

    hubs = {
        "blind-north": (650.0, 140.0),
        "blind-south": (650.0, 850.0),
        "blind-exit": (650.0, 950.0),
    }
    blind_nodes = [
        {
            "id": name,
            "floor": FLOOR_ID,
            "map_xy": list(point),
            "borders": [],
            "dwell": {"median_s": 20, "sigma": 0.9},
        }
        for name, point in hubs.items()
    ]
    route_edges = []
    for camera_id, camera in cameras.items():
        camera_xy = tuple(camera["map_xy"])
        hub = (
            "blind-north"
            if camera_xy[1] < 500
            else "blind-south"
            if camera_xy[1] < 880
            else "blind-exit"
        )
        blind = next(item for item in blind_nodes if item["id"] == hub)
        blind["borders"].append(camera_id)
        route_edges.append(
            _edge(f"{camera_id}-{hub}", camera_id, hub, camera_xy, hubs[hub])
        )
        route_edges.append(
            _edge(f"{hub}-{camera_id}", hub, camera_id, hubs[hub], camera_xy)
        )
    for source, target in (
        ("blind-north", "blind-south"),
        ("blind-south", "blind-north"),
        ("blind-south", "blind-exit"),
        ("blind-exit", "blind-south"),
    ):
        route_edges.append(
            _edge(f"{source}-{target}", source, target, hubs[source], hubs[target])
        )

    return {
        "site": SITE_ID,
        "version": SITE_VERSION,
        "timezone": "UTC",
        "map": {
            "floors": [
                {
                    "id": FLOOR_ID,
                    "image": "map.png",
                    "size_px": list(MAP_SIZE),
                    "m_per_px": M_PER_PX,
                    "walkable_polygons": [
                        [[25, 20], [1105, 20], [1105, 1065], [25, 1065]]
                    ],
                    "blocked_polygons": [
                        [[35, 185], [78, 185], [78, 790], [35, 790]],
                        [[203, 185], [244, 185], [244, 790], [203, 790]],
                        [[372, 185], [419, 185], [419, 790], [372, 790]],
                        [[543, 185], [589, 185], [589, 790], [543, 790]],
                        [[712, 185], [755, 185], [755, 790], [712, 790]],
                        [[883, 185], [929, 185], [929, 790], [883, 790]],
                        [[1052, 185], [1098, 185], [1098, 790], [1052, 790]],
                        [[780, 565], [870, 565], [870, 775], [780, 775]],
                        [[955, 665], [1040, 665], [1040, 780], [955, 780]],
                    ],
                }
            ]
        },
        "cameras": cameras,
        "blind_nodes": blind_nodes,
        "outside": {
            "entrances": {
                "Cam009": 0.25,
                "Cam010": 0.25,
                "Cam011": 0.25,
                "Cam012": 0.25,
            }
        },
        "edges": route_edges,
        "walk_graph": {"nodes": walk_nodes, "edges": walk_edges},
        "max_gap_s": 2400,
    }


def calibration_checksums(site: dict) -> dict[str, str]:
    return {
        camera_id: hashlib.sha256(
            json.dumps(
                camera["calibration"], sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        for camera_id, camera in site["cameras"].items()
    }


def write_site_registry(
    root: Path,
    repository_root: Path,
) -> tuple[Path, dict[str, str]]:
    site = canonical_site(repository_root)
    fixture = repository_root / "data/nvidia_mtmc/mtmc_12cam"
    site_dir = root / "sites" / SITE_ID / SITE_VERSION
    params_dir = root / "routing_params"
    site_dir.mkdir(parents=True, exist_ok=True)
    params_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(fixture / "map.png", site_dir / "map.png")
    (site_dir / "site.yaml").write_text(
        yaml.safe_dump(site, sort_keys=False), encoding="utf-8"
    )
    (params_dir / f"{PARAMS_VERSION}.yaml").write_text(
        "\n".join(
            (
                f"version: {PARAMS_VERSION}",
                "K: 4",
                "B: 5",
                "max_walk_snap_m: 3",
                "reachable_time_step_s: 0.5",
                "reachable_method_version: relative-timegraph-v1",
                "",
            )
        ),
        encoding="utf-8",
    )
    return root, calibration_checksums(site)
