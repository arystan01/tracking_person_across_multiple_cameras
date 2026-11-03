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
        self,
        case: CaseInfo,
        graph: SiteGraph,
        params: RoutingParams,
        anchors: Anchors,
        now: datetime,
    ) -> SearchState:
        del now
        anchored_case = case.model_copy(
            update={
                "last_known_camera": (
                    anchors.last_known_camera or case.last_known_camera
                ),
                "last_known_zone": anchors.last_known_zone or case.last_known_zone,
                "last_known_time": anchors.last_known_time or case.last_known_time,
            },
            deep=True,
        )
        return init_state(anchored_case, graph, params)

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
