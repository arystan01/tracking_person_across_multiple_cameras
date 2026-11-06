"""Pure trajectory projection, distance, speed and reachability estimates."""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime

from javes_contracts import (
    CameraDef,
    Hypothesis,
    ProjectedTrajectoryPoint,
    RangeEstimate,
    ReachableNode,
    ReachableSegment,
    SiteGraph,
    TrajectoryPoint,
)

from .params import RoutingParams


def project_trajectory(
    camera: CameraDef,
    trajectory: list[TrajectoryPoint],
    *,
    max_error_m: float,
) -> tuple[list[ProjectedTrajectoryPoint], bool]:
    calibration = camera.calibration
    if calibration is None or calibration.error_m > max_error_m:
        return [], True
    width, height = calibration.frame_size_px
    matrix = calibration.homography
    projected: list[ProjectedTrajectoryPoint] = []
    for point in trajectory:
        if not (0.0 <= point.x_px <= width and 0.0 <= point.y_px <= height):
            continue
        denominator = (
            matrix[2][0] * point.x_px
            + matrix[2][1] * point.y_px
            + matrix[2][2]
        )
        if abs(denominator) < 1e-12:
            continue
        x = (
            matrix[0][0] * point.x_px
            + matrix[0][1] * point.y_px
            + matrix[0][2]
        ) / denominator
        y = (
            matrix[1][0] * point.x_px
            + matrix[1][1] * point.y_px
            + matrix[1][2]
        ) / denominator
        if not math.isfinite(x) or not math.isfinite(y):
            continue
        projected.append(
            ProjectedTrajectoryPoint(
                timestamp=point.timestamp,
                x=round(x, 4),
                y=round(y, 4),
                floor_id=calibration.floor_id,
                confidence=point.confidence,
                calibration_error_m=calibration.error_m,
            )
        )
    return projected, len(projected) != len(trajectory) or not bool(projected)


def _floor_scale(graph: SiteGraph, floor_id: str) -> float | None:
    if graph.map is None:
        return None
    floor = next((item for item in graph.map.floors if item.id == floor_id), None)
    return floor.m_per_px if floor is not None else None


def _polyline_length(points: list[tuple[float, float]]) -> float:
    return sum(
        math.hypot(right[0] - left[0], right[1] - left[1])
        for left, right in zip(points, points[1:])
    )


def edge_distance_m(edge_id: str, graph: SiteGraph) -> float | None:
    edge = next((item for item in graph.edges if item.id == edge_id), None)
    if edge is None:
        return None
    if edge.d_m is not None:
        return edge.d_m
    source_floor, source = graph.node_floor_xy(edge.source)
    target_floor, target = graph.node_floor_xy(edge.target)
    floor_id = edge.floor or source_floor
    if (
        source is None
        or target is None
        or floor_id is None
        or (target_floor is not None and target_floor != floor_id)
    ):
        return None
    scale = _floor_scale(graph, floor_id)
    if scale is None:
        return None
    return _polyline_length([source, *edge.map_path, target]) * scale


def _observed_segments(
    hypothesis: Hypothesis,
    graph: SiteGraph,
) -> list[tuple[float, float, float]]:
    segments: list[tuple[float, float, float]] = []
    for visit in hypothesis.visits:
        for left, right in zip(
            visit.projected_trajectory, visit.projected_trajectory[1:]
        ):
            if left.floor_id != right.floor_id:
                continue
            scale = _floor_scale(graph, left.floor_id)
            duration_s = (right.timestamp - left.timestamp).total_seconds()
            if scale is None or duration_s <= 0:
                continue
            distance_m = math.hypot(right.x - left.x, right.y - left.y) * scale
            error_m = left.calibration_error_m + right.calibration_error_m
            segments.append((distance_m, duration_s, error_m))
    return segments


