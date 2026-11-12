from __future__ import annotations

from datetime import datetime, timedelta, timezone

from javes_contracts import (
    Anchors,
    CameraCalibration,
    CameraDef,
    Candidate,
    CaseInfo,
    EdgeDef,
    Hypothesis,
    HypothesisStatus,
    MapDef,
    MapFloor,
    ProbeRequest,
    ProbeResult,
    ProbeStatus,
    ProjectedTrajectoryPoint,
    SearchMode,
    SearchState,
    SiteGraph,
    StepResult,
    Tracklet,
    TrajectoryPoint,
    Visit,
    WalkEdgeDef,
    WalkGraphDef,
    WalkNodeDef,
)
from javes_routing.core.ground import build_transition_geometry
from javes_routing.core.expand import expand_hypotheses
from javes_routing.core.params import RoutingParams
from javes_routing.core.render import render_route_maps
from javes_routing.core.step import init_state, step
from javes_routing.core.update import update_hypotheses


UTC = timezone.utc
T0 = datetime(2026, 11, 6, 6, 0, tzinfo=UTC)


def _graph() -> SiteGraph:
    calibration = CameraCalibration(
        frame_size_px=(500, 400),
        homography=((1, 0, 0), (0, 1, 0), (0, 0, 1)),
        floor_id="1F",
        error_m=0.1,
    )
    return SiteGraph(
        site="walk-path",
        version="v1",
        map=MapDef(
            floors=[MapFloor(id="1F", size_px=(500, 400), m_per_px=0.1)]
        ),
        cameras={
            "A": CameraDef(
                id="A", floor="1F", map_xy=(20, 20), calibration=calibration
            ),
            "B": CameraDef(
                id="B", floor="1F", map_xy=(480, 20), calibration=calibration
            ),
            "C": CameraDef(
                id="C",
                floor="1F",
                map_xy=(250, 20),
                calibration=calibration,
                coverage_polygon=[(180, 30), (220, 30), (220, 80), (180, 80)],
            ),
        },
        edges=[
            EdgeDef(
                id="camera-A-B",
                source="A",
                target="B",
                t_min=1,
                t_typ=10,
                t_max=30,
            )
        ],
        walk_graph=WalkGraphDef(
            nodes={
                "west": WalkNodeDef(
                    id="west", floor_id="1F", map_xy=(100, 100)
                ),
                "north": WalkNodeDef(
                    id="north", floor_id="1F", map_xy=(200, 50)
                ),
                "south": WalkNodeDef(
                    id="south", floor_id="1F", map_xy=(200, 200)
                ),
                "east": WalkNodeDef(
                    id="east", floor_id="1F", map_xy=(300, 100)
                ),
            },
            edges=[
                WalkEdgeDef(
                    id="west-north", source="west", target="north", floor_id="1F", d_m=11
                ),
                WalkEdgeDef(
                    id="north-east", source="north", target="east", floor_id="1F", d_m=11
                ),
                WalkEdgeDef(
                    id="west-south", source="west", target="south", floor_id="1F", d_m=15
                ),
                WalkEdgeDef(
                    id="south-east", source="south", target="east", floor_id="1F", d_m=15
                ),
            ],
        ),
    )


def _projected(timestamp: datetime, x: float, y: float) -> ProjectedTrajectoryPoint:
    return ProjectedTrajectoryPoint(
        timestamp=timestamp,
        x=x,
        y=y,
        floor_id="1F",
        calibration_error_m=0.1,
    )


def test_directed_ground_path_uses_only_authorised_walk_edges() -> None:
    graph = _graph()
    geometry = build_transition_geometry(
        _projected(T0, 90, 100),
        _projected(T0 + timedelta(seconds=20), 310, 100),
        graph,
        RoutingParams(max_walk_snap_m=2),
    )
    assert geometry is not None
    assert geometry.points == [
        (100.0, 100.0),
        (200.0, 50.0),
        (300.0, 100.0),
    ]
    assert geometry.walk_edge_ids == ["west-north", "north-east"]
    assert geometry.alternative_paths == [
        [
            (100.0, 100.0),
            (200.0, 200.0),
            (300.0, 100.0),
        ]
    ]
    assert geometry.alternative_walk_edge_paths == [
        ["west-south", "south-east"]
    ]
    assert geometry.start_offset_m == 1.0
    assert geometry.end_offset_m == 1.0
    assert geometry.distance_min_m == 19.8
    assert geometry.distance_max_m == 24.2
    assert geometry.approximate

    assert (
        build_transition_geometry(
            _projected(T0, 310, 100),
            _projected(T0 + timedelta(seconds=20), 90, 100),
            graph,
            RoutingParams(max_walk_snap_m=2),
        )
        is None
    )


