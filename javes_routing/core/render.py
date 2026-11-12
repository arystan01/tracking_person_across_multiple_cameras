"""Pure, deterministic SVG rendering owned by Routing."""
from __future__ import annotations

import base64
import math
from datetime import datetime, timezone
from html import escape

from javes_contracts import (
    GroundPosition,
    Hypothesis,
    PositionState,
    RenderedFloorMap,
    RenderedRouteBundle,
    RouteLayer,
    RoutePresentation,
    SiteGraph,
    StepResult,
    StopReason,
)

from .ground import configured_walk_path, floor_scale


def _mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    raise ValueError("floor assets must be PNG or JPEG raster images")


def _best(result: StepResult) -> Hypothesis | None:
    if isinstance(result.best, Hypothesis):
        return result.best
    if isinstance(result.best, str):
        return next((item for item in result.hypotheses if item.id == result.best), None)
    eligible = [item for item in result.hypotheses if not item.requires_confirmation]
    pool = eligible or result.hypotheses
    return min(pool, key=lambda item: (-item.score, item.id)) if pool else None


def _last_observation(
    hypothesis: Hypothesis | None,
) -> tuple[GroundPosition | None, list[str], bool, bool]:
    if hypothesis is None:
        return None, [], False, False
    segments = [item for item in hypothesis.observed_segments if item.points]
    if segments:
        segment = max(segments, key=lambda item: item.points[-1].timestamp)
        point = max(segment.points, key=lambda item: item.timestamp)
        latest_time = max(
            (visit.t_out for visit in hypothesis.visits),
            default=point.timestamp,
        )
        latest_visits = [
            visit for visit in hypothesis.visits if visit.t_out == latest_time
        ]
        return (
            point,
            list(segment.camera_ids or point.source_camera_ids),
            any(visit.clipped_out for visit in latest_visits),
            any(visit.open for visit in latest_visits),
        )
    candidates = [
        (visit, point)
        for visit in hypothesis.visits
        for point in visit.projected_trajectory
    ]
    if not candidates:
        return None, [], False, False
    visit, point = max(
        candidates,
        key=lambda item: (item[1].timestamp, item[0].camera),
    )
    return (
        GroundPosition(
            timestamp=point.timestamp,
            x=point.x,
            y=point.y,
            floor_id=point.floor_id,
            error_m=point.calibration_error_m,
            confidence=point.confidence,
            source_camera_ids=[visit.camera],
            source_tracklet_ids=list(visit.tracklet_ids),
        ),
        [visit.camera],
        visit.clipped_out,
        visit.open,
    )


def _presentation(
    result: StepResult,
    best: Hypothesis | None,
) -> RoutePresentation:
    point, cameras, visible_at_end, open_observation = _last_observation(best)
    has_search_area = bool(
        result.estimate and result.estimate.reachable_segments
    )
    if point is None:
        state = PositionState.UNKNOWN
    elif open_observation:
        state = PositionState.OBSERVED
    elif has_search_area:
        state = PositionState.LOST
    elif (
        not result.state.done
        and result.estimate is not None
        and result.estimate.reachable_method_version is not None
    ):
        state = PositionState.UNKNOWN
    else:
        state = PositionState.LAST_SEEN
    return RoutePresentation(
        position_state=state,
        last_confirmed_position=point,
        source_camera_ids=cameras,
        visible_at_interval_end=visible_at_end,
        search_area_as_of=(
            result.estimate.calculated_at
            if has_search_area and result.estimate is not None
            else None
        ),
    )


