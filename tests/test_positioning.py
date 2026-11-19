from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from javes_contracts import (
    Anchors,
    BlindNodeDef,
    CameraCalibration,
    CameraDef,
    Candidate,
    EdgeDef,
    Estimate,
    Hypothesis,
    HypothesisStatus,
    MapDef,
    MapFloor,
    OutsideDef,
    PositionState,
    ProbeStatus,
    ProbedWindow,
    ProjectedTrajectoryPoint,
    ReachableNode,
    ReachableSegment,
    RouteLayer,
    SearchFrontier,
    SearchMode,
    SiteGraph,
    StopReason,
    StepResult,
    Tracklet,
    TrajectoryPoint,
    Transition,
    TransitionGeometry,
    Visit,
    WalkEdgeDef,
    WalkGraphDef,
    WalkNodeDef,
)
from javes_routing.core.params import RoutingParams
from javes_routing.core.predict import compute_estimate
from javes_routing.core.positioning import (
    movement_ranges,
    project_trajectory,
    reachable_area,
)
from javes_routing.core.render import render_route_maps
from javes_routing.core.site import parse_site, validate_corridor_geometry
from javes_routing.core.step import step
from javes_routing.core.update import merge_tracklets_to_visits
from javes_site_config.registry import model_checksum


UTC = timezone.utc
T0 = datetime(2026, 11, 6, 4, 0, tzinfo=UTC)


def _calibration(*, error_m: float = 0.1) -> CameraCalibration:
    return CameraCalibration(
        frame_size_px=(640, 480),
        homography=((2.0, 0.0, 10.0), (0.0, 2.0, 20.0), (0.0, 0.0, 1.0)),
        floor_id="1F",
        error_m=error_m,
    )


def _graph(*, calibrated: bool = True) -> SiteGraph:
    return SiteGraph(
        site="positioning",
        version="v1",
        map=MapDef(
            floors=[MapFloor(id="1F", size_px=(1000, 800), m_per_px=0.1)]
        ),
        cameras={
            "A": CameraDef(
                id="A",
                floor="1F",
                map_xy=(20, 40),
                calibration=_calibration() if calibrated else None,
            ),
            "B": CameraDef(id="B", floor="1F", map_xy=(300, 40)),
            "C": CameraDef(id="C", floor="1F", map_xy=(20, 300)),
        },
        blind_nodes=[
            BlindNodeDef(id="blind", floor="1F", map_xy=(500, 40))
        ],
        outside=OutsideDef(entrances={"C": 1.0}),
        edges=[
            EdgeDef(
                id="A-B",
                source="A",
                target="B",
                t_min=2,
                t_typ=5,
                t_max=10,
                d_m=10,
                map_path=[(150, 40)],
            ),
            EdgeDef(
                id="A-C",
                source="A",
                target="C",
                t_min=2,
                t_typ=5,
                t_max=10,
                d_m=10,
                map_path=[(20, 150)],
            ),
            EdgeDef(
                id="B-blind",
                source="B",
                target="blind",
                t_min=2,
                t_typ=5,
                t_max=10,
                d_m=10,
            ),
            EdgeDef(
                id="C-outside",
                source="C",
                target="OUTSIDE",
                t_min=2,
                t_typ=5,
                t_max=10,
                d_m=10,
            ),
        ],
        walk_graph=WalkGraphDef(
            nodes={
                "wa": WalkNodeDef(id="wa", floor_id="1F", map_xy=(26, 48)),
                "wb": WalkNodeDef(id="wb", floor_id="1F", map_xy=(126, 48)),
                "wc": WalkNodeDef(id="wc", floor_id="1F", map_xy=(26, 148)),
                "wblind": WalkNodeDef(
                    id="wblind", floor_id="1F", map_xy=(226, 48), kind="BLIND"
                ),
                "woutside": WalkNodeDef(
                    id="woutside", floor_id="1F", map_xy=(26, 248), kind="OUTSIDE"
                ),
            },
            edges=[
                WalkEdgeDef(id="walk-A-B", source="wa", target="wb", floor_id="1F", d_m=10),
                WalkEdgeDef(id="walk-A-C", source="wa", target="wc", floor_id="1F", d_m=10),
                WalkEdgeDef(id="walk-B-blind", source="wb", target="wblind", floor_id="1F", d_m=10),
                WalkEdgeDef(id="walk-C-outside", source="wc", target="woutside", floor_id="1F", d_m=10),
            ],
        ),
    )


