from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import pytest

from javes_contracts import (
    Anchors,
    CameraCalibration,
    CameraDef,
    CaseInfo,
    Candidate,
    Hypothesis,
    HypothesisStatus,
    MapDef,
    MapFloor,
    MapPosition,
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
    Transition,
    TransitionGeometry,
    Visit,
    WalkEdgeDef,
    WalkGraphDef,
    WalkNodeDef,
)
from javes_routing.core.params import RoutingParams
from javes_routing.core.expand import expand_hypotheses
from javes_routing.core.render import render_route_maps
from javes_routing.core.step import init_state
from javes_routing.core.update import (
    _branch_with_out_of_order_visit,
    _transition_from_gate,
    _with_observed_gap_transitions,
    fuse_observed_segments,
    gate_visit_details,
    update_hypotheses,
)


UTC = timezone.utc
T0 = datetime(2026, 11, 6, 5, 0, tzinfo=UTC)


def _graph() -> SiteGraph:
    calibration = CameraCalibration(
        frame_size_px=(500, 400),
        homography=((1, 0, 0), (0, 1, 0), (0, 0, 1)),
        floor_id="1F",
        error_m=0.1,
    )
    return SiteGraph(
        site="overlap",
        version="v1",
        map=MapDef(
            floors=[MapFloor(id="1F", size_px=(500, 400), m_per_px=0.1)]
        ),
        cameras={
            "A": CameraDef(
                id="A",
                floor="1F",
                map_xy=(20, 20),
                overlap_camera_ids=["B"],
                calibration=calibration,
            ),
            "B": CameraDef(
                id="B",
                floor="1F",
                map_xy=(480, 20),
                overlap_camera_ids=["A"],
                calibration=calibration,
            ),
        },
        edges=[],
    )


def _graph_with_walkway() -> SiteGraph:
    return _graph().model_copy(
        update={
            "walk_graph": WalkGraphDef(
                nodes={
                    "gap-west": WalkNodeDef(
                        id="gap-west", floor_id="1F", map_xy=(110, 100)
                    ),
                    "gap-east": WalkNodeDef(
                        id="gap-east", floor_id="1F", map_xy=(130, 100)
                    ),
                },
                edges=[
                    WalkEdgeDef(
                        id="safe-gap-corridor",
                        source="gap-west",
                        target="gap-east",
                        floor_id="1F",
                        bidirectional=True,
                    )
                ],
            )
        }
    )


def _visit(
    camera: str,
    tracklet_id: str,
    *,
    offset_x: float = 0.0,
) -> Visit:
    return Visit(
        visit_id=f"v-{camera}",
        camera_id=camera,
        t_in=T0,
        t_out=T0 + timedelta(seconds=2),
        tracklet_ids=[tracklet_id],
        appearance_score=0.9,
        projected_trajectory=[
            ProjectedTrajectoryPoint(
                timestamp=T0,
                x=100 + offset_x,
                y=100,
                floor_id="1F",
                calibration_error_m=0.1,
            ),
            ProjectedTrajectoryPoint(
                timestamp=T0 + timedelta(seconds=1),
                x=110 + offset_x,
                y=100,
                floor_id="1F",
                calibration_error_m=0.1,
            ),
        ],
        trajectory_approximate=False,
    )


def test_overlapping_cameras_fuse_into_one_observed_segment() -> None:
    graph = _graph()
    params = RoutingParams()
    first = _visit("A", "track-A")
    second = _visit("B", "track-B", offset_x=1.0)

    gate = gate_visit_details(first, second, graph, params, T0)
    assert gate.reject_reason is None
    assert gate.kind == "OVERLAP"
    assert gate.edge_ids == ()

    hypothesis = Hypothesis(
        id="h",
        visits=[first, second],
        current_node="B",
        status=HypothesisStatus.ALIVE,
    )
    segments = fuse_observed_segments(hypothesis, graph, params)
    assert len(segments) == 1
    assert segments[0].camera_ids == ["A", "B"]
    assert segments[0].tracklet_ids == ["track-A", "track-B"]
    assert len(segments[0].points) == 2
    assert segments[0].points[0].x == pytest.approx(100.5)
    assert segments[0].points[0].source_camera_ids == ["A", "B"]


