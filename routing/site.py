from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

import yaml

from .contracts import (
    BlindNodeDef,
    CameraDef,
    EdgeDef,
    MapDef,
    OutsideDef,
    RegionDef,
    SiteGraph,
)
from .contracts.params import RoutingParams

logger = logging.getLogger(__name__)


def load_site(path: str | Path, params: RoutingParams | None = None) -> SiteGraph:
    """Load and validate a site YAML document.

    Invalid site configuration is the one class of input error that Routing
    intentionally raises instead of skipping.
    """

    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Site config not found: {config_path}")
    with config_path.open(encoding="utf-8") as stream:
        raw = yaml.safe_load(stream)
    if raw is None:
        raise ValueError("Empty site config")
    if not isinstance(raw, dict):
        raise ValueError("Site config root must be a mapping")
    return parse_site(raw, params=params, base_path=config_path.parent)


def _region_items(raw_regions: Any) -> list[tuple[str, dict[str, Any]]]:
    if raw_regions is None:
        return []
    if isinstance(raw_regions, dict):
        result: list[tuple[str, dict[str, Any]]] = []
        for name, value in raw_regions.items():
            if isinstance(value, dict):
                item = dict(value)
                polygon = item.pop("polygon", item.pop("points", None))
                if polygon is None:
                    raise ValueError(f"Region '{name}' is missing polygon")
                item["polygon"] = polygon
            else:
                item = {"polygon": value}
            result.append((str(name), item))
        return result
    if isinstance(raw_regions, list):
        result = []
        for value in raw_regions:
            if not isinstance(value, dict) or not value.get("name"):
                raise ValueError("Camera region list entries require a name")
            item = dict(value)
            name = str(item.pop("name"))
            result.append((name, item))
        return result
    raise ValueError("Camera regions must be a mapping or list")


def _node_floor_xy(
    node: str,
    cameras: dict[str, CameraDef],
    blind_nodes: list[BlindNodeDef],
) -> tuple[str | None, tuple[float, float] | None]:
    if node in cameras:
        camera = cameras[node]
        return camera.floor, camera.map_xy
    blind = next((item for item in blind_nodes if item.id == node), None)
    if blind is not None:
        return blind.floor, blind.map_xy
    return None, None


def _validate_point(
    label: str,
    floor_id: str | None,
    point: tuple[float, float] | None,
    sizes: dict[str, tuple[int, int]],
) -> None:
    if point is None:
        return
    if floor_id is None:
        if sizes:
            raise ValueError(f"{label} has map coordinates but no floor")
        return
    if floor_id not in sizes:
        raise ValueError(f"{label} references unknown floor '{floor_id}'")
    width, height = sizes[floor_id]
    x, y = point
    if not (0 <= x <= width and 0 <= y <= height):
        raise ValueError(f"{label} point {point} lies outside floor '{floor_id}' size {(width, height)}")