def _visit_with_trajectory() -> Visit:
    return Visit(
        visit_id="vA",
        camera_id="A",
        t_in=T0,
        t_out=T0 + timedelta(seconds=4),
        tracklet_ids=["track-A"],
        appearance_score=0.9,
        projected_trajectory=[
            ProjectedTrajectoryPoint(
                timestamp=T0,
                x=20,
                y=40,
                floor_id="1F",
                calibration_error_m=0.1,
            ),
            ProjectedTrajectoryPoint(
                timestamp=T0 + timedelta(seconds=2),
                x=23,
                y=44,
                floor_id="1F",
                calibration_error_m=0.1,
            ),
            ProjectedTrajectoryPoint(
                timestamp=T0 + timedelta(seconds=4),
                x=26,
                y=48,
                floor_id="1F",
                calibration_error_m=0.1,
            ),
        ],
        trajectory_approximate=False,
    )


def test_projection_is_pure_and_visit_survives_json_round_trip() -> None:
    graph = _graph()
    trajectory = [
        TrajectoryPoint(timestamp=T0, x_px=5, y_px=10, confidence=0.9),
        TrajectoryPoint(
            timestamp=T0 + timedelta(seconds=1),
            x_px=15,
            y_px=20,
            confidence=0.8,
        ),
    ]
    before = [item.model_dump_json() for item in trajectory]
    first = project_trajectory(
        graph.cameras["A"], trajectory, max_error_m=2.0
    )
    second = project_trajectory(
        graph.cameras["A"], trajectory, max_error_m=2.0
    )
    assert first == second
    assert [item.model_dump_json() for item in trajectory] == before
    assert [(item.x, item.y) for item in first[0]] == [(20, 40), (40, 60)]
    assert first[1] is False

    tracklet = Tracklet(
        tracklet_id="track-A",
        camera_id="A",
        t_start=T0,
        t_end=T0 + timedelta(seconds=1),
        bbox_first=(0, 0, 10, 20),
        bbox_last=(10, 10, 10, 20),
        trajectory=trajectory,
    )
    visit = merge_tracklets_to_visits(
        [tracklet],
        [Candidate(candidate_id="candidate-A", tracklet_id="track-A", appearance_score=0.9)],
        graph,
        RoutingParams(),
        T0 + timedelta(seconds=2),
    )[0]
    assert [(item.x, item.y) for item in visit.projected_trajectory] == [
        (20, 40),
        (40, 60),
    ]
    assert Visit.model_validate_json(visit.model_dump_json()) == visit


def test_bad_or_missing_calibration_falls_back_to_approximate() -> None:
    point = [TrajectoryPoint(timestamp=T0, x_px=5, y_px=10)]
    missing, missing_approximate = project_trajectory(
        _graph(calibrated=False).cameras["A"], point, max_error_m=2.0
    )
    assert missing == [] and missing_approximate

    camera = CameraDef(
        id="A", floor="1F", calibration=_calibration(error_m=3.0)
    )
    rejected, bad_approximate = project_trajectory(
        camera, point, max_error_m=2.0
    )
    assert rejected == [] and bad_approximate

    with pytest.raises(ValidationError, match="invertible"):
        CameraCalibration(
            frame_size_px=(640, 480),
            homography=((1, 0, 0), (1, 0, 0), (0, 0, 1)),
            floor_id="1F",
        )
    with pytest.raises(ValueError, match="unknown floor"):
        parse_site(
            {
                "map": {"floors": [{"id": "1F", "size_px": [100, 100]}]},
                "cameras": {
                    "A": {
                        "floor": "1F",
                        "calibration": {
                            "frame_size_px": [640, 480],
                            "homography": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                            "floor_id": "2F",
                        },
                    }
                },
                "edges": [],
            }
        )


def test_calibration_changes_immutable_site_checksum() -> None:
    graph = _graph()
    changed_camera = graph.cameras["A"].model_copy(
        update={"calibration": _calibration(error_m=0.2)}, deep=True
    )
    changed = graph.model_copy(
        update={"cameras": {**graph.cameras, "A": changed_camera}}, deep=True
    )
    assert model_checksum(graph) != model_checksum(changed)


