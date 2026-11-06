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
    Visit,
)
from javes_routing.core.params import RoutingParams
from javes_routing.core.expand import expand_hypotheses
from javes_routing.core.render import render_route_maps
from javes_routing.core.step import init_state
from javes_routing.core.update import (
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
