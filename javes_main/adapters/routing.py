from __future__ import annotations

from datetime import datetime

from javes_contracts import (
    Anchors,
    CaseInfo,
    ProbeResult,
    RoutingWorkerResult,
    SearchState,
    SiteGraph,
)
from routing.contracts.params import RoutingParams
from routing.step import init_state
from routing.worker import step_and_render


class LocalRoutingAdapter:
    def initialise(
        self, case: CaseInfo, graph: SiteGraph, params: RoutingParams
    ) -> SearchState:
        return init_state(case, graph, params)

    def advance(
        self,
        state: SearchState,
        probe_results: list[ProbeResult],
        graph: SiteGraph,
        anchors: Anchors,
        params: RoutingParams,
        now: datetime,
        floor_assets: dict[str, bytes],
    ) -> RoutingWorkerResult:
        return step_and_render(
            state,
            probe_results,
            graph,
            anchors,
            params,
            now,
            floor_assets,
        )