def test_site_has_separate_camera_coverage_and_pedestrian_topology() -> None:
    graph = parse_site(
        {
            "site": "ground-plane",
            "version": "v1",
            "map": {
                "floors": [
                    {"id": "1F", "size_px": [500, 400], "m_per_px": 0.1}
                ]
            },
            "cameras": {
                "A": {
                    "floor": "1F",
                    "map_xy": [25, 20],
                    "coverage_polygon": [[0, 0], [250, 0], [250, 200], [0, 200]],
                    "overlap_camera_ids": ["B"],
                },
                "B": {
                    "floor": "1F",
                    "map_xy": [475, 20],
                    "coverage_polygon": [[200, 0], [500, 0], [500, 200], [200, 200]],
                    "overlap_camera_ids": ["A"],
                },
            },
            "edges": [],
            "walk_graph": {
                "nodes": {
                    "west": {"floor_id": "1F", "map_xy": [50, 100]},
                    "east": {"floor_id": "1F", "map_xy": [450, 100]},
                },
                "edges": [
                    {
                        "id": "west-east",
                        "from": "west",
                        "to": "east",
                        "floor_id": "1F",
                        "map_path": [[250, 120]],
                        "d_m": 40,
                        "bidirectional": True,
                    }
                ],
            },
        }
    )
    assert graph.cameras["A"].overlap_camera_ids == ["B"]
    assert graph.cameras["A"].coverage_polygon[0] == (0.0, 0.0)
    assert graph.walk_graph is not None
    assert graph.walk_graph.edges[0].source == "west"
    assert graph.walk_graph.nodes["east"].map_xy == (450.0, 100.0)
    reverse_edges = graph.walk_graph.get_edges_from("east")
    assert [(edge.id, edge.target) for edge in reverse_edges] == [
        ("west-east::reverse", "west")
    ]

    camera_only_changed = graph.model_copy(
        update={
            "cameras": {
                **graph.cameras,
                "A": graph.cameras["A"].model_copy(
                    update={"map_xy": (30.0, 20.0)}
                ),
            }
        },
        deep=True,
    )
    walk_changed = graph.model_copy(
        update={
            "walk_graph": WalkGraphDef(
                nodes={
                    **graph.walk_graph.nodes,
                    "west": WalkNodeDef(
                        id="west", floor_id="1F", map_xy=(60, 100)
                    ),
                },
                edges=[
                    WalkEdgeDef(
                        id="west-east",
                        source="west",
                        target="east",
                        floor_id="1F",
                        map_path=[(250, 120)],
                        d_m=40,
                    )
                ],
            )
        },
        deep=True,
    )
    assert model_checksum(graph) != model_checksum(camera_only_changed)
    assert model_checksum(graph) != model_checksum(walk_changed)


def test_site_rejects_asymmetric_overlap_and_invalid_walk_geometry() -> None:
    base = {
        "map": {"floors": [{"id": "1F", "size_px": [100, 100]}]},
        "cameras": {
            "A": {"floor": "1F", "overlap_camera_ids": ["B"]},
            "B": {"floor": "1F"},
        },
        "edges": [],
    }
    with pytest.raises(ValueError, match="symmetric"):
        parse_site(base)

    invalid_walk = {
        **base,
        "cameras": {"A": {"floor": "1F"}, "B": {"floor": "1F"}},
        "walk_graph": {
            "nodes": {
                "inside": {"floor_id": "1F", "map_xy": [50, 50]},
                "outside": {"floor_id": "1F", "map_xy": [101, 50]},
            },
            "edges": [],
        },
    }
    with pytest.raises(ValueError, match="outside floor"):
        parse_site(invalid_walk)