def test_spatially_conflicting_synchronous_sightings_do_not_attach() -> None:
    gate = gate_visit_details(
        _visit("A", "track-A"),
        _visit("B", "track-B", offset_x=100.0),
        _graph(),
        RoutingParams(),
        T0,
    )
    assert gate.reject_reason is not None
    assert "overlap conflict" in gate.reject_reason
    assert gate.gate_margin_s is not None and gate.gate_margin_s < 0


def test_camera_only_last_known_accepts_declared_overlap_evidence() -> None:
    anchor = Visit(
        visit_id="anchor",
        camera_id="A",
        t_in=T0,
        t_out=T0,
        appearance_score=0.5,
        source="LAST_KNOWN",
    )

    gate = gate_visit_details(
        anchor,
        _visit("B", "track-B", offset_x=1.0),
        _graph(),
        RoutingParams(),
        T0 + timedelta(seconds=2),
    )

    assert gate.reject_reason is None
    assert gate.kind == "OVERLAP"


def test_same_camera_reappearance_has_a_physical_speed_gate() -> None:
    first = _visit("A", "track-A")
    impossible = _visit("A", "track-fast").model_copy(
        update={
            "id": "v-fast",
            "t_in": T0 + timedelta(seconds=3),
            "t_out": T0 + timedelta(seconds=4),
            "projected_trajectory": [
                ProjectedTrajectoryPoint(
                    timestamp=T0 + timedelta(seconds=3),
                    x=310,
                    y=100,
                    floor_id="1F",
                    calibration_error_m=0.1,
                )
            ],
        }
    )

    rejected = gate_visit_details(
        first,
        impossible,
        _graph(),
        RoutingParams(gate_speed=10.0, max_speed=2.0),
        T0 + timedelta(seconds=4),
    )
    assert rejected.reject_reason is not None
    assert "same-camera movement requires" in rejected.reject_reason
    assert rejected.gate_margin_s is not None and rejected.gate_margin_s < 0

    plausible = impossible.model_copy(
        update={
            "id": "v-plausible",
            "projected_trajectory": [
                impossible.projected_trajectory[0].model_copy(update={"x": 120})
            ],
        }
    )
    accepted = gate_visit_details(
        first,
        plausible,
        _graph(),
        RoutingParams(gate_speed=10.0, max_speed=2.0),
        T0 + timedelta(seconds=4),
    )
    assert accepted.reject_reason is None
    assert accepted.kind == "OVERLAP"


def test_unobserved_time_gap_splits_the_drawn_observed_path() -> None:
    first = _visit("A", "track-A")
    later = _visit("A", "track-later").model_copy(
        update={
            "id": "v-later",
            "t_in": T0 + timedelta(seconds=5),
            "t_out": T0 + timedelta(seconds=7),
            "projected_trajectory": [
                point.model_copy(
                    update={"timestamp": point.timestamp + timedelta(seconds=5)}
                )
                for point in first.projected_trajectory
            ],
        }
    )
    hypothesis = Hypothesis(
        id="h-gap",
        visits=[first, later],
        current_node="A",
        status=HypothesisStatus.ALIVE,
    )

    segments = fuse_observed_segments(
        hypothesis,
        _graph(),
        RoutingParams(observed_gap_s=2.0),
    )
    assert len(segments) == 2
    assert [item.tracklet_ids for item in segments] == [
        ["track-A"],
        ["track-later"],
    ]


