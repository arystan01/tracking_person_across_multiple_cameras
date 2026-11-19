"""Pure trajectory projection, distance, speed and reachability estimates."""
from __future__ import annotations

import math
from collections import defaultdict, deque
from datetime import datetime

from javes_contracts import (
    CameraDef,
    Hypothesis,
    ProjectedTrajectoryPoint,
    RangeEstimate,
    ReachableNode,
    ReachableSegment,
    SearchState,
    SiteGraph,
    TrajectoryPoint,
)

from .params import RoutingParams
from .ground import (
    floor_scale,
    nearest_walk_position,
    truncate_polyline,
    walk_edge_distance_m,
    walk_edge_points,
)


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
    if hypothesis.observed_segments:
        for observed in hypothesis.observed_segments:
            for left, right in zip(observed.points, observed.points[1:]):
                if left.floor_id != right.floor_id:
                    continue
                scale = _floor_scale(graph, left.floor_id)
                duration_s = (right.timestamp - left.timestamp).total_seconds()
                if scale is None or duration_s <= 0:
                    continue
                distance_m = math.hypot(right.x - left.x, right.y - left.y) * scale
                segments.append((distance_m, duration_s, left.error_m + right.error_m))
        return segments
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
        for transition in [*hypothesis.transitions, *hypothesis.observed_gaps]
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


def last_ground_point(
    hypothesis: Hypothesis | None,
) -> ProjectedTrajectoryPoint | None:
    if hypothesis is None or not hypothesis.visits:
        return None
    if hypothesis.observed_segments and hypothesis.observed_segments[-1].points:
        ground = hypothesis.observed_segments[-1].points[-1]
        return ProjectedTrajectoryPoint(
            timestamp=ground.timestamp,
            x=ground.x,
            y=ground.y,
            floor_id=ground.floor_id,
            confidence=ground.confidence,
            calibration_error_m=ground.error_m,
        )
    projected = [
        point
        for visit in hypothesis.visits
        for point in visit.projected_trajectory
    ]
    return (
        max(projected, key=lambda item: (item.timestamp, item.x, item.y))
        if projected
        else None
    )


def _last_heading(
    hypothesis: Hypothesis,
) -> tuple[float, float] | None:
    points: list[ProjectedTrajectoryPoint] = []
    if hypothesis.observed_segments:
        for point in hypothesis.observed_segments[-1].points:
            points.append(
                ProjectedTrajectoryPoint(
                    timestamp=point.timestamp,
                    x=point.x,
                    y=point.y,
                    floor_id=point.floor_id,
                    confidence=point.confidence,
                    calibration_error_m=point.error_m,
                )
            )
    else:
        points = [
            point
            for visit in hypothesis.visits
            for point in visit.projected_trajectory
        ]
    if len(points) < 2:
        return None
    end = points[-1]
    for start in reversed(points[:-1]):
        if start.floor_id != end.floor_id:
            continue
        vector = (end.x - start.x, end.y - start.y)
        length = math.hypot(*vector)
        if length > 1e-9:
            return (vector[0] / length, vector[1] / length)
    return None


def _direction_weight(
    points: list[tuple[float, float]],
    heading: tuple[float, float] | None,
    kappa: float,
) -> float:
    if heading is None or kappa <= 0.0 or len(points) < 2:
        return 1.0
    for left, right in zip(points, points[1:]):
        vector = (right[0] - left[0], right[1] - left[1])
        length = math.hypot(*vector)
        if length > 1e-9:
            cosine = max(
                min((vector[0] * heading[0] + vector[1] * heading[1]) / length, 1.0),
                -1.0,
            )
            return math.exp(kappa * cosine)
    return 1.0


def _point_in_polygon(
    point: tuple[float, float],
    polygon: list[tuple[float, float]],
) -> bool:
    x, y = point
    inside = False
    previous = polygon[-1]
    for current in polygon:
        x1, y1 = previous
        x2, y2 = current
        if (y1 > y) != (y2 > y):
            crossing = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x <= crossing:
                inside = not inside
        previous = current
    return inside