def test_distance_speed_ranges_and_max_speed_guard() -> None:
    graph = _graph()
    first = _visit_with_trajectory()
    second = Visit(
        visit_id="vB",
        camera_id="B",
        t_in=T0 + timedelta(seconds=10),
        t_out=T0 + timedelta(seconds=12),
        appearance_score=0.9,
        projected_trajectory=[
            ProjectedTrajectoryPoint(
                timestamp=T0 + timedelta(seconds=10),
                x=300,
                y=40,
                floor_id="1F",
                calibration_error_m=0.1,
            )
        ],
        trajectory_approximate=False,
    )
    hypothesis = Hypothesis(
        id="h",
        visits=[first, second],
        transitions=[
            Transition(
                from_visit_id="vA",
                to_visit_id="vB",
                edge_ids=["A-B"],
                geometry=TransitionGeometry(
                    floor_id="1F",
                    points=[(26, 48), (150, 40), (300, 40)],
                    distance_min_m=9.5,
                    distance_max_m=10.5,
                ),
            )
        ],
        current_node="B",
        status=HypothesisStatus.ALIVE,
    )
    speed, distance = movement_ranges(
        hypothesis, graph, RoutingParams(max_speed=2.0)
    )
    assert speed.source == "OBSERVED"
    assert (speed.minimum, speed.maximum) == pytest.approx((0.2, 0.3))
    assert speed.maximum <= 2.0
    assert distance.source == "MIXED"
    assert distance.minimum < 11.0 < distance.maximum

    noisy = first.model_copy(
        update={
            "projected_trajectory": [
                ProjectedTrajectoryPoint(timestamp=T0, x=0, y=0, floor_id="1F"),
                ProjectedTrajectoryPoint(
                    timestamp=T0 + timedelta(seconds=0.1),
                    x=100,
                    y=0,
                    floor_id="1F",
                ),
            ]
        },
        deep=True,
    )
    noisy_hypothesis = hypothesis.model_copy(
        update={"visits": [noisy]}, deep=True
    )
    noisy_speed, _ = movement_ranges(
        noisy_hypothesis, graph, RoutingParams(max_speed=2.0)
    )
    assert noisy_speed.source == "FALLBACK"
    assert noisy_speed.maximum == 2.0


def test_reachable_area_follows_directed_graph_and_normalises_leaf_nodes() -> None:
    graph = _graph()
    visit = _visit_with_trajectory().model_copy(
        update={"t_out": T0}, deep=True
    )
    hypothesis = Hypothesis(
        id="h",
        visits=[visit],
        current_node="A",
        status=HypothesisStatus.ALIVE,
    )
    partial_segments, partial_nodes = reachable_area(
        hypothesis,
        graph,
        RoutingParams(max_speed=2.0),
        T0 + timedelta(seconds=10),
    )
    assert {item.edge_id for item in partial_segments} == {
        "walk-A-B",
        "walk-A-C",
        "walk-B-blind",
        "walk-C-outside",
    }
    assert {item.node_id for item in partial_nodes} == {"wb", "wc"}
    initial = [
        item
        for item in partial_segments
        if item.edge_id in {"walk-A-B", "walk-A-C"}
    ]
    assert all(item.points[0] == (26.0, 48.0) for item in initial)
    assert {item.progress for item in partial_segments} == {1.0, 0.2}
    assert sum(item.probability for item in partial_nodes) == pytest.approx(1.0)

    _, terminal_nodes = reachable_area(
        hypothesis,
        graph,
        RoutingParams(max_speed=2.0),
        T0 + timedelta(seconds=15),
    )
    probabilities = {item.node_id: item.probability for item in terminal_nodes}
    assert sum(probabilities.values()) == pytest.approx(1.0)
    assert probabilities["OUTSIDE"] > probabilities["wblind"]