def test_routing_attaches_walk_geometry_and_svg_avoids_camera_markers() -> None:
    graph = _graph()
    params = RoutingParams(max_walk_snap_m=2)
    first = Visit(
        visit_id="visit-A",
        camera_id="A",
        t_in=T0,
        t_out=T0 + timedelta(seconds=2),
        tracklet_ids=["track-A"],
        appearance_score=0.9,
        projected_trajectory=[
            _projected(T0, 80, 100),
            _projected(T0 + timedelta(seconds=2), 90, 100),
        ],
        trajectory_approximate=False,
    )
    hypothesis = Hypothesis(
        id="h-A",
        visits=[first],
        current_node="A",
        status=HypothesisStatus.ALIVE,
    )
    probe = ProbeRequest(
        probe_id="probe-B",
        camera_id="B",
        t_from=T0 + timedelta(seconds=3),
        t_to=T0 + timedelta(seconds=30),
    )
    state = SearchState(
        case_id="case",
        site_version=graph.version,
        params_version=params.version,
        hypotheses=[hypothesis],
        pending_probes=[probe],
    )
    tracklet = Tracklet(
        tracklet_id="track-B",
        probe_id=probe.probe_id,
        camera_id="B",
        t_start=T0 + timedelta(seconds=20),
        t_end=T0 + timedelta(seconds=22),
        bbox_first=(300, 60, 20, 40),
        bbox_last=(310, 60, 20, 40),
        trajectory=[
            TrajectoryPoint(
                timestamp=T0 + timedelta(seconds=20), x_px=310, y_px=100
            ),
            TrajectoryPoint(
                timestamp=T0 + timedelta(seconds=22), x_px=320, y_px=100
            ),
        ],
    )
    hypotheses, _, updated_state = update_hypotheses(
        state,
        [
            ProbeResult(
                probe_id=probe.probe_id,
                status=ProbeStatus.DONE,
                tracklets=[tracklet],
                candidates=[
                    Candidate(
                        candidate_id="candidate-B",
                        tracklet_id=tracklet.id,
                        appearance_score=0.9,
                    )
                ],
            )
        ],
        graph,
        Anchors(),
        params,
        T0 + timedelta(seconds=30),
    )
    attached = next(item for item in hypotheses if len(item.visits) == 2)
    geometry = attached.transitions[-1].geometry
    assert geometry is not None
    assert geometry.points[0] == (100.0, 100.0)
    assert geometry.points[-1] == (300.0, 100.0)
    assert (90.0, 100.0) not in geometry.points
    assert (310.0, 100.0) not in geometry.points

    svg = render_route_maps(
        StepResult(
            state=updated_state,
            probes=[],
            hypotheses=[attached],
            best=attached,
            mode=SearchMode.CATCHING_UP,
        ),
        graph,
        {},
    ).maps[0].svg
    assert 'data-role="inferred-path"' in svg
    assert "100.00,100.00 200.00,50.00 300.00,100.00" in svg
    assert "90.00,100.00 100.00,100.00" not in svg
    assert "300.00,100.00 310.00,100.00" not in svg
    assert "20.00,20.00" not in svg
    assert "480.00,20.00" not in svg


def test_reachable_ground_path_selects_camera_by_coverage_polygon() -> None:
    graph = _graph()
    params = RoutingParams(max_walk_snap_m=2, max_speed=2)
    visit = Visit(
        visit_id="visit-A",
        camera_id="A",
        t_in=T0,
        t_out=T0 + timedelta(seconds=2),
        tracklet_ids=["track-A"],
        appearance_score=0.9,
        projected_trajectory=[
            _projected(T0, 80, 100),
            _projected(T0 + timedelta(seconds=2), 90, 100),
        ],
        trajectory_approximate=False,
    )
    hypothesis = Hypothesis(
        id="h-A",
        visits=[visit],
        current_node="A",
        status=HypothesisStatus.ALIVE,
    )
    state = SearchState(
        case_id="case",
        site_version=graph.version,
        params_version=params.version,
        hypotheses=[hypothesis],
        window_start=T0,
        window_end=T0 + timedelta(seconds=30),
    )
    probes, _ = expand_hypotheses(
        state, graph, params, T0 + timedelta(seconds=10)
    )
    coverage = [probe for probe in probes if probe.camera == "C"]
    assert len(coverage) == 1
    assert coverage[0].reason == "GROUND_COVERAGE"


def test_historical_clamp_does_not_duplicate_pending_ground_probe() -> None:
    graph = _graph()
    params = RoutingParams(
        historical_shell_s=30,
        max_gap_s=60,
        max_walk_snap_m=2,
    )
    state = init_state(
        CaseInfo(
            case_id="historical-ground",
            target_id="target",
            start_time=T0,
            last_known_camera="A",
            last_known_time=T0,
            window_start=T0,
            window_end=T0 + timedelta(minutes=5),
        ),
        graph,
        params,
    )
    issued = step(
        state,
        [],
        graph,
        Anchors(),
        params,
        T0 + timedelta(minutes=5),
    )
    pending_ids = [probe.probe_id for probe in issued.state.pending_probes]
    assert len(pending_ids) == len(set(pending_ids))

    heartbeat = step(
        issued.state,
        [],
        graph,
        Anchors(),
        params,
        T0 + timedelta(minutes=5),
    )
    repeated_ids = [probe.probe_id for probe in heartbeat.state.pending_probes]
    assert repeated_ids == pending_ids

    completed = step(
        heartbeat.state,
        [
            ProbeResult(
                probe_id=probe.probe_id,
                status=ProbeStatus.DONE,
                coverage_ratio=1.0,
                processed_duration_s=(probe.t_to - probe.t_from).total_seconds(),
            )
            for probe in heartbeat.state.pending_probes
            if probe.t_to is not None
        ],
        graph,
        Anchors(),
        params,
        T0 + timedelta(minutes=5),
    )
    assert not set(pending_ids) & {
        probe.probe_id for probe in completed.state.pending_probes
    }