def _empty_probe_factor(
    target: str,
    graph: SiteGraph,
    state: SearchState | None,
    after: datetime,
    params: RoutingParams,
) -> float:
    """Down-weight a branch only when its camera coverage was fully checked empty."""
    if state is None or graph.walk_graph is None:
        return 1.0
    node = graph.walk_graph.nodes.get(target)
    if node is None:
        return 1.0
    covering = [
        camera
        for camera in graph.cameras.values()
        if camera.floor == node.floor_id
        and camera.coverage_polygon
        and _point_in_polygon(node.map_xy, camera.coverage_polygon)
    ]
    for camera in covering:
        if any(
            window.camera == camera.id
            and window.status.value == "DONE"
            and window.coverage_ratio >= 1.0
            and not window.found
            and (window.t_to is None or window.t_to >= after)
            for window in state.probed_windows
        ):
            return max(1.0 - (camera.p_det or params.p_det), 0.0)
    return 1.0


def reachable_area(
    hypothesis: Hypothesis | None,
    graph: SiteGraph,
    params: RoutingParams,
    now: datetime,
    state: SearchState | None = None,
) -> tuple[list[ReachableSegment], list[ReachableNode]]:
    if hypothesis is None or not hypothesis.visits or graph.walk_graph is None:
        return [], []
    last_point = last_ground_point(hypothesis)
    if last_point is None:
        return [], []
    scale = floor_scale(graph, last_point.floor_id)
    snap = nearest_walk_position(last_point, graph, params.max_walk_snap_m)
    if scale is None or snap is None:
        return [], []
    elapsed_s = max((now - last_point.timestamp).total_seconds(), 0.0)
    budget_m = elapsed_s * params.max_speed
    if budget_m <= 1e-9:
        return [], []

    node_probability: dict[str, float] = defaultdict(float)
    segment_data: dict[str, dict[str, object]] = {}

    def record_segment(
        edge,
        points: list[tuple[float, float]],
        distance_m: float,
        available_m: float,
        probability: float,
        *,
        approximate: bool,
    ) -> bool:
        if distance_m <= 1e-9 or len(points) < 2:
            return True
        progress = min(max(available_m / distance_m, 0.0), 1.0)
        visible = (
            points
            if progress >= 1.0 - 1e-9
            else truncate_polyline(points, available_m, scale)
        )
        previous = segment_data.get(edge.id)
        if previous is None:
            segment_data[edge.id] = {
                "edge": edge,
                "probability": probability,
                "points": visible,
                "progress": progress,
                "approximate": approximate,
            }
        else:
            previous["probability"] = min(
                float(previous["probability"]) + probability, 1.0
            )
            if progress > float(previous["progress"]):
                previous["points"] = visible
                previous["progress"] = progress
        return progress >= 1.0 - 1e-9

    # A state is a graph node reached at a physical time bin.  Distance always
    # increases, so cycles are finite and cannot create probability mass.
    frontier: deque[tuple[str, float, float, str | None, int]] = deque()
    heading = _last_heading(hypothesis)
    endpoint_tolerance_m = 1e-6
    if snap.offset_from_source_m <= endpoint_tolerance_m:
        frontier.append((snap.edge.source, 0.0, 1.0, None, 0))
    elif snap.offset_to_target_m <= endpoint_tolerance_m:
        frontier.append((snap.edge.target, 0.0, 1.0, None, 0))
    else:
        directions = [
            (
                snap.edge,
                list(snap.points_to_target),
                snap.offset_to_target_m,
                snap.edge.target,
            )
        ]
        if snap.edge.bidirectional:
            directions.append(
                (
                    snap.edge.model_copy(
                        update={
                            "id": f"{snap.edge.id}::reverse",
                            "source": snap.edge.target,
                            "target": snap.edge.source,
                            "map_path": list(reversed(snap.edge.map_path)),
                            "bidirectional": False,
                        }
                    ),
                    list(snap.points_to_source),
                    snap.offset_from_source_m,
                    snap.edge.source,
                )
            )
        weighted = [
            max(edge.branch_prior or 1.0, params.reachable_probability_epsilon)
            * _direction_weight(points, heading, params.reachable_heading_kappa)
            * _empty_probe_factor(target, graph, state, last_point.timestamp, params)
            for edge, points, _, target in directions
        ]
        total_weight = sum(weighted) or 1.0
        for (edge, points, distance_m, target), weight in zip(directions, weighted):
            probability = weight / total_weight
            reached = record_segment(
                edge,
                points,
                distance_m,
                budget_m,
                probability,
                approximate=snap.distance_m > 0.1,
            )
            if reached:
                frontier.append((target, distance_m, probability, snap.edge.id, 1))
            else:
                node_probability[edge.source] += probability

    visited: set[tuple[str, int, str | None]] = set()
    expanded = 0
    while frontier and expanded < params.reachable_max_states:
        node, spent_m, probability, incoming_edge, depth = frontier.popleft()
        time_bin = int(
            math.floor(
                spent_m
                / max(params.max_speed * params.reachable_time_step_s, 1e-9)
                + 1e-9
            )
        )
        state_key = (node, time_bin, incoming_edge)
        if state_key in visited:
            node_probability[node] += probability
            continue
        visited.add(state_key)
        expanded += 1
        if spent_m >= budget_m - 1e-9:
            node_probability[node] += probability
            continue
        outgoing = sorted(
            graph.walk_graph.get_edges_from(node), key=lambda item: item.id
        )
        viable = [
            (edge, distance)
            for edge in outgoing
            if (distance := walk_edge_distance_m(edge, graph)) is not None
            and edge.id.removesuffix("::reverse") != incoming_edge
        ]
        if not viable:
            node_probability[node] += probability
            continue
        weights = [
            max(edge.branch_prior or 1.0, params.reachable_probability_epsilon)
            * (
                _direction_weight(
                    walk_edge_points(edge, graph),
                    heading,
                    params.reachable_heading_kappa,
                )
                if depth == 0
                else 1.0
            )
            * _empty_probe_factor(
                edge.target, graph, state, last_point.timestamp, params
            )
            for edge, _ in viable
        ]
        weight_total = sum(weights) or 1.0
        for (edge, distance), weight in zip(viable, weights):
            share = probability * weight / weight_total
            if share < params.reachable_probability_epsilon:
                node_probability[node] += share
                continue
            edge_points = walk_edge_points(edge, graph)
            available_m = max(budget_m - spent_m, 0.0)
            reached = record_segment(
                edge,
                edge_points,
                distance,
                available_m,
                share,
                approximate=False,
            )
            if not reached:
                node_probability[node] += share
                continue
            frontier.append(
                (
                    edge.target,
                    spent_m + distance,
                    share,
                    edge.id.removesuffix("::reverse"),
                    depth + 1,
                )
            )

    for node, _, probability, _, _ in frontier:
        node_probability[node] += probability

    canonical_node_probability: dict[str, float] = defaultdict(float)
    for node_id, probability in node_probability.items():
        node = graph.walk_graph.nodes.get(node_id)
        canonical_id = (
            "OUTSIDE"
            if node is not None and node.kind == "OUTSIDE"
            else node_id
        )
        canonical_node_probability[canonical_id] += probability

    node_total = sum(canonical_node_probability.values()) or 1.0
    segments = [
        ReachableSegment(
            edge_id=edge_id,
            source=data["edge"].source,  # type: ignore[union-attr]
            target=data["edge"].target,  # type: ignore[union-attr]
            probability=round(float(data["probability"]), 6),
            floor_id=data["edge"].floor_id,  # type: ignore[union-attr]
            points=data["points"],  # type: ignore[arg-type]
            width_m=data["edge"].width_m,  # type: ignore[union-attr]
            progress=round(float(data["progress"]), 6),
            approximate=bool(data["approximate"]),
        )
        for edge_id, data in sorted(
            segment_data.items(),
            key=lambda item: (-float(item[1]["probability"]), item[0]),
        )
    ]
    nodes = [
        ReachableNode(node_id=node, probability=round(value / node_total, 12))
        for node, value in sorted(
            canonical_node_probability.items(), key=lambda item: (-item[1], item[0])
        )
    ]
    return segments, nodes