def test_reachable_area_uses_branch_priors_and_done_empty_evidence() -> None:
    graph = _graph()
    assert graph.walk_graph is not None
    graph = graph.model_copy(
        update={
            "cameras": {
                **graph.cameras,
                "B": graph.cameras["B"].model_copy(
                    update={
                        "coverage_polygon": [
                            (100, 20),
                            (150, 20),
                            (150, 70),
                            (100, 70),
                        ],
                        "p_det": 0.9,
                    }
                ),
            },
            "walk_graph": graph.walk_graph.model_copy(
                update={
                    "edges": [
                        edge.model_copy(
                            update={
                                "branch_prior": 3.0
                                if edge.id == "walk-A-B"
                                else 1.0
                            }
                        )
                        for edge in graph.walk_graph.edges
                    ]
                }
            ),
        }
    )
    hypothesis = Hypothesis(
        id="h",
        visits=[_visit_with_trajectory()],
        current_node="A",
        status=HypothesisStatus.ALIVE,
    )
    params = RoutingParams(max_speed=2.0, reachable_heading_kappa=0.0)
    _, prior_nodes = reachable_area(
        hypothesis, graph, params, T0 + timedelta(seconds=15)
    )
    prior = {item.node_id: item.probability for item in prior_nodes}
    assert prior == pytest.approx({"wblind": 0.75, "OUTSIDE": 0.25})

    from javes_contracts import SearchState

    state = SearchState(
        case_id="case",
        site_version=graph.version,
        params_version=params.version,
        probed_windows=[
            ProbedWindow(
                camera_id="B",
                t_from=T0,
                t_to=T0 + timedelta(seconds=15),
                probe_id="empty-B",
                status=ProbeStatus.DONE,
                found=False,
            )
        ],
    )
    _, checked_nodes = reachable_area(
        hypothesis, graph, params, T0 + timedelta(seconds=15), state
    )
    checked = {item.node_id: item.probability for item in checked_nodes}
    assert sum(checked.values()) == pytest.approx(1.0, abs=1e-9)
    assert checked["wblind"] < checked["OUTSIDE"]

    partial_state = state.model_copy(
        update={
            "probed_windows": [
                state.probed_windows[0].model_copy(
                    update={"coverage_ratio": 0.5}
                )
            ]
        },
        deep=True,
    )
    unavailable_state = state.model_copy(
        update={
            "probed_windows": [
                state.probed_windows[0].model_copy(
                    update={
                        "status": ProbeStatus.UNAVAILABLE,
                        "coverage_ratio": 0.0,
                    }
                )
            ]
        },
        deep=True,
    )
    for unverified_state in (partial_state, unavailable_state):
        _, unverified_nodes = reachable_area(
            hypothesis,
            graph,
            params,
            T0 + timedelta(seconds=15),
            unverified_state,
        )
        assert {
            item.node_id: item.probability for item in unverified_nodes
        } == pytest.approx(prior)


def test_reachable_area_cycles_are_finite_deterministic_and_normalised() -> None:
    graph = _graph()
    assert graph.walk_graph is not None
    cycle = WalkGraphDef(
        nodes={
            key: graph.walk_graph.nodes[key]
            for key in ("wa", "wb", "wc")
        },
        edges=[
            WalkEdgeDef(
                id="cycle-a-b", source="wa", target="wb", floor_id="1F", d_m=10
            ),
            WalkEdgeDef(
                id="cycle-b-c", source="wb", target="wc", floor_id="1F", d_m=10
            ),
            WalkEdgeDef(
                id="cycle-c-a", source="wc", target="wa", floor_id="1F", d_m=10
            ),
        ],
    )
    graph = graph.model_copy(update={"walk_graph": cycle}, deep=True)
    hypothesis = Hypothesis(
        id="cycle",
        visits=[_visit_with_trajectory()],
        current_node="A",
        status=HypothesisStatus.ALIVE,
    )
    params = RoutingParams(
        max_speed=2.0,
        reachable_heading_kappa=0.0,
        reachable_max_states=64,
    )
    first = reachable_area(
        hypothesis, graph, params, T0 + timedelta(seconds=120)
    )
    second = reachable_area(
        hypothesis, graph, params, T0 + timedelta(seconds=120)
    )
    assert first == second
    segments, nodes = first
    assert {item.edge_id for item in segments} == {
        "cycle-a-b",
        "cycle-b-c",
        "cycle-c-a",
    }
    assert len(nodes) == 1
    assert sum(item.probability for item in nodes) == pytest.approx(1.0)


def test_corridor_validation_checks_width_not_only_centreline() -> None:
    floor = MapFloor(
        id="1F",
        size_px=(100, 100),
        m_per_px=0.1,
        walkable_polygons=[[(0, 0), (100, 0), (100, 100), (0, 100)]],
        blocked_polygons=[[(40, 40), (60, 40), (60, 60), (40, 60)]],
    )
    validate_corridor_geometry(
        "safe corridor", [(10, 20), (90, 20)], 1.0, floor
    )
    with pytest.raises(ValueError, match="crosses a blocked area"):
        validate_corridor_geometry(
            "blocked corridor", [(10, 50), (90, 50)], 1.0, floor
        )
    with pytest.raises(ValueError, match="leaves the walkable area"):
        validate_corridor_geometry(
            "too wide corridor", [(10, 2), (90, 2)], 1.0, floor
        )