def _layout(
    graph: SiteGraph,
    floor_id: str,
    width: int,
    height: int,
) -> dict[str, tuple[float, float]]:
    nodes: list[tuple[str, tuple[float, float] | None]] = []
    for camera in sorted(graph.cameras.values(), key=lambda item: item.id):
        if (camera.floor or "ABSTRACT") == floor_id:
            nodes.append((camera.id, camera.map_xy))
    for node in sorted(graph.blind_nodes, key=lambda item: item.id):
        if (node.floor or "ABSTRACT") == floor_id:
            nodes.append((node.id, node.map_xy))
    positions = {node: point for node, point in nodes if point is not None}
    missing = [node for node, point in nodes if point is None]
    radius = max(min(width, height) * 0.34, 30.0)
    cx, cy = width / 2.0, height / 2.0
    count = max(len(missing), 1)
    for index, node in enumerate(missing):
        angle = -math.pi / 2 + 2 * math.pi * index / count
        positions[node] = (cx + radius * math.cos(angle), cy + radius * math.sin(angle))
    return positions


def _point(value: tuple[float, float]) -> str:
    return f"{value[0]:.2f},{value[1]:.2f}"


def _time_label(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _edge_points(
    graph: SiteGraph,
    edge_id: str,
    floor_id: str,
    positions: dict[str, tuple[float, float]],
) -> list[tuple[float, float]]:
    edge = next((item for item in graph.edges if item.id == edge_id), None)
    if edge is None:
        return []
    source_floor, source = graph.node_floor_xy(edge.source)
    target_floor, target = graph.node_floor_xy(edge.target)
    source_floor = source_floor or "ABSTRACT"
    target_floor = target_floor or "ABSTRACT"
    source = source or positions.get(edge.source)
    target = target or positions.get(edge.target)
    if source_floor == target_floor == floor_id and source and target:
        points = [source, *edge.map_path, target]
        return [point for index, point in enumerate(points) if index == 0 or point != points[index - 1]]
    if source_floor == floor_id and source:
        return [source]
    if target_floor == floor_id and target:
        return [target]
    return []


def _endpoint_for_visit(
    hypothesis: Hypothesis,
    visit_index: int,
    floor_id: str,
    *,
    first: bool,
) -> tuple[float, float, float] | None:
    visit = hypothesis.visits[visit_index]
    points = [
        point for point in visit.projected_trajectory if point.floor_id == floor_id
    ]
    if not points:
        return None
    point = points[0] if first else points[-1]
    return point.x, point.y, point.calibration_error_m


def _geometry_is_continuous(
    hypothesis: Hypothesis,
    visit_index: int,
    graph: SiteGraph,
) -> bool:
    transition = hypothesis.transitions[visit_index - 1]
    geometry = transition.geometry
    if geometry is None or geometry.source != "WALK_GRAPH":
        return False
    configured = configured_walk_path(geometry.walk_edge_ids, graph)
    if len(configured) < 2:
        return False
    start = _endpoint_for_visit(
        hypothesis, visit_index - 1, geometry.floor_id, first=False
    )
    end = _endpoint_for_visit(hypothesis, visit_index, geometry.floor_id, first=True)
    if start is None or end is None:
        return False
    floor = next(
        (
            item
            for item in (graph.map.floors if graph.map is not None else [])
            if item.id == geometry.floor_id
        ),
        None,
    )
    scale = floor.m_per_px if floor is not None else None
    if scale is None:
        return False
    start_distance_m = math.hypot(
        configured[0][0] - start[0], configured[0][1] - start[1]
    ) * scale
    end_distance_m = math.hypot(
        configured[-1][0] - end[0], configured[-1][1] - end[1]
    ) * scale
    tolerance_m = 0.02
    return (
        start_distance_m <= geometry.start_offset_m + tolerance_m
        and end_distance_m <= geometry.end_offset_m + tolerance_m
    )


def _route_elements(
    hypothesis: Hypothesis,
    graph: SiteGraph,
    floor_id: str,
    positions: dict[str, tuple[float, float]],
    role: str,
    dashed: bool,
) -> list[str]:
    elements: list[str] = []
    colour = "#d97706"
    width = "3" if dashed else "5"
    for index in range(1, len(hypothesis.visits)):
        previous, current = hypothesis.visits[index - 1], hypothesis.visits[index]
        transition = hypothesis.transitions[index - 1] if index - 1 < len(hypothesis.transitions) else None
        pieces: list[tuple[list[tuple[float, float]], bool, str]] = []
        if (
            transition is not None
            and transition.geometry is not None
            and transition.geometry.floor_id == floor_id
            and _geometry_is_continuous(hypothesis, index, graph)
        ):
            configured = configured_walk_path(
                transition.geometry.walk_edge_ids, graph
            )
            pieces = [
                (
                    configured,
                    transition.geometry.approximate,
                    ",".join(transition.geometry.walk_edge_ids),
                )
            ]
        for points, approximate, edge_label in pieces:
            dash = ' stroke-dasharray="10 7"'
            if len(points) >= 2:
                elements.append(
                    f'<polyline data-role="{role}" data-edge="{escape(edge_label)}" '
                    f'data-kind="{escape(transition.kind if transition else "UNKNOWN")}" '
                    f'data-source="{escape(transition.geometry.source if transition and transition.geometry else "UNKNOWN")}" '
                    f'data-from="{escape(previous.t_out.isoformat())}" '
                    f'data-to="{escape(current.t_in.isoformat())}" '
                    f'data-approximate="{str(approximate).lower()}" points="{" ".join(_point(item) for item in points)}" '
                    f'fill="none" stroke="{colour}" stroke-width="{width}" stroke-linecap="round" '
                    f'stroke-linejoin="round"{dash}/>'
                )
            elif points:
                label = "exit" if graph.node_floor_xy(previous.camera)[0] == floor_id else "entry"
                x, y = points[0]
                elements.append(
                    f'<g data-role="inter-floor"><circle cx="{x:.2f}" cy="{y:.2f}" r="9" fill="{colour}"/>'
                    f'<text x="{x + 12:.2f}" y="{y - 12:.2f}">{escape(label)}</text></g>'
                )
        if (
            transition
            and transition.geometry
            and transition.geometry.floor_id == floor_id
            and _geometry_is_continuous(hypothesis, index, graph)
        ):
            for alternative_index, edge_ids in enumerate(
                transition.geometry.alternative_walk_edge_paths
            ):
                points = configured_walk_path(edge_ids, graph)
                if len(points) >= 2:
                    elements.append(
                        f'<polyline data-role="alternative-path" data-edge="alternative-{alternative_index}" '
                        f'points="{" ".join(_point(item) for item in points)}" fill="none" '
                        f'stroke="#f59e0b" stroke-width="3" stroke-dasharray="5 7"/>'
                    )
        flags = set(current.flags) | (set(transition.flags) if transition else set())
        for flag in sorted(flags & {"LONG_GAP", "GATE_OVERRIDE", "UNKNOWN"}):
            point = positions.get(current.camera)
            if point:
                x, y = point
                elements.append(
                    f'<text data-role="gap-label" x="{x + 11:.2f}" y="{y + 24:.2f}" '
                    f'fill="#b91c1c">{escape(flag)}</text>'
                )
    for transition in hypothesis.observed_gaps:
        geometry = transition.geometry
        if (
            geometry is None
            or geometry.floor_id != floor_id
            or geometry.source != "WALK_GRAPH"
        ):
            continue
        configured = configured_walk_path(geometry.walk_edge_ids, graph)
        if len(configured) < 2:
            continue
        timestamp_from = transition.t_from.isoformat() if transition.t_from else ""
        timestamp_to = transition.t_to.isoformat() if transition.t_to else ""
        elements.append(
            f'<polyline data-role="{role}" data-edge="{escape(",".join(geometry.walk_edge_ids))}" '
            f'data-kind="GAP" data-source="{escape(geometry.source)}" '
            f'data-from="{escape(timestamp_from)}" data-to="{escape(timestamp_to)}" '
            f'data-approximate="true" points="{" ".join(_point(item) for item in configured)}" '
            f'fill="none" stroke="{colour}" stroke-width="{width}" stroke-linecap="round" '
            'stroke-linejoin="round" stroke-dasharray="10 7"/>'
        )
    return elements


def _observed_elements(
    hypothesis: Hypothesis,
    floor_id: str,
) -> list[str]:
    elements: list[str] = []
    if hypothesis.observed_segments:
        for segment in hypothesis.observed_segments:
            points = [point for point in segment.points if point.floor_id == floor_id]
            if len(points) >= 2:
                elements.append(
                    f'<polyline data-role="observed-path" data-camera="{escape(",".join(segment.camera_ids))}" '
                    f'data-segment="{escape(segment.id)}" '
                    f'data-from="{escape(points[0].timestamp.isoformat())}" '
                    f'data-to="{escape(points[-1].timestamp.isoformat())}" '
                    f'data-approximate="{str(segment.approximate).lower()}" '
                    f'points="{" ".join(_point((point.x, point.y)) for point in points)}" '
                    'fill="none" stroke="#0891b2" stroke-width="6" stroke-linecap="round" '
                    'stroke-linejoin="round"/>'
                )
            elif points:
                point = points[0]
                elements.append(
                    f'<circle data-role="observed-path" data-camera="{escape(",".join(segment.camera_ids))}" '
                    f'data-segment="{escape(segment.id)}" '
                    f'data-from="{escape(point.timestamp.isoformat())}" '
                    f'data-to="{escape(point.timestamp.isoformat())}" '
                    f'data-approximate="{str(segment.approximate).lower()}" '
                    f'cx="{point.x:.2f}" cy="{point.y:.2f}" r="5" fill="#0891b2"/>'
                )
        return elements
    for visit in hypothesis.visits:
        points = [
            point
            for point in visit.projected_trajectory
            if point.floor_id == floor_id
        ]
        if len(points) >= 2:
            elements.append(
                f'<polyline data-role="observed-path" data-camera="{escape(visit.camera)}" '
                f'data-from="{escape(points[0].timestamp.isoformat())}" '
                f'data-to="{escape(points[-1].timestamp.isoformat())}" '
                f'data-approximate="{str(visit.trajectory_approximate).lower()}" '
                f'points="{" ".join(_point((point.x, point.y)) for point in points)}" '
                'fill="none" stroke="#0891b2" stroke-width="6" stroke-linecap="round" '
                'stroke-linejoin="round"/>'
            )
        elif points:
            point = points[0]
            elements.append(
                f'<circle data-role="observed-path" data-camera="{escape(visit.camera)}" '
                f'data-from="{escape(point.timestamp.isoformat())}" '
                f'data-to="{escape(point.timestamp.isoformat())}" '
                f'data-approximate="{str(visit.trajectory_approximate).lower()}" '
                f'cx="{point.x:.2f}" cy="{point.y:.2f}" r="5" fill="#0891b2"/>'
            )
    return elements


def _reachable_elements(
    step_result: StepResult,
    graph: SiteGraph,
    floor_id: str,
    positions: dict[str, tuple[float, float]],
) -> list[str]:
    if step_result.estimate is None:
        return []
    elements: list[str] = []
    scale = floor_scale(graph, floor_id)
    for segment in step_result.estimate.reachable_segments:
        points = segment.points if segment.floor_id == floor_id else []
        if len(points) < 2:
            continue
        stroke_width = max(segment.width_m / scale, 2.0) if scale else 18.0
        elements.append(
            f'<polyline data-role="reachable-area" data-edge="{escape(segment.edge_id)}" '
            f'data-progress="{segment.progress:.6f}" '
            f'data-width-m="{segment.width_m:.3f}" '
            f'data-approximate="{str(segment.approximate).lower()}" '
            f'points="{" ".join(_point(item) for item in points)}" fill="none" '
            f'stroke="#7c3aed" stroke-opacity="0.32" stroke-width="{stroke_width:.2f}" '
            'stroke-linecap="round" stroke-linejoin="round"/>'
        )
    return elements


def _relevant_cameras(
    result: StepResult,
    best: Hypothesis | None,
) -> dict[str, str]:
    states: dict[str, str] = {}
    if best is not None:
        for visit in best.visits:
            states[visit.camera] = "evidence"
    for visit in result.state.pending_review_visits:
        states.setdefault(visit.camera, "review")
    for probe in [*result.state.pending_probes, *result.probes]:
        states.setdefault(probe.camera, "active-probe")
    return states


def _camera_element(
    camera: str,
    state: str,
    point: tuple[float, float],
) -> str:
    x, y = point
    state_label = {
        "evidence": "video evidence",
        "active-probe": "being checked",
        "review": "candidate review",
    }.get(state, "search camera")
    return (
        f'<g data-role="camera" data-state="{escape(state)}">'
        f'<rect x="{x - 9:.2f}" y="{y - 6:.2f}" width="15" height="12" rx="2" '
        'fill="#475569" stroke="#fff" stroke-width="2"/>'
        f'<path d="M {x + 6:.2f} {y - 4:.2f} L {x + 12:.2f} {y - 7:.2f} '
        f'L {x + 12:.2f} {y + 7:.2f} L {x + 6:.2f} {y + 4:.2f} Z" '
        'fill="#475569" stroke="#fff" stroke-width="1.5"/>'
        f'<circle cx="{x - 2:.2f}" cy="{y:.2f}" r="2.5" fill="#cbd5e1"/>'
        f'<text class="label" x="{x + 16:.2f}" y="{y - 10:.2f}">'
        f'{escape(camera)} · {escape(state_label)}</text>'
        '</g>'
    )


def _last_position_element(point: GroundPosition) -> str:
    timestamp = _time_label(point.timestamp)
    return (
        '<g data-role="last-confirmed-position">'
        f'<circle cx="{point.x:.2f}" cy="{point.y:.2f}" r="12" fill="#fff" '
        'stroke="#111827" stroke-width="3"/>'
        f'<circle cx="{point.x:.2f}" cy="{point.y - 4:.2f}" r="3.2" fill="#111827"/>'
        f'<path d="M {point.x:.2f} {point.y:.2f} L {point.x:.2f} {point.y + 7:.2f} '
        f'M {point.x - 5:.2f} {point.y + 2:.2f} L {point.x + 5:.2f} {point.y + 2:.2f}" '
        'stroke="#111827" stroke-width="2.5" stroke-linecap="round"/>'
        f'<text class="label" x="{point.x + 17:.2f}" y="{point.y + 20:.2f}">'
        f'Last confirmed position · {escape(timestamp)}</text></g>'
    )


def _status_text(
    result: StepResult,
    presentation: RoutePresentation,
) -> str:
    point = presentation.last_confirmed_position
    if point is None:
        text = "No confirmed position"
    else:
        timestamp = _time_label(point.timestamp)
        if presentation.position_state == PositionState.OBSERVED:
            text = f"Currently visible · last confirmed {timestamp}"
        elif presentation.position_state == PositionState.LOST:
            text = f"Not seen since {timestamp}"
            if presentation.search_area_as_of is not None:
                calculated = _time_label(presentation.search_area_as_of)
                text += f" · possible area as of {calculated}"
        else:
            text = f"Last confirmed {timestamp}"
            if presentation.visible_at_interval_end:
                text += " · visible at end of checked clip"
    if result.done:
        text += (
            " · search stopped at maximum gap"
            if result.stop_reason == StopReason.MAX_GAP
            else " · search closed"
        )
    return text


def _status_element(text: str) -> str:
    return (
        '<g data-role="route-status" transform="translate(18 24)">'
        '<rect x="-8" y="-18" width="950" height="36" rx="6" '
        'fill="#fff" fill-opacity="0.90"/>'
        f'<text class="label" x="0" y="6">{escape(text)}</text></g>'
    )


def _legend_element(
    layers: list[RouteLayer],
    *,
    search_stopped_at_max_gap: bool = False,
) -> str:
    rows: list[str] = []
    y = 0
    for layer in layers:
        if layer == RouteLayer.OBSERVED:
            sample = f'<line x1="0" y1="{y}" x2="34" y2="{y}" stroke="#0891b2" stroke-width="6"/>'
            label = "Seen on video"
        elif layer == RouteLayer.INFERRED:
            sample = f'<line x1="0" y1="{y}" x2="34" y2="{y}" stroke="#d97706" stroke-width="5" stroke-dasharray="10 7"/>'
            label = "Estimated between sightings"
        elif layer == RouteLayer.SEARCH_AREA:
            sample = f'<line x1="0" y1="{y}" x2="34" y2="{y}" stroke="#7c3aed" stroke-width="14" stroke-opacity="0.32"/>'
            label = (
                "Possible area when search stopped"
                if search_stopped_at_max_gap
                else "Possible area after last sighting"
            )
        elif layer == RouteLayer.LAST_POSITION:
            sample = f'<circle cx="17" cy="{y}" r="7" fill="#fff" stroke="#111827" stroke-width="3"/>'
            label = "Last confirmed position"
        else:
            sample = f'<rect x="8" y="{y - 6}" width="16" height="12" rx="2" fill="#475569"/>'
            label = "Camera involved in this search"
        rows.append(sample + f'<text x="44" y="{y + 5}">{label}</text>')
        y += 24
    height = max(y + 4, 28)
    return (
        '<g data-role="legend" transform="translate(18 82)">'
        f'<rect x="-8" y="-18" width="340" height="{height}" rx="6" '
        'fill="#fff" fill-opacity="0.86"/>'
        f'{"".join(rows)}</g>'
    )


def render_route_maps(
    step_result: StepResult,
    graph: SiteGraph,
    floor_assets: dict[str, bytes],
) -> RenderedRouteBundle:
    """Return standalone SVGs; never reads files and never mutates its inputs."""
    best = _best(step_result)
    presentation = _presentation(step_result, best)
    if graph.map and graph.map.floors:
        floors = [(floor.id, floor.size_px[0], floor.size_px[1], floor.image) for floor in graph.map.floors]
    else:
        explicit = sorted(
            {item.floor for item in graph.cameras.values() if item.floor}
            | {item.floor for item in graph.blind_nodes if item.floor}
        )
        floors = [(floor, 1200, 800, None) for floor in explicit] or [("ABSTRACT", 1200, 800, None)]

    maps: list[RenderedFloorMap] = []
    relevant_cameras = _relevant_cameras(step_result, best)
    shown_layers: set[RouteLayer] = set()

    for floor_id, width, height, image_path in sorted(floors):
        positions = _layout(graph, floor_id, width, height)
        content: list[str] = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="route-title">',
            f'<title id="route-title">{escape(f"Route on floor {floor_id}")}</title>',
            '<rect width="100%" height="100%" fill="#f8fafc"/>',
            '<style>text{font-family:system-ui,sans-serif;font-size:14px;paint-order:stroke;stroke:#fff;stroke-width:3px;stroke-linejoin:round}.label{font-weight:650}</style>',
        ]
        asset = floor_assets.get(floor_id)
        if asset is None and image_path:
            asset = floor_assets.get(image_path)
        if asset:
            uri = base64.b64encode(asset).decode("ascii")
            content.append(
                f'<image data-role="floor-plan" href="data:{_mime(asset)};base64,{uri}" '
                f'x="0" y="0" width="{width}" height="{height}" preserveAspectRatio="none"/>'
            )

        reachable_elements = _reachable_elements(
            step_result, graph, floor_id, positions
        )
        if reachable_elements:
            shown_layers.add(RouteLayer.SEARCH_AREA)
            content.extend(reachable_elements)

        floor_has_inferred = False
        observed_elements: list[str] = []
        inferred_elements: list[str] = []
        # Competing hypotheses remain available in the Controls response, but
        # drawing all of them on the primary operator map makes their inferred
        # geometry look like extra movement by the selected person.  The main
        # SVG therefore renders only ``best``.  Hidden path alternatives that
        # belong to a transition of that best hypothesis are still rendered by
        # ``_route_elements`` as local corridor alternatives.
        if best:
            inferred_elements = _route_elements(
                best,
                graph,
                floor_id,
                positions,
                "inferred-path",
                False,
            )
            observed_elements = _observed_elements(best, floor_id)
            content.append('<g data-role="best-route">')
            if inferred_elements:
                floor_has_inferred = True
                shown_layers.add(RouteLayer.INFERRED)
                content.extend(inferred_elements)
            if observed_elements:
                shown_layers.add(RouteLayer.OBSERVED)
                content.extend(observed_elements)
            content.append('</g>')

        floor_camera_count = 0
        for camera, camera_state in sorted(relevant_cameras.items()):
            point = positions.get(camera)
            if point is None:
                continue
            floor_camera_count += 1
            content.append(_camera_element(camera, camera_state, point))
        if floor_camera_count:
            shown_layers.add(RouteLayer.CAMERAS)

        last_position = presentation.last_confirmed_position
        position_on_floor = bool(
            last_position and last_position.floor_id == floor_id
        )
        if last_position is not None and position_on_floor:
            shown_layers.add(RouteLayer.LAST_POSITION)
            content.append(_last_position_element(last_position))
        relevant_blind_nodes = {
            node_id
            for segment in (
                step_result.estimate.reachable_segments
                if step_result.estimate is not None
                else []
            )
            for node_id in (segment.source, segment.target)
        } | {
            node.node_id
            for node in (
                step_result.estimate.reachable_nodes
                if step_result.estimate is not None
                else []
            )
        }
        for node in sorted(
            (
                item
                for item in graph.blind_nodes
                if item.id in relevant_blind_nodes
            ),
            key=lambda item: item.id,
        ):
            point = positions.get(node.id)
            if point:
                x, y = point
                content.append(
                    f'<g data-role="blind-node"><rect x="{x - 8:.2f}" y="{y - 8:.2f}" width="16" height="16" '
                    f'fill="#111827"/><text x="{x + 13:.2f}" y="{y + 5:.2f}">{escape(node.id)}</text></g>'
                )
        content.append(_status_element(_status_text(step_result, presentation)))
        floor_layers: list[RouteLayer] = []
        if observed_elements:
            floor_layers.append(RouteLayer.OBSERVED)
        if floor_has_inferred:
            floor_layers.append(RouteLayer.INFERRED)
        if reachable_elements:
            floor_layers.append(RouteLayer.SEARCH_AREA)
        if position_on_floor:
            floor_layers.append(RouteLayer.LAST_POSITION)
        if floor_camera_count:
            floor_layers.append(RouteLayer.CAMERAS)
        if floor_layers:
            content.append(
                _legend_element(
                    floor_layers,
                    search_stopped_at_max_gap=(
                        step_result.done
                        and step_result.stop_reason == StopReason.MAX_GAP
                    ),
                )
            )
        content.append('</svg>')
        if best and best.observed_segments:
            observed_cameras = sorted(
                {
                    camera
                    for segment in best.observed_segments
                    for camera in segment.camera_ids
                }
            )
            sources = ", ".join(observed_cameras) or "calibrated cameras"
            alt_text = (
                f"Observed movement on floor {floor_id} from {sources}. "
                f"{_status_text(step_result, presentation)}."
            )
        elif best and best.visits:
            sources = ", ".join(sorted({visit.camera for visit in best.visits}))
            alt_text = (
                f"Search evidence on floor {floor_id} from {sources}. "
                f"{_status_text(step_result, presentation)}."
            )
        else:
            alt_text = f"Search map on floor {floor_id}; no confirmed position"
        maps.append(
            RenderedFloorMap(
                floor_id=floor_id,
                width=width,
                height=height,
                svg="".join(content),
                alt_text=alt_text,
            )
        )
    presentation = presentation.model_copy(
        update={"shown_layers": sorted(shown_layers, key=lambda item: item.value)}
    )
    return RenderedRouteBundle(
        route_revision=step_result.state.state_version,
        best_hypothesis_id=best.id if best else None,
        presentation=presentation,
        maps=maps,
    )
