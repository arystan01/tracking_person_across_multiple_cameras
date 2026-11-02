"""Pure, deterministic SVG rendering owned by Routing."""
from __future__ import annotations

import base64
import math
from html import escape

from .contracts import (
    Hypothesis,
    ProbeStatus,
    RenderedFloorMap,
    RenderedRouteBundle,
    SiteGraph,
    StepResult,
)


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


def _route_elements(
    hypothesis: Hypothesis,
    graph: SiteGraph,
    floor_id: str,
    positions: dict[str, tuple[float, float]],
    role: str,
    dashed: bool,
) -> list[str]:
    elements: list[str] = []
    colour = "#f59e0b" if dashed else "#ef4444"
    width = "4" if dashed else "7"
    for index in range(1, len(hypothesis.visits)):
        previous, current = hypothesis.visits[index - 1], hypothesis.visits[index]
        transition = hypothesis.transitions[index - 1] if index - 1 < len(hypothesis.transitions) else None
        pieces: list[tuple[list[tuple[float, float]], bool, str]] = []
        if transition and transition.edge_ids:
            pieces = [
                (points, not bool(edge.map_path), edge.id)
                for edge_id in transition.edge_ids
                if (edge := next((item for item in graph.edges if item.id == edge_id), None)) is not None
                if (points := _edge_points(graph, edge_id, floor_id, positions))
            ]
        if not pieces:
            p1 = positions.get(previous.camera)
            p2 = positions.get(current.camera)
            camera_floor_1 = graph.cameras.get(previous.camera).floor if previous.camera in graph.cameras else None
            camera_floor_2 = graph.cameras.get(current.camera).floor if current.camera in graph.cameras else None
            if (camera_floor_1 or "ABSTRACT") == floor_id and (camera_floor_2 or "ABSTRACT") == floor_id and p1 and p2:
                pieces = [([p1, p2], True, "approximate")]
        transition_gap = bool(
            transition
            and (
                transition.kind in {"BLIND", "OUTSIDE", "UNKNOWN", "GAP", "VERTICAL"}
                or set(transition.flags) & {"LONG_GAP", "GATE_OVERRIDE", "UNKNOWN"}
            )
        )
        for points, approximate, edge_label in pieces:
            segment_dashed = dashed or approximate or transition_gap
            dash = ' stroke-dasharray="10 7"' if segment_dashed else ""
            if len(points) >= 2:
                elements.append(
                    f'<polyline data-role="{role}" data-edge="{escape(edge_label)}" '
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
        if transition:
            for alternative in transition.alternative_edge_paths:
                for edge_id in alternative:
                    points = _edge_points(graph, edge_id, floor_id, positions)
                    if len(points) >= 2:
                        elements.append(
                            f'<polyline data-role="alternative-path" data-edge="{escape(edge_id)}" '
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
    return elements


def render_route_maps(
    step_result: StepResult,
    graph: SiteGraph,
    floor_assets: dict[str, bytes],
) -> RenderedRouteBundle:
    """Return standalone SVGs; never reads files and never mutates its inputs."""
    best = _best(step_result)
    if graph.map and graph.map.floors:
        floors = [(floor.id, floor.size_px[0], floor.size_px[1], floor.image) for floor in graph.map.floors]
    else:
        explicit = sorted(
            {item.floor for item in graph.cameras.values() if item.floor}
            | {item.floor for item in graph.blind_nodes if item.floor}
        )
        floors = [(floor, 1200, 800, None) for floor in explicit] or [("ABSTRACT", 1200, 800, None)]

    maps: list[RenderedFloorMap] = []
    found = {visit.camera for hypothesis in step_result.hypotheses for visit in hypothesis.visits}
    checked_empty = {
        window.camera for window in step_result.state.probed_windows
        if window.status == ProbeStatus.DONE and not window.found
    }
    current = best.visits[-1].camera if best and best.visits else None

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

        alternatives = sorted(
            (item for item in step_result.hypotheses if best is None or item.id != best.id),
            key=lambda item: (-item.score, item.id),
        )
        for hypothesis in alternatives:
            content.extend(_route_elements(hypothesis, graph, floor_id, positions, "alternative-route", True))
        if best:
            content.extend(_route_elements(best, graph, floor_id, positions, "best-route", False))

        for zone in step_result.estimate.zones if step_result.estimate else []:
            point = positions.get(zone.zone)
            if point:
                x, y = point
                content.append(
                    f'<g data-role="estimate"><circle cx="{x:.2f}" cy="{y:.2f}" r="24" '
                    f'fill="#8b5cf6" fill-opacity="{min(0.15 + zone.probability * 0.55, 0.7):.3f}"/>'
                    f'<text x="{x + 28:.2f}" y="{y + 5:.2f}">{zone.probability:.0%}</text></g>'
                )

        for camera in sorted(graph.cameras):
            point = positions.get(camera)
            if point is None:
                continue
            if camera == current:
                colour, state_name, radius = "#dc2626", "current", 11
            elif camera in found:
                colour, state_name, radius = "#16a34a", "found", 9
            elif camera in checked_empty:
                colour, state_name, radius = "#64748b", "checked-empty", 8
            else:
                colour, state_name, radius = "#2563eb", "unchecked", 8
            x, y = point
            content.append(
                f'<g data-role="camera" data-state="{state_name}"><circle cx="{x:.2f}" cy="{y:.2f}" '
                f'r="{radius}" fill="{colour}" stroke="#fff" stroke-width="2"/>'
                f'<text class="label" x="{x + 13:.2f}" y="{y - 10:.2f}">{escape(camera)}</text></g>'
            )
        for node in sorted(graph.blind_nodes, key=lambda item: item.id):
            point = positions.get(node.id)
            if point:
                x, y = point
                content.append(
                    f'<g data-role="blind-node"><rect x="{x - 8:.2f}" y="{y - 8:.2f}" width="16" height="16" '
                    f'fill="#111827"/><text x="{x + 13:.2f}" y="{y + 5:.2f}">{escape(node.id)}</text></g>'
                )
        content.append('</svg>')
        if best and best.visits:
            route_text = " to ".join(visit.camera for visit in best.visits)
            alt_text = f"Best route on floor {floor_id}: {route_text}"
        else:
            alt_text = f"Route map on floor {floor_id}; no confirmed route"
        maps.append(
            RenderedFloorMap(
                floor_id=floor_id,
                width=width,
                height=height,
                svg="".join(content),
                alt_text=alt_text,
            )
        )
    return RenderedRouteBundle(
        route_revision=step_result.state.state_version,
        best_hypothesis_id=best.id if best else None,
        maps=maps,
    )