def test_reentry_replaces_old_reachable_origin_and_area() -> None:
    graph = _graph()
    params = RoutingParams(max_speed=2.0)
    initial = Hypothesis(
        id="initial",
        visits=[_visit_with_trajectory()],
        current_node="A",
        status=HypothesisStatus.ALIVE,
    )
    from javes_contracts import SearchState

    initial_state = SearchState(
        case_id="case",
        site_version=graph.version,
        params_version=params.version,
        hypotheses=[initial],
    )
    before = compute_estimate(
        [initial], graph, T0 + timedelta(seconds=15), initial_state, params
    )
    assert before.reachable_segments
    assert before.reachable_origin is not None

    reentry_point = ProjectedTrajectoryPoint(
        timestamp=T0 + timedelta(seconds=15),
        x=126,
        y=48,
        floor_id="1F",
        calibration_error_m=0.1,
    )
    reentry = Visit(
        visit_id="vB-reentry",
        camera_id="B",
        t_in=reentry_point.timestamp,
        t_out=reentry_point.timestamp,
        tracklet_ids=["track-B-reentry"],
        appearance_score=0.9,
        projected_trajectory=[reentry_point],
    )
    updated = initial.model_copy(
        update={"visits": [*initial.visits, reentry], "current_node": "B"},
        deep=True,
    )
    updated_state = initial_state.model_copy(
        update={"hypotheses": [updated]}, deep=True
    )
    after = compute_estimate(
        [updated], graph, reentry_point.timestamp, updated_state, params
    )
    assert after.reachable_segments == []
    assert after.reachable_nodes == []
    assert after.reachable_origin is not None
    assert (after.reachable_origin.x, after.reachable_origin.y) == (126.0, 48.0)
    assert after.reachable_origin != before.reachable_origin


def test_reachable_area_snaps_to_edge_instead_of_nearest_node() -> None:
    graph = _graph()
    assert graph.walk_graph is not None
    edge = graph.walk_graph.edges[0].model_copy(update={"bidirectional": True})
    graph = graph.model_copy(
        update={
            "walk_graph": WalkGraphDef(
                nodes=graph.walk_graph.nodes,
                edges=[edge],
            )
        }
    )
    visit = _visit_with_trajectory().model_copy(
        update={
            "projected_trajectory": [
                ProjectedTrajectoryPoint(
                    timestamp=T0,
                    x=76,
                    y=48,
                    floor_id="1F",
                    calibration_error_m=0.1,
                )
            ],
            "t_in": T0,
            "t_out": T0,
        },
        deep=True,
    )
    hypothesis = Hypothesis(
        id="mid-edge",
        visits=[visit],
        current_node="A",
        status=HypothesisStatus.ALIVE,
    )
    segments, nodes = reachable_area(
        hypothesis,
        graph,
        RoutingParams(max_speed=1.0, reachable_heading_kappa=0.0),
        T0 + timedelta(seconds=2),
    )
    assert {item.edge_id for item in segments} == {
        "walk-A-B",
        "walk-A-B::reverse",
    }
    assert all(item.points[0] == (76.0, 48.0) for item in segments)
    assert {item.node_id for item in nodes} == {"wa", "wb"}
    assert sum(item.probability for item in nodes) == pytest.approx(1.0)


def test_impossible_walk_snap_returns_no_false_geometry() -> None:
    graph = _graph()
    visit = _visit_with_trajectory().model_copy(
        update={
            "projected_trajectory": [
                ProjectedTrajectoryPoint(
                    timestamp=T0,
                    x=900,
                    y=700,
                    floor_id="1F",
                    calibration_error_m=0.1,
                )
            ],
            "t_in": T0,
            "t_out": T0,
        },
        deep=True,
    )
    hypothesis = Hypothesis(
        id="off-walk",
        visits=[visit],
        current_node="A",
        status=HypothesisStatus.ALIVE,
    )
    from javes_contracts import SearchState

    state = SearchState(
        case_id="case",
        site_version=graph.version,
        params_version="1",
        hypotheses=[hypothesis],
    )
    estimate = compute_estimate(
        [hypothesis],
        graph,
        T0 + timedelta(seconds=10),
        state,
        RoutingParams(max_walk_snap_m=1.0),
    )
    assert estimate.reachable_segments == []
    assert estimate.reachable_nodes == []
    assert estimate.reachable_origin is not None
    assert estimate.reachable_method_version == "relative-timegraph-v1"


