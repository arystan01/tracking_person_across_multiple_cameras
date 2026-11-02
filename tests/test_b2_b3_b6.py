from __future__ import annotations

from datetime import datetime, timedelta, timezone
import pytest

from routing.contracts import (
    Anchors,
    Candidate,
    CaseInfo,
    Hypothesis,
    HypothesisStatus,
    ProbeRequest,
    ProbeResult,
    ProbeStatus,
    ProbedWindow,
    RegionDef,
    SearchMode,
    SearchState,
    StopReason,
    Tracklet,
    Visit,
)
from routing.contracts.params import RoutingParams
from routing.sim.generator import generate_ring_graph
from routing.site import parse_site
from routing.step import init_state, step
from routing.update import _bbox_center, _edges_for_bbox, bbox_to_xyxy, gate_visit_to_hypothesis


def test_b2_child_profile() -> None:
    """Create a 40m edge, derive t_min from gate_speed (4.0 m/s -> t_min=10s).

    A child (profile CHILD, tmin_k=0.6) arriving at dt=7s should be accepted
    (scaled t_min = 6s, minus clock_tolerance 3s -> gate at 3s).
    """
    raw_site = {
        "cameras": {
            "cam_0": {"zone": "zone_0", "map_xy": [0.0, 0.0]},
            "cam_1": {"zone": "zone_1", "map_xy": [40.0, 0.0]},
        },
        "edges": [
            {"id": "e_0_to_1", "source": "cam_0", "target": "cam_1", "d_m": 40.0},
        ],
    }
    graph = parse_site(raw_site)
    edge = graph.edges[0]

    # Verify t_min is derived from gate_speed 4.0 m/s -> 10.0s
    assert edge.t_min == 10.0

    params = RoutingParams()
    now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    v1 = Visit(
        id="v1",
        camera="cam_0",
        t_in=now,
        t_out=now + timedelta(seconds=10),
        tracklet_ids=["t1"],
        entry_edges=[],
        exit_edges=[],
        score=0.9,
    )
    # Arrival at dt = 7.0s after v1 ended
    v2 = Visit(
        id="v2",
        camera="cam_1",
        t_in=now + timedelta(seconds=17),
        t_out=now + timedelta(seconds=25),
        tracklet_ids=["t2"],
        entry_edges=[],
        exit_edges=[],
        score=0.9,
    )

    # For CHILD: tmin_k = 0.6 -> scaled t_min = 6.0s, minus clock_tolerance 3s -> gate at 3s
    # Since dt=7s >= 3s, it is accepted!
    trans_lik, via, reason = gate_visit_to_hypothesis(
        v1, v2, graph, params, now + timedelta(seconds=30), profile="CHILD"
    )
    assert reason is None
    assert trans_lik is not None

    # Verify that a child arriving at dt=4s is also accepted (4s >= 3s gate)
    v2_fast = Visit(
        id="v2_fast",
        camera="cam_1",
        t_in=now + timedelta(seconds=14),
        t_out=now + timedelta(seconds=22),
        tracklet_ids=["t2f"],
        entry_edges=[],
        exit_edges=[],
        score=0.9,
    )
    trans_lik_child, _, reason_child = gate_visit_to_hypothesis(
        v1, v2_fast, graph, params, now + timedelta(seconds=30), profile="CHILD"
    )
    assert reason_child is None

    # For ADULT (tmin_k=1.0, scaled t_min=10.0s, gate at 10 - 3 = 7.0s):
    # At dt=4s (14s - 10s), ADULT is rejected by the hard gate!
    trans_lik_adult, _, reason_adult = gate_visit_to_hypothesis(
        v1, v2_fast, graph, params, now + timedelta(seconds=30), profile="ADULT"
    )
    assert reason_adult is not None
    assert "hard gate" in reason_adult