def test_accepted_tracking_gap_uses_only_configured_walkway() -> None:
    graph = _graph_with_walkway()
    params = RoutingParams(observed_gap_s=2.0, gap_bridge_max_s=10.0)
    first = _visit("A", "track-A")
    later = _visit("A", "track-later").model_copy(
        update={
            "id": "v-later",
            "t_in": T0 + timedelta(seconds=6),
            "t_out": T0 + timedelta(seconds=8),
            "projected_trajectory": [
                ProjectedTrajectoryPoint(
                    timestamp=T0 + timedelta(seconds=6),
                    x=130,
                    y=100,
                    floor_id="1F",
                    calibration_error_m=0.1,
                ),
                ProjectedTrajectoryPoint(
                    timestamp=T0 + timedelta(seconds=7),
                    x=140,
                    y=100,
                    floor_id="1F",
                    calibration_error_m=0.1,
                ),
            ],
        }
    )
    gate = gate_visit_details(first, later, graph, params, T0 + timedelta(seconds=8))
    assert gate.reject_reason is None
    assert gate.kind == "GAP"
    transition = _transition_from_gate(first, later, gate, graph, params)
    assert transition.geometry is not None
    assert transition.geometry.source == "WALK_GRAPH"
    assert transition.geometry.approximate
    assert transition.geometry.points == [(110.0, 100.0), (130.0, 100.0)]
    assert transition.geometry.walk_edge_ids == ["safe-gap-corridor"]

    hypothesis = Hypothesis(
        id="h-gap",
        visits=[first, later],
        transitions=[transition],
        current_node="A",
        status=HypothesisStatus.ALIVE,
        observed_segments=fuse_observed_segments(
            Hypothesis(
                id="h-unfused",
                visits=[first, later],
                current_node="A",
                status=HypothesisStatus.ALIVE,
            ),
            graph,
            params,
        ),
    )
    state = SearchState(
        case_id="case",
        site_version=graph.version,
        params_version=params.version,
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
    root = ET.fromstring(svg)
    observed = [
        item for item in root.iter() if item.attrib.get("data-role") == "observed-path"
    ]
    inferred = [
        item for item in root.iter() if item.attrib.get("data-role") == "inferred-path"
    ]
    assert len(observed) == 2
    assert len(inferred) == 1
    assert inferred[0].attrib["data-kind"] == "GAP"
    assert inferred[0].attrib["data-source"] == "WALK_GRAPH"
    assert inferred[0].attrib["data-approximate"] == "true"
    assert inferred[0].attrib["stroke-dasharray"]
    assert "Estimated between sightings" in svg


def test_long_same_camera_gap_does_not_get_a_direct_bridge() -> None:
    graph = _graph_with_walkway()
    params = RoutingParams(observed_gap_s=2.0, gap_bridge_max_s=10.0)
    first = _visit("A", "track-A")
    later = _visit("A", "track-later").model_copy(
        update={
            "id": "v-later",
            "t_in": T0 + timedelta(seconds=20),
            "t_out": T0 + timedelta(seconds=22),
            "projected_trajectory": [
                ProjectedTrajectoryPoint(
                    timestamp=T0 + timedelta(seconds=20),
                    x=130,
                    y=100,
                    floor_id="1F",
                    calibration_error_m=0.1,
                )
            ],
        }
    )
    gate = gate_visit_details(first, later, graph, params, T0 + timedelta(seconds=22))
    assert gate.reject_reason is None
    assert gate.kind == "GAP"
    transition = _transition_from_gate(first, later, gate, graph, params)
    assert transition.geometry is None


def test_gap_inside_one_confirmed_visit_is_rendered_and_round_trips() -> None:
    graph = _graph_with_walkway()
    params = RoutingParams(observed_gap_s=2.0, gap_bridge_max_s=10.0)
    visit = _visit("A", "track-A").model_copy(
        update={
            "t_out": T0 + timedelta(seconds=7),
            "projected_trajectory": [
                ProjectedTrajectoryPoint(
                    timestamp=T0 + timedelta(seconds=offset),
                    x=x,
                    y=100,
                    floor_id="1F",
                    calibration_error_m=0.1,
                )
                for offset, x in ((0, 100), (1, 110), (6, 130), (7, 140))
            ],
        }
    )
    hypothesis = Hypothesis(
        id="h-internal-gap",
        visits=[visit],
        current_node="A",
        status=HypothesisStatus.ALIVE,
    )
    segments = fuse_observed_segments(hypothesis, graph, params)
    bridged = _with_observed_gap_transitions(
        hypothesis.model_copy(update={"observed_segments": segments}),
        segments,
        graph,
        params,
    )

    assert len(bridged.observed_segments) == 2
    assert len(bridged.observed_gaps) == 1
    gap = bridged.observed_gaps[0]
    assert gap.kind == "GAP"
    assert gap.from_visit_id == gap.to_visit_id == visit.id
    assert gap.geometry is not None
    assert gap.geometry.source == "WALK_GRAPH"
    assert gap.geometry.approximate
    assert Hypothesis.model_validate_json(bridged.model_dump_json()) == bridged

    state = SearchState(
        case_id="case",
        site_version=graph.version,
        params_version=params.version,
        hypotheses=[bridged],
    )
    svg = render_route_maps(
        StepResult(
            state=state,
            probes=[],
            hypotheses=[bridged],
            best=bridged,
            mode=SearchMode.CATCHING_UP,
        ),
        graph,
        {},
    ).maps[0].svg
    root = ET.fromstring(svg)
    inferred = [
        item for item in root.iter() if item.attrib.get("data-role") == "inferred-path"
    ]
    assert len(inferred) == 1
    assert inferred[0].attrib["stroke"] == "#d97706"
    assert inferred[0].attrib["stroke-dasharray"]


def test_gap_without_walk_graph_stays_unmapped_and_never_draws_a_line() -> None:
    graph = _graph()
    params = RoutingParams(observed_gap_s=2.0, gap_bridge_max_s=10.0)
    first = _visit("A", "track-A")
    later = _visit("A", "track-later").model_copy(
        update={
            "id": "v-later",
            "t_in": T0 + timedelta(seconds=6),
            "t_out": T0 + timedelta(seconds=8),
            "projected_trajectory": [
                ProjectedTrajectoryPoint(
                    timestamp=T0 + timedelta(seconds=6),
                    x=130,
                    y=100,
                    floor_id="1F",
                    calibration_error_m=0.1,
                )
            ],
        }
    )
    gate = gate_visit_details(first, later, graph, params, T0 + timedelta(seconds=8))
    transition = _transition_from_gate(first, later, gate, graph, params)
    assert transition.kind == "GAP"
    assert transition.geometry is None

    hypothesis = Hypothesis(
        id="h-unmapped-gap",
        visits=[first, later],
        transitions=[transition],
        current_node="A",
        status=HypothesisStatus.ALIVE,
    )
    state = SearchState(
        case_id="case",
        site_version=graph.version,
        params_version=params.version,
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
    assert "Estimated between sightings" not in svg


def test_renderer_rejects_unsupported_endpoint_interpolation_from_saved_state() -> None:
    graph = _graph()
    first = _visit("A", "track-A")
    later = _visit("A", "track-later").model_copy(
        update={
            "id": "v-later",
            "t_in": T0 + timedelta(seconds=6),
            "t_out": T0 + timedelta(seconds=8),
            "projected_trajectory": [
                ProjectedTrajectoryPoint(
                    timestamp=T0 + timedelta(seconds=6),
                    x=130,
                    y=100,
                    floor_id="1F",
                    calibration_error_m=0.1,
                )
            ],
        }
    )
    transition = Transition(
        from_visit_id=first.id,
        to_visit_id=later.id,
        t_from=first.t_out,
        t_to=later.t_in,
        kind="GAP",
        geometry=TransitionGeometry(
            floor_id="1F",
            points=[(110, 100), (130, 100)],
            distance_min_m=1.8,
            distance_max_m=2.2,
            approximate=True,
            source="ENDPOINT_INTERPOLATION",
        ),
    )
    hypothesis = Hypothesis(
        id="h-unsupported-gap",
        visits=[first, later],
        transitions=[transition],
        current_node="A",
        status=HypothesisStatus.ALIVE,
    )
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
    assert "110.00,100.00 130.00,100.00" not in svg


def test_probe_result_builds_overlap_evidence_without_inferred_line() -> None:
    graph = _graph()
    params = RoutingParams()
    first = _visit("A", "track-A")
    hypothesis = Hypothesis(
        id="h-A",
        visits=[first],
        current_node="A",
        status=HypothesisStatus.ALIVE,
    )
    probe = ProbeRequest(
        probe_id="probe-B",
        camera_id="B",
        t_from=T0,
        t_to=T0 + timedelta(seconds=2),
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
        t_start=T0,
        t_end=T0 + timedelta(seconds=2),
        bbox_first=(90, 60, 20, 40),
        bbox_last=(100, 60, 20, 40),
        trajectory=[
            TrajectoryPoint(timestamp=T0, x_px=101, y_px=100),
            TrajectoryPoint(
                timestamp=T0 + timedelta(seconds=1), x_px=111, y_px=100
            ),
        ],
    )
    updated, rejected, updated_state = update_hypotheses(
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
        T0 + timedelta(seconds=3),
    )
    attached = next(item for item in updated if len(item.visits) == 2)
    assert not rejected
    assert attached.transitions[-1].kind == "OVERLAP"
    assert attached.transitions[-1].geometry is None
    assert len(attached.observed_segments) == 1

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
    ET.fromstring(svg)
    assert 'data-camera="A,B"' in svg
    assert 'data-role="observed-path"' in svg
    assert 'data-role="inferred-path"' not in svg
    assert "20.00,20.00 480.00,20.00" not in svg


def test_delayed_overlap_result_keeps_later_existing_visit() -> None:
    graph = _graph()
    params = RoutingParams()
    first = _visit("A", "track-A")
    later = _visit("A", "track-later").model_copy(
        update={
            "id": "v-later",
            "t_in": T0 + timedelta(seconds=5),
            "t_out": T0 + timedelta(seconds=7),
            "projected_trajectory": [
                point.model_copy(
                    update={
                        "timestamp": point.timestamp + timedelta(seconds=5),
                        "x": point.x + 20,
                    }
                )
                for point in first.projected_trajectory
            ],
        }
    )
    existing_gate = gate_visit_details(
        first,
        later,
        graph,
        params,
        T0 + timedelta(seconds=8),
    )
    hypothesis = Hypothesis(
        id="h-existing-late",
        visits=[first, later],
        transitions=[
            _transition_from_gate(first, later, existing_gate, graph, params)
        ],
        current_node="A",
        status=HypothesisStatus.ALIVE,
    )
    delayed = _visit("B", "track-B", offset_x=1.0)

    merged = _branch_with_out_of_order_visit(
        hypothesis,
        delayed,
        graph,
        params,
        T0 + timedelta(seconds=8),
        "ADULT",
    )

    assert merged is not None
    assert [visit.id for visit in merged.visits] == [first.id, delayed.id, later.id]
    assert len(merged.transitions) == 2
    assert merged.transitions[0].kind == "OVERLAP"
    assert merged.transitions[1].kind == "GAP"
    assert merged.transitions[1].geometry is None


def test_delayed_contained_overlap_uses_latest_camera_frontier() -> None:
    graph = _graph()
    params = RoutingParams()
    anchor = Visit(
        visit_id="anchor",
        camera_id="A",
        t_in=T0,
        t_out=T0,
        appearance_score=0.5,
        source="LAST_KNOWN",
    )
    long_overlap = _visit("B", "track-B-long").model_copy(
        update={
            "id": "v-B-long",
            "t_out": T0 + timedelta(seconds=24),
            "projected_trajectory": [
                ProjectedTrajectoryPoint(
                    timestamp=T0 + timedelta(seconds=3),
                    x=103,
                    y=100,
                    floor_id="1F",
                    calibration_error_m=0.1,
                ),
                ProjectedTrajectoryPoint(
                    timestamp=T0 + timedelta(seconds=14),
                    x=114,
                    y=100,
                    floor_id="1F",
                    calibration_error_m=0.1,
                ),
            ],
        }
    )
    later = _visit("B", "track-B-later").model_copy(
        update={
            "id": "v-B-later",
            "t_in": T0 + timedelta(seconds=25.8),
            "t_out": T0 + timedelta(seconds=35.8),
            "projected_trajectory": [
                ProjectedTrajectoryPoint(
                    timestamp=T0 + timedelta(seconds=25.8),
                    x=126,
                    y=100,
                    floor_id="1F",
                    calibration_error_m=0.1,
                )
            ],
        }
    )
    first_gate = gate_visit_details(anchor, long_overlap, graph, params, T0)
    second_gate = gate_visit_details(
        long_overlap, later, graph, params, T0 + timedelta(seconds=36)
    )
    hypothesis = Hypothesis(
        id="h-camera-B-first",
        visits=[anchor, long_overlap, later],
        transitions=[
            _transition_from_gate(anchor, long_overlap, first_gate, graph, params),
            _transition_from_gate(long_overlap, later, second_gate, graph, params),
        ],
        current_node="B",
        status=HypothesisStatus.ALIVE,
    )
    delayed = _visit("A", "track-A-delayed").model_copy(
        update={
            "id": "v-A-delayed",
            "t_in": T0 + timedelta(seconds=2.6),
            "t_out": T0 + timedelta(seconds=15),
            "projected_trajectory": [
                ProjectedTrajectoryPoint(
                    timestamp=T0 + timedelta(seconds=3),
                    x=103,
                    y=100,
                    floor_id="1F",
                    calibration_error_m=0.1,
                ),
                ProjectedTrajectoryPoint(
                    timestamp=T0 + timedelta(seconds=14),
                    x=114,
                    y=100,
                    floor_id="1F",
                    calibration_error_m=0.1,
                ),
            ],
        }
    )

    merged = _branch_with_out_of_order_visit(
        hypothesis,
        delayed,
        graph,
        params,
        T0 + timedelta(seconds=36),
        "ADULT",
    )

    assert merged is not None
    assert [visit.id for visit in merged.visits] == [
        anchor.id,
        delayed.id,
        long_overlap.id,
        later.id,
    ]
    assert [transition.kind for transition in merged.transitions] == [
        "GAP",
        "OVERLAP",
        "OVERLAP",
    ]


def test_last_known_anchor_probes_source_and_overlap_cameras() -> None:
    graph = _graph()
    params = RoutingParams(anchor_probe_window_s=5)
    anchor = _visit("A", "unused").model_copy(
        update={
            "tracklet_ids": [],
            "projected_trajectory": [],
            "trajectory_approximate": True,
            "source": "LAST_KNOWN",
            "t_out": T0,
        }
    )
    hypothesis = Hypothesis(
        id="anchor",
        visits=[anchor],
        current_node="A",
        status=HypothesisStatus.ALIVE,
    )
    state = SearchState(
        case_id="case",
        site_version=graph.version,
        params_version=params.version,
        hypotheses=[hypothesis],
        window_end=T0 + timedelta(seconds=20),
    )
    probes, _ = expand_hypotheses(
        state, graph, params, T0 + timedelta(seconds=10)
    )
    assert {probe.camera for probe in probes} == {"A", "B"}
    assert {probe.reason for probe in probes} == {"ANCHOR_EVIDENCE"}
    assert {probe.t_from for probe in probes} == {T0}
    assert {probe.t_to for probe in probes} == {T0 + timedelta(seconds=5)}


def test_last_known_footpoint_is_projected_into_ground_evidence() -> None:
    graph = _graph()
    params = RoutingParams()
    state = init_state(
        CaseInfo(
            case_id="case",
            target_id="target",
            start_time=T0,
            last_known_camera="A",
            last_known_time=T0,
            last_known_footpoint_px=(101, 100),
        ),
        graph,
        params,
    )
    point = state.hypotheses[0].visits[0].projected_trajectory[0]
    assert (point.x, point.y, point.floor_id) == (101.0, 100.0, "1F")
    assert not state.hypotheses[0].visits[0].trajectory_approximate

    mapped = init_state(
        CaseInfo(
            case_id="mapped",
            target_id="target",
            start_time=T0,
            last_known_camera="A",
            last_known_time=T0,
            last_known_map_position=MapPosition(
                x=120, y=130, floor_id="1F", error_m=0.2
            ),
        ),
        graph,
        params,
    )
    mapped_point = mapped.hypotheses[0].visits[0].projected_trajectory[0]
    assert (mapped_point.x, mapped_point.y) == (120.0, 130.0)