def movement_ranges(
    hypothesis: Hypothesis | None,
    graph: SiteGraph,
    params: RoutingParams,
) -> tuple[RangeEstimate, RangeEstimate]:
    if hypothesis is None:
        return (
            RangeEstimate(
                minimum=0.0,
                maximum=params.max_speed,
                unit="m/s",
                source="FALLBACK",
            ),
            RangeEstimate(
                minimum=0.0,
                maximum=0.0,
                unit="m",
                source="FALLBACK",
            ),
        )
    observed = _observed_segments(hypothesis, graph)
    observed_distance = sum(item[0] for item in observed)
    observed_error = sum(item[2] for item in observed)
    inferred_distances = [
        (transition.geometry.distance_min_m + transition.geometry.distance_max_m)
        / 2.0
        for transition in hypothesis.transitions
        if transition.geometry is not None
    ]
    inferred_distance = sum(inferred_distances)
    total = observed_distance + inferred_distance
    distance_source = (
        "MIXED"
        if observed and inferred_distances
        else "OBSERVED"
        if observed
        else "INFERRED"
        if inferred_distances
        else "FALLBACK"
    )
    distance = RangeEstimate(
        minimum=round(max(total - observed_error, 0.0), 3),
        maximum=round(total + observed_error, 3),
        unit="m",
        source=distance_source,
    )

    speeds = [
        item[0] / item[1]
        for item in observed
        if 0.0 <= item[0] / item[1] <= params.max_speed
    ]
    if len(speeds) < 2:
        speed = RangeEstimate(
            minimum=0.0,
            maximum=params.max_speed,
            unit="m/s",
            source="FALLBACK",
        )
    else:
        ordered = sorted(speeds)
        low = ordered[max(int(len(ordered) * 0.1) - 1, 0)]
        high = ordered[min(int(len(ordered) * 0.9), len(ordered) - 1)]
        speed = RangeEstimate(
            minimum=round(max(low * 0.8, 0.0), 3),
            maximum=round(
                min(max(high * 1.2, low * 1.01), params.max_speed), 3
            ),
            unit="m/s",
            source="OBSERVED",
        )
    return speed, distance


def reachable_area(
    hypothesis: Hypothesis | None,
    graph: SiteGraph,
    params: RoutingParams,
    now: datetime,
) -> tuple[list[ReachableSegment], list[ReachableNode]]:
    if hypothesis is None or not hypothesis.visits:
        return [], []
    last = hypothesis.visits[-1]
    elapsed_s = max((now - last.t_out).total_seconds(), 0.0)
    budget_m = elapsed_s * params.max_speed
    node_probability: dict[str, float] = defaultdict(float)
    segment_probability: dict[str, float] = defaultdict(float)
    frontier = [(hypothesis.current_node, 0.0, 1.0, 0)]
    best_distance = {hypothesis.current_node: 0.0}
    max_depth = max(len(graph.get_node_ids()) * 2, 1)
    while frontier:
        node, spent_m, probability, depth = frontier.pop(0)
        outgoing = sorted(graph.get_edges_from(node), key=lambda item: item.id)
        viable = []
        for edge in outgoing:
            distance = edge_distance_m(edge.id, graph)
            if distance is None:
                distance = edge.t_min * params.max_speed
            if spent_m < budget_m - 1e-9:
                viable.append((edge, distance))
        if not viable or depth >= max_depth:
            node_probability[node] += probability
            continue
        share = probability / len(viable)
        for edge, distance in viable:
            segment_probability[edge.id] += share
            reached = spent_m + distance
            if reached > budget_m + 1e-9:
                node_probability[node] += share
                continue
            previous = best_distance.get(edge.target)
            if previous is None or reached + 1e-9 < previous:
                best_distance[edge.target] = reached
                frontier.append((edge.target, reached, share, depth + 1))
            else:
                node_probability[edge.target] += share

    node_total = sum(node_probability.values()) or 1.0
    segment_total = sum(segment_probability.values()) or 1.0
    segments = [
        ReachableSegment(
            edge_id=edge_id,
            source=edge.source,
            target=edge.target,
            probability=round(value / segment_total, 6),
        )
        for edge_id, value in sorted(
            segment_probability.items(), key=lambda item: (-item[1], item[0])
        )
        if (
            edge := next(
                (item for item in graph.edges if item.id == edge_id), None
            )
        )
        is not None
    ]
    nodes = [
        ReachableNode(node_id=node, probability=round(value / node_total, 6))
        for node, value in sorted(
            node_probability.items(), key=lambda item: (-item[1], item[0])
        )
    ]
    return segments, nodes
