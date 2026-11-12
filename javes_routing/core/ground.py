"""Pure pedestrian ground-plane geometry over an immutable walk graph."""
from __future__ import annotations

import math
from dataclasses import dataclass

from javes_contracts import (
    ProjectedTrajectoryPoint,
    SiteGraph,
    TransitionGeometry,
    WalkEdgeDef,
)

from .params import RoutingParams


@dataclass(frozen=True)
class WalkSnap:
    edge: WalkEdgeDef
    point: tuple[float, float]
    distance_m: float
    offset_from_source_m: float
    offset_to_target_m: float
    points_to_source: tuple[tuple[float, float], ...]
    points_to_target: tuple[tuple[float, float], ...]


def floor_scale(graph: SiteGraph, floor_id: str) -> float | None:
    if graph.map is None:
        return None
    floor = next((item for item in graph.map.floors if item.id == floor_id), None)
    return floor.m_per_px if floor is not None else None


def polyline_length(points: list[tuple[float, float]]) -> float:
    return sum(
        math.hypot(right[0] - left[0], right[1] - left[1])
        for left, right in zip(points, points[1:])
    )


def deduplicate_points(
    points: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    return [
        point
        for index, point in enumerate(points)
        if index == 0 or point != points[index - 1]
    ]


def walk_edge_points(edge: WalkEdgeDef, graph: SiteGraph) -> list[tuple[float, float]]:
    if graph.walk_graph is None:
        return []
    source = graph.walk_graph.nodes.get(edge.source)
    target = graph.walk_graph.nodes.get(edge.target)
    if source is None or target is None:
        return []
    return deduplicate_points([source.map_xy, *edge.map_path, target.map_xy])


def walk_edge_distance_m(edge: WalkEdgeDef, graph: SiteGraph) -> float | None:
    if edge.d_m is not None:
        return edge.d_m
    scale = floor_scale(graph, edge.floor_id)
    points = walk_edge_points(edge, graph)
    if scale is None or len(points) < 2:
        return None
    return polyline_length(points) * scale


def nearest_walk_node(
    point: ProjectedTrajectoryPoint,
    graph: SiteGraph,
    max_distance_m: float,
) -> tuple[str, float] | None:
    if graph.walk_graph is None:
        return None
    scale = floor_scale(graph, point.floor_id)
    if scale is None:
        return None
    candidates = [
        (
            node.id,
            math.hypot(node.map_xy[0] - point.x, node.map_xy[1] - point.y) * scale,
        )
        for node in graph.walk_graph.nodes.values()
        if node.floor_id == point.floor_id
    ]
    if not candidates:
        return None
    result = min(candidates, key=lambda item: (item[1], item[0]))
    return result if result[1] <= max_distance_m else None


def nearest_walk_position(
    point: ProjectedTrajectoryPoint,
    graph: SiteGraph,
    max_distance_m: float,
) -> WalkSnap | None:
    """Snap a ground point to pedestrian geometry without losing edge direction."""
    if graph.walk_graph is None:
        return None
    scale = floor_scale(graph, point.floor_id)
    if scale is None:
        return None
    candidates: list[WalkSnap] = []
    for edge in graph.walk_graph.edges:
        if edge.floor_id != point.floor_id:
            continue
        points = walk_edge_points(edge, graph)
        if len(points) < 2:
            continue
        segment_lengths = [
            math.hypot(right[0] - left[0], right[1] - left[1])
            for left, right in zip(points, points[1:])
        ]
        total_px = sum(segment_lengths)
        travelled_px = 0.0
        for index, (left, right, segment_length) in enumerate(
            zip(points, points[1:], segment_lengths)
        ):
            if segment_length <= 1e-12:
                continue
            dx = right[0] - left[0]
            dy = right[1] - left[1]
            ratio = min(
                max(
                    (
                        (point.x - left[0]) * dx
                        + (point.y - left[1]) * dy
                    )
                    / (segment_length**2),
                    0.0,
                ),
                1.0,
            )
            snapped = (left[0] + dx * ratio, left[1] + dy * ratio)
            distance_m = math.hypot(
                point.x - snapped[0], point.y - snapped[1]
            ) * scale
            offset_px = travelled_px + segment_length * ratio
            candidates.append(
                WalkSnap(
                    edge=edge,
                    point=snapped,
                    distance_m=distance_m,
                    offset_from_source_m=offset_px * scale,
                    offset_to_target_m=(total_px - offset_px) * scale,
                    points_to_source=tuple(
                        deduplicate_points([snapped, *reversed(points[: index + 1])])
                    ),
                    points_to_target=tuple(
                        deduplicate_points([snapped, *points[index + 1 :]])
                    ),
                )
            )
            travelled_px += segment_length
    if not candidates:
        return None
    result = min(
        candidates,
        key=lambda item: (item.distance_m, item.edge.id, item.point),
    )
    return result if result.distance_m <= max_distance_m else None


def point_to_polyline_distance_m(
    point: tuple[float, float],
    points: list[tuple[float, float]],
    scale: float,
) -> float | None:
    if len(points) < 2:
        return None
    distances = []
    for left, right in zip(points, points[1:]):
        dx = right[0] - left[0]
        dy = right[1] - left[1]
        length_sq = dx * dx + dy * dy
        if length_sq <= 1e-12:
            distances.append(math.hypot(point[0] - left[0], point[1] - left[1]))
            continue
        ratio = min(
            max(
                ((point[0] - left[0]) * dx + (point[1] - left[1]) * dy)
                / length_sq,
                0.0,
            ),
            1.0,
        )
        projected = (left[0] + dx * ratio, left[1] + dy * ratio)
        distances.append(
            math.hypot(point[0] - projected[0], point[1] - projected[1])
        )
    return min(distances) * scale


def _simple_paths(
    source: str,
    target: str,
    graph: SiteGraph,
    max_edges: int,
) -> list[list[WalkEdgeDef]]:
    if graph.walk_graph is None:
        return []
    if source == target:
        return [[]]
    paths: list[list[WalkEdgeDef]] = []

    def walk(node: str, visited: tuple[str, ...], path: list[WalkEdgeDef]) -> None:
        if len(path) >= max_edges:
            return
        for edge in sorted(
            graph.walk_graph.get_edges_from(node), key=lambda item: item.id
        ):
            if edge.target == target:
                paths.append([*path, edge])
                continue
            if edge.target in visited:
                continue
            walk(edge.target, (*visited, edge.target), [*path, edge])

    walk(source, (source,), [])
    return paths


def _path_points(
    edges: list[WalkEdgeDef],
    graph: SiteGraph,
) -> list[tuple[float, float]]:
    """Return only site-authorised pedestrian geometry.

    Observation-to-graph connectors are deliberately excluded.  A straight
    connector may cross a wall or a shelf when the observed point and nearest
    graph node happen to be on opposite sides of an obstacle.
    """
    points: list[tuple[float, float]] = []
    for edge in edges:
        points.extend(walk_edge_points(edge, graph))
    return deduplicate_points(points)


def _path_distance_m(
    edges: list[WalkEdgeDef],
    graph: SiteGraph,
) -> float | None:
    distances = [walk_edge_distance_m(edge, graph) for edge in edges]
    if any(distance is None for distance in distances):
        return None
    return sum(distance for distance in distances if distance is not None)


def walk_edge_by_id(edge_id: str, graph: SiteGraph) -> WalkEdgeDef | None:
    """Resolve a canonical or generated reverse walk-edge id."""
    if graph.walk_graph is None:
        return None
    reverse = edge_id.endswith("::reverse")
    base_id = edge_id.removesuffix("::reverse") if reverse else edge_id
    edge = next(
        (item for item in graph.walk_graph.edges if item.id == base_id),
        None,
    )
    if edge is None or (reverse and not edge.bidirectional):
        return None
    if not reverse:
        return edge
    return edge.model_copy(
        update={
            "id": edge_id,
            "source": edge.target,
            "target": edge.source,
            "map_path": list(reversed(edge.map_path)),
            "bidirectional": False,
        }
    )


def configured_walk_path(
    edge_ids: list[str],
    graph: SiteGraph,
) -> list[tuple[float, float]]:
    """Rebuild a contiguous path solely from immutable Site Config edges."""
    if not edge_ids:
        return []
    edges = [walk_edge_by_id(edge_id, graph) for edge_id in edge_ids]
    if any(edge is None for edge in edges):
        return []
    resolved = [edge for edge in edges if edge is not None]
    if any(
        left.target != right.source or left.floor_id != right.floor_id
        for left, right in zip(resolved, resolved[1:])
    ):
        return []
    return _path_points(resolved, graph)


def build_transition_geometry(
    start: ProjectedTrajectoryPoint,
    end: ProjectedTrajectoryPoint,
    graph: SiteGraph,
    params: RoutingParams,
) -> TransitionGeometry | None:
    """Return the shortest directed path made only of Site Config walk edges."""
    if start.floor_id != end.floor_id or graph.walk_graph is None:
        return None
    source_snap = nearest_walk_node(start, graph, params.max_walk_snap_m)
    target_snap = nearest_walk_node(end, graph, params.max_walk_snap_m)
    if source_snap is None or target_snap is None:
        return None
    source_node, source_distance = source_snap
    target_node, target_distance = target_snap
    candidates = []
    for edges in _simple_paths(
        source_node, target_node, graph, params.max_hidden_edges
    ):
        distance = _path_distance_m(edges, graph)
        if distance is None:
            continue
        points = _path_points(edges, graph)
        if len(points) < 2:
            continue
        candidates.append(
            (
                distance,
                tuple(edge.id for edge in edges),
                points,
            )
        )
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    best_distance, best_edge_ids, best_points = candidates[0]
    uncertainty = start.calibration_error_m + end.calibration_error_m
    return TransitionGeometry(
        floor_id=start.floor_id,
        points=best_points,
        walk_edge_ids=list(best_edge_ids),
        alternative_paths=[
            points
            for _, _, points in candidates[1 : 1 + params.walk_alternative_limit]
        ],
        alternative_walk_edge_paths=[
            list(edge_ids)
            for _, edge_ids, _ in candidates[1 : 1 + params.walk_alternative_limit]
        ],
        start_offset_m=round(source_distance, 3),
        end_offset_m=round(target_distance, 3),
        distance_min_m=round(
            max(best_distance - source_distance - target_distance - uncertainty, 0.0),
            3,
        ),
        distance_max_m=round(
            best_distance + source_distance + target_distance + uncertainty,
            3,
        ),
        approximate=(source_distance > 0.1 or target_distance > 0.1),
        source="WALK_GRAPH",
    )


def truncate_polyline(
    points: list[tuple[float, float]],
    distance_m: float,
    scale_m_per_px: float,
) -> list[tuple[float, float]]:
    """Return a prefix ending exactly at the metric travel budget."""
    if not points:
        return []
    if distance_m <= 0:
        return [points[0]]
    result = [points[0]]
    remaining = distance_m
    for left, right in zip(points, points[1:]):
        segment_px = math.hypot(right[0] - left[0], right[1] - left[1])
        segment_m = segment_px * scale_m_per_px
        if segment_m <= remaining + 1e-12:
            result.append(right)
            remaining -= segment_m
            continue
        ratio = remaining / segment_m if segment_m > 0 else 0.0
        result.append(
            (
                left[0] + (right[0] - left[0]) * ratio,
                left[1] + (right[1] - left[1]) * ratio,
            )
        )
        break
    return deduplicate_points(result)