def parse_site(
    raw: dict[str, Any],
    params: RoutingParams | None = None,
    base_path: Path | None = None,
) -> SiteGraph:
    """Parse both the canonical architecture YAML and the legacy test form."""

    params = params or RoutingParams()
    allowed_root = {
        "site", "site_id", "version", "timezone", "cameras", "edges",
        "blind_nodes", "outside", "map", "max_gap_s", "gate_speed",
        "max_speed", "walk_speed", "tmax_over_ttyp",
    }
    unknown_root = sorted(set(raw) - allowed_root)
    if unknown_root:
        raise ValueError(f"Unknown site config field(s): {unknown_root}")
    cameras_raw = raw.get("cameras", {})
    edges_raw = raw.get("edges", [])
    blind_raw = raw.get("blind_nodes", [])
    outside_raw = raw.get("outside")
    if not isinstance(cameras_raw, dict) or not cameras_raw:
        raise ValueError("Site config must define at least one camera")
    if not isinstance(edges_raw, list):
        raise ValueError("edges must be a list")
    if not isinstance(blind_raw, list):
        raise ValueError("blind_nodes must be a list")

    # Normalize edges first so camera regions can infer references from `via`.
    edge_dicts: list[dict[str, Any]] = []
    edge_ids: set[str] = set()
    gate_speed = params.gate_speed
    walk_speed = params.walk_speed
    tmax_factor = params.tmax_over_ttyp
    if "gate_speed" in raw or "max_speed" in raw:
        logger.warning("site-level gate/max_speed is deprecated; move it to params.yaml")
        gate_speed = float(raw.get("gate_speed", raw.get("max_speed")))
    if "walk_speed" in raw:
        logger.warning("site-level walk_speed is deprecated; move it to params.yaml")
        walk_speed = float(raw["walk_speed"])
    if "tmax_over_ttyp" in raw:
        logger.warning("site-level tmax_over_ttyp is deprecated; move it to params.yaml")
        tmax_factor = float(raw["tmax_over_ttyp"])

    for item in edges_raw:
        if not isinstance(item, dict):
            raise ValueError("Every edge must be a mapping")
        data = dict(item)
        edge_id = data.get("id")
        if not edge_id:
            raise ValueError("Edge missing ID")
        if edge_id in edge_ids:
            raise ValueError(f"Duplicate edge ID found: {edge_id}")
        edge_ids.add(str(edge_id))
        if "source" not in data and "from" in data:
            data["source"] = data.pop("from")
        if "target" not in data and "to" in data:
            data["target"] = data.pop("to")
        distance = data.get("d_m")
        if data.get("t_min") is None:
            if distance is None:
                raise ValueError(f"Edge '{edge_id}' requires t_min or d_m")
            data["t_min"] = float(distance) / gate_speed
        if data.get("t_typ") is None:
            if distance is None:
                raise ValueError(f"Edge '{edge_id}' requires t_typ or d_m")
            data["t_typ"] = float(distance) / walk_speed
        if data.get("t_max") is None:
            data["t_max"] = float(data["t_typ"]) * tmax_factor
        edge_dicts.append(data)

    camera_ids = set(str(key) for key in cameras_raw)
    blind_ids: set[str] = set()
    for item in blind_raw:
        if not isinstance(item, dict) or not item.get("id"):
            raise ValueError("Blind node missing ID")
        node_id = str(item["id"])
        if node_id in camera_ids or node_id in blind_ids:
            raise ValueError(f"Duplicate node ID found: {node_id}")
        blind_ids.add(node_id)

    all_nodes = camera_ids | blind_ids
    if outside_raw is not None:
        all_nodes.add("OUTSIDE")

    for data in edge_dicts:
        if data.get("source") not in all_nodes:
            raise ValueError(f"Edge '{data['id']}' has invalid source '{data.get('source')}'")
        if data.get("target") not in all_nodes:
            raise ValueError(f"Edge '{data['id']}' has invalid target '{data.get('target')}'")

    cameras: dict[str, CameraDef] = {}
    for camera_id, raw_camera in cameras_raw.items():
        if not isinstance(raw_camera, dict):
            raise ValueError(f"Camera '{camera_id}' must be a mapping")
        data = dict(raw_camera)
        regions: list[RegionDef] = []
        for name, region_data in _region_items(data.pop("regions", None)):
            region_edges = list(region_data.pop("edges", []))
            if not region_edges:
                region_edges = [
                    str(edge["id"])
                    for edge in edge_dicts
                    if edge.get("via") == name
                    and (edge.get("source") == camera_id or edge.get("target") == camera_id)
                ]
            unknown = sorted(set(region_edges) - edge_ids)
            if unknown:
                raise ValueError(f"Region '{name}' references unknown edge(s) {unknown}")
            regions.append(RegionDef(name=name, edges=sorted(region_edges), **region_data))
        data["id"] = camera_id
        data["regions"] = regions
        cameras[camera_id] = CameraDef(**data)

    blind_nodes = [BlindNodeDef(**dict(item)) for item in blind_raw]
    for node in blind_nodes:
        unknown_borders = sorted(set(node.borders) - camera_ids)
        unknown_exits = sorted(set(node.exits) - camera_ids)
        if unknown_borders or unknown_exits:
            raise ValueError(f"Blind node '{node.id}' references unknown cameras {unknown_borders + unknown_exits}")
        if node.exits:
            if any(value < 0 for value in node.exits.values()) or sum(node.exits.values()) <= 0:
                raise ValueError(f"Blind node '{node.id}' has invalid exit probabilities")

    outside = None
    if outside_raw is not None:
        if not isinstance(outside_raw, dict):
            raise ValueError("outside must be a mapping")
        outside_data = dict(outside_raw)
        entrances = dict(outside_data.get("entrances", {}))
        unknown = sorted(set(entrances) - camera_ids)
        if unknown:
            raise ValueError(f"Outside entrance references unknown camera(s) {unknown}")
        if any(float(value) < 0 for value in entrances.values()):
            raise ValueError("Outside entrance probabilities must be non-negative")
        total = sum(float(value) for value in entrances.values())
        if entrances and total <= 0:
            raise ValueError("Outside entrance probabilities sum to 0")
        if entrances and not math.isclose(total, 1.0, abs_tol=0.01):
            logger.warning("Outside entrances probabilities sum to %s, normalizing to 1.0", total)
            entrances = {key: float(value) / total for key, value in entrances.items()}
        outside_data["entrances"] = entrances
        outside = OutsideDef(**outside_data)

    edges: list[EdgeDef] = []
    for data in edge_dicts:
        edge = EdgeDef(**data)
        if not (edge.t_min <= edge.t_typ <= edge.t_max):
            raise ValueError(
                f"Edge '{edge.id}' has invalid timings: "
                f"t_min={edge.t_min}, t_typ={edge.t_typ}, t_max={edge.t_max}"
            )
        edges.append(edge)

    map_def = MapDef(**raw["map"]) if raw.get("map") is not None else None
    floor_sizes = {floor.id: floor.size_px for floor in map_def.floors} if map_def else {}
    if map_def:
        if len(floor_sizes) != len(map_def.floors):
            raise ValueError("Duplicate floor ID in map")
        for floor in map_def.floors:
            if floor.image and base_path is not None and not (base_path / floor.image).exists():
                logger.warning("Map image does not exist: %s", base_path / floor.image)

    for camera in cameras.values():
        _validate_point(f"Camera '{camera.id}'", camera.floor, camera.map_xy, floor_sizes)
    for node in blind_nodes:
        _validate_point(f"Blind node '{node.id}'", node.floor, node.map_xy, floor_sizes)
    for edge in edges:
        source_floor, source_xy = _node_floor_xy(edge.source, cameras, blind_nodes)
        target_floor, target_xy = _node_floor_xy(edge.target, cameras, blind_nodes)
        if edge.floor is not None and edge.floor not in floor_sizes:
            raise ValueError(f"Edge '{edge.id}' references unknown floor '{edge.floor}'")
        if source_floor and target_floor and source_floor != target_floor and edge.map_path:
            raise ValueError(f"Vertical edge '{edge.id}' must not define map_path")
        if edge.vertical and source_floor and target_floor and source_floor == target_floor:
            raise ValueError(f"Edge '{edge.id}' is marked vertical but both endpoints are on '{source_floor}'")
        path_floor = edge.floor or source_floor
        for index, point in enumerate(edge.map_path):
            _validate_point(f"Edge '{edge.id}' map_path[{index}]", path_floor, point, floor_sizes)
        # A path without explicit endpoints is valid; renderer joins it to node markers.
        if edge.map_path and (source_xy is None or target_xy is None):
            logger.warning("Edge '%s' has map_path but one endpoint lacks map_xy", edge.id)

    return SiteGraph(
        site_id=str(raw.get("site", raw.get("site_id", "site"))),
        version=str(raw.get("version", "1")),
        timezone=str(raw.get("timezone", "UTC")),
        cameras=cameras,
        edges=edges,
        blind_nodes=blind_nodes,
        outside=outside,
        map=map_def,
        max_gap_s=float(raw.get("max_gap_s", params.max_gap_s)),
    )