def test_open_observation_and_closed_summary_suppress_reachable_area() -> None:
    graph = _graph()
    params = RoutingParams(max_speed=2.0)
    open_visit = _visit_with_trajectory().model_copy(
        update={"open": True}, deep=True
    )
    open_hypothesis = Hypothesis(
        id="open",
        visits=[open_visit],
        current_node="A",
        status=HypothesisStatus.ALIVE,
    )
    from javes_contracts import SearchState

    active_state = SearchState(
        case_id="case",
        site_version=graph.version,
        params_version=params.version,
        hypotheses=[open_hypothesis],
    )
    open_estimate = compute_estimate(
        [open_hypothesis], graph, T0 + timedelta(seconds=20), active_state, params
    )
    assert open_estimate.reachable_segments == []
    assert open_estimate.reachable_nodes == []

    closed_visit = open_visit.model_copy(update={"open": False}, deep=True)
    closed_hypothesis = open_hypothesis.model_copy(
        update={"visits": [closed_visit]}, deep=True
    )
    closed_state = active_state.model_copy(
        update={
            "hypotheses": [closed_hypothesis],
            "done": True,
            "stop_reason": StopReason.ALL_ENDED,
            "mode": SearchMode.PAST_SUMMARY,
        },
        deep=True,
    )
    closed_estimate = compute_estimate(
        [closed_hypothesis],
        graph,
        T0 + timedelta(seconds=20),
        closed_state,
        params,
    )
    assert closed_estimate.reachable_segments == []
    assert closed_estimate.reachable_nodes == []


def test_max_gap_keeps_final_reachable_snapshot() -> None:
    graph = _graph()
    params = RoutingParams(max_gap_s=20, max_speed=2.0)
    visit = _visit_with_trajectory().model_copy(
        update={"t_out": T0}, deep=True
    )
    hypothesis = Hypothesis(
        id="h",
        visits=[visit],
        current_node="A",
        status=HypothesisStatus.ALIVE,
    )
    from javes_contracts import SearchState

    state = SearchState(
        case_id="case",
        site_version=graph.version,
        params_version=params.version,
        hypotheses=[hypothesis],
        search_frontier=SearchFrontier(
            last_observed_at=T0,
            reached_at=T0 + timedelta(seconds=20),
        ),
    )
    result = step(
        state,
        [],
        graph,
        Anchors(),
        params,
        T0 + timedelta(seconds=20),
    )
    assert result.done and result.stop_reason.value == "MAX_GAP"
    assert result.estimate is not None
    assert result.estimate.calculated_at == T0 + timedelta(seconds=20)
    assert result.estimate.reachable_segments
    assert result.estimate.reachable_nodes