def test_b2_clock_tolerance() -> None:
    """dt slightly below t_min but within clock_tolerance -> not rejected."""
    raw_site = {
        "cameras": {
            "cam_0": {"zone": "zone_0", "map_xy": [0.0, 0.0]},
            "cam_1": {"zone": "zone_1", "map_xy": [10.0, 0.0]},
        },
        "edges": [
            {
                "id": "e_0_to_1",
                "source": "cam_0",
                "target": "cam_1",
                "t_min": 10.0,
                "t_typ": 20.0,
                "t_max": 50.0,
            },
        ],
    }
    graph = parse_site(raw_site)
    params = RoutingParams(clock_tolerance_s=3.0)
    now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    v1 = Visit(
        id="v1",
        camera="cam_0",
        t_in=now,
        t_out=now + timedelta(seconds=10),
        tracklet_ids=["t1"],
        entry_edges=[],
        exit_edges=[],
        score=0.9,
    )

    # dt = 8.0s: below t_min (10.0s), but within clock tolerance (10 - 3 = 7.0s) -> accepted
    v_tol = Visit(
        id="v_tol",
        camera="cam_1",
        t_in=now + timedelta(seconds=18),
        t_out=now + timedelta(seconds=25),
        tracklet_ids=["t_tol"],
        entry_edges=[],
        exit_edges=[],
        score=0.9,
    )
    trans_lik, via, reason = gate_visit_to_hypothesis(
        v1, v_tol, graph, params, now + timedelta(seconds=30), profile="ADULT"
    )
    assert reason is None

    # dt = 6.0s: below t_min - clock_tolerance (7.0s) -> rejected
    v_below = Visit(
        id="v_below",
        camera="cam_1",
        t_in=now + timedelta(seconds=16),
        t_out=now + timedelta(seconds=25),
        tracklet_ids=["t_below"],
        entry_edges=[],
        exit_edges=[],
        score=0.9,
    )
    trans_lik_rej, _, reason_rej = gate_visit_to_hypothesis(
        v1, v_below, graph, params, now + timedelta(seconds=30), profile="ADULT"
    )
    assert reason_rej is not None
    assert "hard gate" in reason_rej


def test_b3_bbox_xywh() -> None:
    """Test bbox_to_xyxy conversion and polygon hit test with xywh box."""
    # 1. Coordinate conversion
    bbox_xywh = (10.0, 20.0, 30.0, 40.0)
    xyxy = bbox_to_xyxy(bbox_xywh)
    assert xyxy == (10.0, 20.0, 40.0, 60.0)

    # Center of bbox
    cx, cy = _bbox_center(bbox_xywh)
    assert cx == 25.0
    assert cy == 40.0

    # 2. Polygon hit test via _edges_for_bbox
    raw_site = {
        "cameras": {
            "cam_0": {
                "zone": "lobby",
                "map_xy": [0.0, 0.0],
                "regions": [
                    {
                        "name": "north_door",
                        "edges": ["e_north"],
                        "polygon": [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)],
                    },
                    {
                        "name": "south_door",
                        "edges": ["e_south"],
                        "polygon": [(100.0, 0.0), (200.0, 0.0), (200.0, 100.0), (100.0, 100.0)],
                    },
                ],
            },
            "cam_n": {"zone": "north", "map_xy": [50.0, 150.0]},
            "cam_s": {"zone": "south", "map_xy": [150.0, 150.0]},
        },
        "edges": [
            {"id": "e_north", "source": "cam_0", "target": "cam_n", "t_min": 5.0, "t_typ": 10.0, "t_max": 20.0},
            {"id": "e_south", "source": "cam_0", "target": "cam_s", "t_min": 5.0, "t_typ": 10.0, "t_max": 20.0},
        ],
    }
    graph = parse_site(raw_site)

    # Box located in north_door region: xywh = (20, 20, 20, 20) -> center (30, 30)
    box_north = (20.0, 20.0, 20.0, 20.0)
    edges_north = _edges_for_bbox("cam_0", box_north, graph)
    assert edges_north == ["e_north"]

    # Box located in south_door region: xywh = (120, 20, 20, 20) -> center (130, 30)
    box_south = (120.0, 20.0, 20.0, 20.0)
    edges_south = _edges_for_bbox("cam_0", box_south, graph)
    assert edges_south == ["e_south"]

    # Box located outside defined regions -> returns all camera edges
    box_outside = (500.0, 500.0, 20.0, 20.0)
    edges_outside = _edges_for_bbox("cam_0", box_outside, graph)
    assert set(edges_outside) == {"e_north", "e_south"}


