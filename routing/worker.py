"""Convenience boundary returning both logical and already-rendered output."""
from __future__ import annotations

from datetime import datetime

from .contracts import (
    Anchors,
    ProbeResult,
    RoutingWorkerResult,
    SearchState,
    SiteGraph,
)
from .contracts.params import RoutingParams
from .render import render_route_maps
from .step import step


def step_and_render(
    state: SearchState,
    probe_results: list[ProbeResult | dict],
    graph: SiteGraph,
    anchors: Anchors,
    params: RoutingParams,
    now: datetime,
    floor_assets: dict[str, bytes],
) -> RoutingWorkerResult:
    """Pure worker facade for Case Manager integrations."""
    step_result = step(state, probe_results, graph, anchors, params, now)
    return RoutingWorkerResult(
        step_result=step_result,
        rendered_route=render_route_maps(step_result, graph, floor_assets),
    )