def test_svg_has_stable_observed_inferred_alternative_and_reachable_layers() -> None:
    graph = _graph()
    assert graph.walk_graph is not None
    graph = graph.model_copy(
        update={
            "walk_graph": graph.walk_graph.model_copy(
                update={
                    "edges": [
                        *graph.walk_graph.edges,
                        WalkEdgeDef(
                            id="walk-A-B-visual-alt",
                            source="wa",
                            target="wb",
                            floor_id="1F",
                            map_path=[(26, 148)],
                            d_m=14,
                        ),
                    ]
                }
            )
        }
    )
    first = _visit_with_trajectory()
    second = Visit(
        visit_id="vB",
        camera_id="B",
        t_in=T0 + timedelta(seconds=10),
        t_out=T0 + timedelta(seconds=12),
        appearance_score=0.9,
        projected_trajectory=[
            ProjectedTrajectoryPoint(
                timestamp=T0 + timedelta(seconds=10),
                x=300,
                y=40,
                floor_id="1F",
                calibration_error_m=0.1,
            )
        ],
        trajectory_approximate=False,
    )
    hypothesis = Hypothesis(
        id="h",
        visits=[first, second],
        transitions=[
            Transition(
                from_visit_id="vA",
                to_visit_id="vB",
                edge_ids=["A-B"],
                alternative_edge_paths=[["A-C"]],
                geometry=TransitionGeometry(
                    floor_id="1F",
                    points=[(26, 48), (126, 48)],
                    alternative_paths=[[(26, 48), (26, 148), (126, 48)]],
                    walk_edge_ids=["walk-A-B"],
                    alternative_walk_edge_paths=[["walk-A-B-visual-alt"]],
                    end_offset_m=17.418,
                    distance_min_m=9.5,
                    distance_max_m=10.5,
                ),
            )
        ],
        current_node="B",
        status=HypothesisStatus.ALIVE,
    )
    estimate = Estimate(
        zones=[],
        reachable_segments=[
            ReachableSegment(
                edge_id="walk-B-blind",
                source="wb",
                target="wblind",
                probability=1.0,
                floor_id="1F",
                points=[(300, 40), (400, 40)],
            )
        ],
        reachable_nodes=[ReachableNode(node_id="blind", probability=1.0)],
        calculated_at=T0 + timedelta(seconds=12),
    )
    from javes_contracts import SearchState

    state = SearchState(
        case_id="case",
        site_version=graph.version,
        params_version="1",
        hypotheses=[hypothesis],
        mode=SearchMode.CATCHING_UP,
    )
    result = StepResult(
        state=state,
        probes=[],
        hypotheses=[hypothesis],
        best=hypothesis,
        estimate=estimate,
        mode=SearchMode.CATCHING_UP,
    )
    first_bundle = render_route_maps(result, graph, {})
    second_bundle = render_route_maps(result, graph, {})
    first_svg = first_bundle.maps[0].svg
    second_svg = second_bundle.maps[0].svg
    assert first_svg == second_svg
    assert first_bundle.presentation is not None
    assert first_bundle.presentation.position_state == PositionState.LOST
    assert first_bundle.presentation.shown_layers == [
        RouteLayer.CAMERAS,
        RouteLayer.INFERRED,
        RouteLayer.LAST_POSITION,
        RouteLayer.OBSERVED,
        RouteLayer.SEARCH_AREA,
    ]
    ET.fromstring(first_svg)
    for role in (
        "best-route",
        "observed-path",
        "inferred-path",
        "alternative-path",
        "reachable-area",
        "legend",
    ):
        assert f'data-role="{role}"' in first_svg
    assert "Possible area after last sighting" in first_svg
    assert "possible area as of 2026-11-06 04:00:12 UTC" in first_svg
    assert 'data-role="estimate"' not in first_svg
    visible_text = " ".join(
        text.strip()
        for text in ET.fromstring(first_svg).itertext()
        if text and text.strip()
    )
    assert "%" not in visible_text

    final_state = state.model_copy(
        update={"done": True, "stop_reason": StopReason.MAX_GAP},
        deep=True,
    )
    final_svg = render_route_maps(
        result.model_copy(
            update={
                "state": final_state,
                "done": True,
                "stop_reason": StopReason.MAX_GAP,
            },
            deep=True,
        ),
        graph,
        {},
    ).maps[0].svg
    assert "Possible area when search stopped" in final_svg
    assert "Possible area after last sighting" not in final_svg


def test_svg_never_invents_route_from_camera_markers() -> None:
    graph = _graph()
    first = _visit_with_trajectory()
    second = Visit(
        visit_id="vB",
        camera_id="B",
        t_in=T0 + timedelta(seconds=10),
        t_out=T0 + timedelta(seconds=12),
        appearance_score=0.9,
    )
    hypothesis = Hypothesis(
        id="h",
        visits=[first, second],
        transitions=[
            Transition(from_visit_id="vA", to_visit_id="vB", edge_ids=["A-B"])
        ],
        current_node="B",
        status=HypothesisStatus.ALIVE,
    )
    from javes_contracts import SearchState

    state = SearchState(
        case_id="case",
        site_version=graph.version,
        params_version="1",
        hypotheses=[hypothesis],
    )
    svg = render_route_maps(
        StepResult(
            state=state,
            probes=[],
            hypotheses=[hypothesis],
            best=hypothesis,
            mode=SearchMode.CATCHING_UP,
        ),
        graph,
        {},
    ).maps[0].svg
    assert 'data-role="inferred-path"' not in svg
    assert 'points="20.00,40.00 150.00,40.00 300.00,40.00"' not in svg
    assert "Estimated between sightings" not in svg
    assert "Possible area after last sighting" not in svg
    assert "A · video evidence" in svg
    assert "B · video evidence" in svg
    assert "C ·" not in svg