def test_b6_empty_call_triggers_dormant() -> None:
    """Call step() with empty results at a later now -> triggers mode changes."""
    graph = generate_ring_graph(3, with_outside=True)
    t0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    case = CaseInfo(case_id="case_b6", target_id="target_b6", start_time=t0)
    params = RoutingParams(max_gap_s=2400.0)
    state = init_state(case, graph, params)

    # Place an ALIVE hypothesis at cam_0 (which connects to OUTSIDE)
    v1 = Visit(
        id="v1",
        camera="cam_0",
        t_in=t0,
        t_out=t0 + timedelta(seconds=10),
        tracklet_ids=["t1"],
        entry_edges=[],
        exit_edges=[],
        score=0.9,
    )
    h = Hypothesis(
        id="h1",
        visits=[v1],
        current_node="cam_0",
        status=HypothesisStatus.ALIVE,
        score=0.0,
    )
    state.hypotheses = [h]

    # Pre-record completed probes for all outgoing camera edges from cam_0
    for edge in graph.get_edges_from("cam_0"):
        if edge.target in graph.cameras:
            state.probed_windows.append(
                ProbedWindow(
                    camera=edge.target,
                    t_from=t0,
                    t_to=t0 + timedelta(seconds=edge.t_max + 10),
                    probe_id=f"probe_{edge.target}",
                    status=ProbeStatus.DONE,
                )
            )

    # Initial mode is CATCHING_UP
    assert state.mode == SearchMode.CATCHING_UP

    # Step at a later time (after all outgoing camera edge windows have passed)
    now_later = t0 + timedelta(seconds=300)
    res = step(state, [], graph, Anchors(), params, now_later)

    # Hypothesis should transition to DORMANT at OUTSIDE
    dormant_hyp = res.hypotheses[0]
    assert dormant_hyp.status == HypothesisStatus.DORMANT
    assert dormant_hyp.current_node == "OUTSIDE"

    # Mode should change to WAITING
    assert res.mode == SearchMode.WAITING


def test_b6_idempotent_same_now() -> None:
    """Two calls with same now give identical output."""
    graph = generate_ring_graph(4, with_outside=True)
    t0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    case = CaseInfo(case_id="case_idem", target_id="tgt_idem", start_time=t0, profile="ADULT")
    params = RoutingParams()
    state = init_state(case, graph, params)

    v1 = Visit(
        id="v1",
        camera="cam_0",
        t_in=t0,
        t_out=t0 + timedelta(seconds=10),
        tracklet_ids=["t1"],
        entry_edges=[],
        exit_edges=[],
        score=0.9,
    )
    h = Hypothesis(
        id="h1",
        visits=[v1],
        current_node="cam_0",
        status=HypothesisStatus.ALIVE,
        score=0.0,
    )
    state.hypotheses = [h]

    now = t0 + timedelta(seconds=30)

    # Two empty calls at same now
    res1 = step(state, [], graph, Anchors(), params, now)
    res2 = step(state, [], graph, Anchors(), params, now)

    assert res1.model_dump_json() == res2.model_dump_json()
    assert res1.mode == res2.mode
    assert len(res1.probes) == len(res2.probes)
    for p1, p2 in zip(res1.probes, res2.probes):
        assert p1.probe_id == p2.probe_id
        assert p1.camera == p2.camera
        assert p1.t_from == p2.t_from
        assert p1.t_to == p2.t_to
