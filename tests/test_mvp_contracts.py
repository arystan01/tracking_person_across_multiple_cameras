from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from javes_contracts import (
    Anchors,
    CameraDef,
    BlindNodeDef,
    Candidate,
    CaseInfo,
    EdgeDef,
    Estimate,
    Hypothesis,
    HypothesisStatus,
    MapDef,
    MapFloor,
    ProbeRequest,
    ProbeResult,
    ProbeStatus,
    ProbedWindow,
    ProjectedTrajectoryPoint,
    SearchFrontier,
    SearchMode,
    SearchState,
    SiteGraph,
    StepResult,
    Tracklet,
    Transition,
    TransitionGeometry,
    Visit,
    WalkEdgeDef,
    WalkGraphDef,
    WalkNodeDef,
)
from javes_routing.core.params import RoutingParams
from javes_routing.core.expand import expand_dormant_watch_probes
from javes_routing.core.render import render_route_maps
from javes_routing.core.facade import step_and_render
from javes_routing.service.demo import _write_viewer
from javes_routing.core.sim.generator import generate_ring_graph
from javes_routing.core.site import parse_site
from javes_routing.core.step import init_state, step
from javes_routing.core.update import gate_visit_to_hypothesis


UTC = timezone.utc
T0 = datetime(2026, 10, 16, 5, 0, tzinfo=UTC)


def _tracklet(tracklet_id: str, camera: str, start: datetime, *, open: bool = False) -> Tracklet:
    return Tracklet(
        tracklet_id=tracklet_id,
        camera_id=camera,
        t_start=start,
        t_end=start + timedelta(seconds=10),
        bbox_first=[0, 0, 20, 40],
        bbox_last=[5, 0, 20, 40],
        open=open,
    )


def test_canonical_wire_names_open_and_aware_time() -> None:
    tracklet = _tracklet("t1", "A", T0)
    payload = tracklet.model_dump(mode="json")
    assert payload["tracklet_id"] == "t1"
    assert payload["camera_id"] == "A"
    assert "id" not in payload and "camera" not in payload

    probe = ProbeRequest(
        probe_id="p1",
        camera_id="A",
        t_from=T0,
        t_to="OPEN",
        reason="LIVE_CURRENT",
        hypothesis_ids=["h2", "h1", "h2"],
    )
    wire = json.loads(probe.model_dump_json())
    assert wire["t_to"] == "OPEN"
    assert wire["camera_id"] == "A"
    assert wire["hypothesis_ids"] == ["h1", "h2"]
    assert ProbeRequest.model_validate_json(probe.model_dump_json()) == probe

    with pytest.raises(ValidationError, match="timezone-aware"):
        _tracklet("bad", "A", datetime(2026, 1, 1))


def test_canonical_site_and_nested_case_request() -> None:
    raw = {
        "site": "demo",
        "version": "2026-10-16",
        "timezone": "Asia/Seoul",
        "map": {"floors": [{"id": "1F", "image": "1f.png", "size_px": [400, 300]}]},
        "cameras": {
            "A": {"zone": "lobby", "floor": "1F", "map_xy": [10, 20], "regions": {"east": [[0, 0], [1, 0], [1, 1]]}},
            "B": {"zone": "hall", "floor": "1F", "map_xy": [300, 20]},
        },
        "edges": [{"id": "A-B", "from": "A", "via": "east", "to": "B", "t_min": 5, "t_typ": 10, "t_max": 20, "map_path": [[10, 20], [150, 40], [300, 20]]}],
        "blind_nodes": [{"id": "room", "floor": "1F", "map_xy": [200, 200], "dwell": {"median_s": 30, "sigma": 0.8}}],
    }
    graph = parse_site(raw)
    assert graph.edges[0].source == "A"
    assert graph.edges[0].map_path[1] == (150.0, 40.0)
    assert graph.blind_nodes[0].dwell_median_s == 30
    restored = SiteGraph.model_validate_json(graph.model_dump_json())
    assert restored == graph

    case = CaseInfo.model_validate(
        {
            "case_id": "c1",
            "target_id": "target",
            "window": {"start": T0.isoformat(), "end": (T0 + timedelta(minutes=10)).isoformat()},
            "last_known": {"camera": "A", "time": T0.isoformat()},
        }
    )
    state = init_state(case, graph, RoutingParams())
    assert state.hypotheses[0].visits[0].source == "LAST_KNOWN"
    assert state.hypotheses[0].current_node == "A"


def test_retry_limit_and_unobserved_window() -> None:
    graph = generate_ring_graph(3)
    params = RoutingParams(max_retries=2)
    state = init_state(CaseInfo(case_id="c", target_id="t", start_time=T0), graph, params)
    original = ProbeRequest(
        probe_id="p0", camera_id="cam_0", t_from=T0, t_to=T0 + timedelta(seconds=30)
    )
    state.pending_probes = [original]

    first = step(state, [ProbeResult(probe_id="p0", status="FAILED")], graph, Anchors(), params, T0)
    assert first.probes[0].retry_count == 1
    retry_one = first.probes[0]
    second = step(first.state, [ProbeResult(probe_id=retry_one.probe_id, status="TIMEOUT")], graph, Anchors(), params, T0)
    assert second.probes[0].retry_count == 2
    retry_two = second.probes[0]
    third = step(second.state, [ProbeResult(probe_id=retry_two.probe_id, status="FAILED")], graph, Anchors(), params, T0)
    assert all(item.retry_of != "p0" for item in third.probes)
    assert third.estimate and third.estimate.unobserved_windows[0]["reason"] == "FAILED"


def test_terminal_unavailable_open_watch_is_not_reissued() -> None:
    graph = SiteGraph(
        site="watch",
        version="v1",
        cameras={"A": CameraDef(id="A")},
        edges=[],
        blind_nodes=[BlindNodeDef(id="blind", borders=["A"])],
    )
    visit = Visit(
        visit_id="v1",
        camera_id="A",
        t_in=T0,
        t_out=T0,
        score=0.9,
    )
    state = SearchState(
        case_id="case",
        site_version="v1",
        params_version="1",
        hypotheses=[
            Hypothesis(
                id="h1",
                visits=[visit],
                current_node="blind",
                status=HypothesisStatus.DORMANT,
            )
        ],
        probed_windows=[
            ProbedWindow(
                camera="A",
                t_from=T0,
                t_to=None,
                probe_id="watch-A",
                status=ProbeStatus.UNAVAILABLE,
                coverage_ratio=0.0,
                found=False,
            )
        ],
    )

    assert expand_dormant_watch_probes(
        state, graph, RoutingParams(), T0 + timedelta(seconds=30)
    ) == []


def test_non_empty_step_is_byte_deterministic_and_does_not_mutate_input() -> None:
    graph = generate_ring_graph(3)
    params = RoutingParams()
    state = init_state(CaseInfo(case_id="c", target_id="t", start_time=T0), graph, params)
    state.pending_probes = [
        ProbeRequest(probe_id="p0", camera_id="cam_0", t_from=T0, t_to=T0 + timedelta(seconds=30))
    ]
    before = state.model_dump_json()
    result = ProbeResult(
        probe_id="p0",
        status="DONE",
        tracklets=[_tracklet("t1", "cam_0", T0)],
        candidates=[Candidate(candidate_id="cand1", tracklet_id="t1", appearance_score=0.8)],
    )
    first = step(state, [result], graph, Anchors(), params, T0 + timedelta(seconds=30))
    second = step(state, [result], graph, Anchors(), params, T0 + timedelta(seconds=30))
    assert first.model_dump_json() == second.model_dump_json()
    assert state.model_dump_json() == before
    assert first.state == type(first.state).model_validate_json(first.state.model_dump_json())


def test_long_gap_requires_confirmation() -> None:
    graph = generate_ring_graph(3, with_outside=True)
    params = RoutingParams()
    state = init_state(CaseInfo(case_id="c", target_id="t", start_time=T0), graph, params)
    first_visit = Visit(
        id="v0", camera="cam_0", t_in=T0, t_out=T0 + timedelta(seconds=10),
        tracklet_ids=["old"], score=0.9,
    )
    state.hypotheses = [
        Hypothesis(id="h0", visits=[first_visit], current_node="OUTSIDE", status="DORMANT")
    ]
    sighting_time = T0 + timedelta(minutes=27)
    result = ProbeResult(
        probe_id="external",
        status="DONE",
        tracklets=[_tracklet("reentry", "cam_1", sighting_time)],
        candidates=[Candidate(candidate_id="c-reentry", tracklet_id="reentry", appearance_score=0.9)],
    )
    candidate = step(state, [result], graph, Anchors(), params, sighting_time + timedelta(seconds=10))
    long_gap = next(
        item for item in candidate.hypotheses
        if any("LONG_GAP" in visit.flags for visit in item.visits)
    )
    assert long_gap.requires_confirmation
    assert candidate.best.id != long_gap.id

    confirmed = step(
        candidate.state, [], graph, Anchors(confirm_tracklet_ids=["reentry"]), params,
        sighting_time + timedelta(seconds=11),
    )
    assert len(confirmed.hypotheses) == 1
    assert not confirmed.hypotheses[0].requires_confirmation

    closed_state = candidate.state.model_copy(
        update={
            "done": True,
            "mode": SearchMode.PAST_SUMMARY,
            "stop_reason": "MAX_GAP",
        },
        deep=True,
    )
    reviewed_closed = step(
        closed_state,
        [],
        graph,
        Anchors(confirm_tracklet_ids=["reentry"], review_only=True),
        params,
        sighting_time + timedelta(seconds=12),
    )
    assert reviewed_closed.done
    assert reviewed_closed.mode == SearchMode.PAST_SUMMARY
    assert reviewed_closed.stop_reason.value == "MAX_GAP"
    assert reviewed_closed.probes == []
    assert len(reviewed_closed.hypotheses) == 1
    assert not reviewed_closed.hypotheses[0].requires_confirmation


def test_open_upsert_and_explicit_stream_close() -> None:
    graph = generate_ring_graph(3)
    params = RoutingParams()
    state = init_state(CaseInfo(case_id="c", target_id="t", start_time=T0), graph, params)
    state.pending_probes = [ProbeRequest(probe_id="live", camera_id="cam_0", t_from=T0, t_to="OPEN")]
    first_result = ProbeResult(
        probe_id="live", status="DONE", tracklets=[_tracklet("same", "cam_0", T0, open=True)],
        candidates=[Candidate(candidate_id="same-candidate", tracklet_id="same", appearance_score=0.8)],
    )
    first = step(state, [first_result], graph, Anchors(), params, T0 + timedelta(seconds=10))
    assert any(probe.probe_id == "live" for probe in first.state.pending_probes)
    assert first.hypotheses[0].visits[-1].open

    closed_tracklet = _tracklet("same", "cam_0", T0, open=False).model_copy(
        update={"t_out": T0 + timedelta(seconds=20)}
    )
    closed = step(
        first.state,
        [ProbeResult(
            probe_id="live", status="DONE", stream_closed=True,
            tracklets=[closed_tracklet],
            candidates=[Candidate(candidate_id="same-candidate", tracklet_id="same", appearance_score=0.8)],
        )],
        graph, Anchors(), params, T0 + timedelta(seconds=20),
    )
    assert all(probe.probe_id != "live" for probe in closed.state.pending_probes)
    assert not closed.hypotheses[0].visits[-1].open


def test_renderer_is_safe_deterministic_multifloor_svg() -> None:
    graph = SiteGraph(
        site="two-floor",
        version="v1",
        map=MapDef(floors=[
            MapFloor(id="1F", image="one.png", size_px=(400, 300), m_per_px=0.1),
            MapFloor(id="2F", size_px=(500, 350)),
        ]),
        cameras={
            "A": CameraDef(id="A", floor="1F", map_xy=(20, 30)),
            "B": CameraDef(id="B", floor="1F", map_xy=(300, 80)),
            "C": CameraDef(id="C", floor="2F", map_xy=(100, 120)),
        },
        edges=[
            EdgeDef(id="A-B", source="A", target="B", t_min=5, t_typ=10, t_max=20, map_path=[(20, 30), (150, 40), (300, 80)]),
            EdgeDef(id="A-B-alt", source="A", target="B", t_min=6, t_typ=12, t_max=24),
            EdgeDef(id="B-C", source="B", target="C", t_min=20, t_typ=30, t_max=60),
        ],
        walk_graph=WalkGraphDef(
            nodes={
                "wa": WalkNodeDef(id="wa", floor_id="1F", map_xy=(20, 30)),
                "wb": WalkNodeDef(id="wb", floor_id="1F", map_xy=(300, 80)),
            },
            edges=[
                WalkEdgeDef(
                    id="walk-main",
                    source="wa",
                    target="wb",
                    floor_id="1F",
                    map_path=[(150, 40)],
                ),
                WalkEdgeDef(
                    id="walk-alt",
                    source="wa",
                    target="wb",
                    floor_id="1F",
                    map_path=[(180, 130)],
                ),
            ],
        ),
    )
    visits = [
        Visit(
            id="vA", camera="A", t_in=T0, t_out=T0, score=0.7,
            projected_trajectory=[ProjectedTrajectoryPoint(timestamp=T0, x=20, y=30, floor_id="1F")],
            trajectory_approximate=False,
        ),
        Visit(
            id="vB", camera="B", t_in=T0 + timedelta(seconds=10), t_out=T0 + timedelta(seconds=12), score=0.8,
            projected_trajectory=[ProjectedTrajectoryPoint(timestamp=T0 + timedelta(seconds=10), x=300, y=80, floor_id="1F")],
            trajectory_approximate=False,
        ),
        Visit(id="vC", camera="C", t_in=T0 + timedelta(seconds=45), t_out=T0 + timedelta(seconds=50), score=0.8, flags=["LONG_GAP"]),
    ]
    hypothesis = Hypothesis(
        id="h1", visits=visits,
        transitions=[
            Transition(
                from_visit_id="vA", to_visit_id="vB", edge_ids=["A-B"], alternative_edge_paths=[["A-B-alt"]],
                geometry=TransitionGeometry(
                    floor_id="1F",
                    points=[(20, 30), (150, 40), (300, 80)],
                    alternative_paths=[[(20, 30), (180, 130), (300, 80)]],
                    walk_edge_ids=["walk-main"],
                    alternative_walk_edge_paths=[["walk-alt"]],
                    distance_min_m=10,
                    distance_max_m=12,
                ),
            ),
            Transition(from_visit_id="vB", to_visit_id="vC", edge_ids=["B-C"], kind="VERTICAL", flags=["LONG_GAP"]),
        ],
        current_node="C", status=HypothesisStatus.ALIVE, score=1.0,
    )
    competing = hypothesis.model_copy(
        update={
            "id": "h2",
            "score": 0.5,
            "visits": visits[:2],
            "transitions": [
                hypothesis.transitions[0].model_copy(
                    update={
                        "geometry": hypothesis.transitions[0].geometry.model_copy(
                            update={
                                "points": [(20, 30), (100, 300), (300, 80)],
                                "alternative_paths": [],
                            }
                        )
                    }
                )
            ],
            "current_node": "B",
        }
    )
    state = init_state(
        CaseInfo(case_id="c", target_id="t", start_time=T0), graph, RoutingParams()
    ).model_copy(
        update={"hypotheses": [hypothesis, competing], "state_version": 12}
    )
    result = StepResult(
        state=state,
        probes=[],
        hypotheses=[hypothesis, competing],
        best=hypothesis,
        estimate=Estimate(zones=[]), mode=SearchMode.CATCHING_UP,
    )
    assets = {"1F": b"\x89PNG\r\n\x1a\nplaceholder"}
    first = render_route_maps(result, graph, assets)
    second = render_route_maps(result, graph, assets)
    assert first.model_dump_json() == second.model_dump_json()
    assert first.route_revision == 12 and len(first.maps) == 2
    for rendered in first.maps:
        ET.fromstring(rendered.svg)
        lowered = rendered.svg.lower()
        assert "<script" not in lowered
        assert 'href="http://' not in lowered and 'href="https://' not in lowered
    floor_one = next(item.svg for item in first.maps if item.floor_id == "1F")
    assert 'data-role="best-route"' in floor_one
    assert 'data-approximate="false"' in floor_one
    assert 'data-role="alternative-path"' in floor_one
    assert 'stroke-dasharray="5 7"' in floor_one
    assert "100.00,300.00" not in floor_one
    assert 'data-role="inter-floor"' not in floor_one
    floor_two = next(item.svg for item in first.maps if item.floor_id == "2F")
    assert "LONG_GAP" in floor_two


def test_abstract_renderer_fallback() -> None:
    graph = generate_ring_graph(4)
    state = init_state(CaseInfo(case_id="c", target_id="t", start_time=T0), graph, RoutingParams())
    result = StepResult(state=state, probes=[], hypotheses=[], mode=SearchMode.CATCHING_UP)
    bundle = render_route_maps(result, graph, {})
    assert [item.floor_id for item in bundle.maps] == ["ABSTRACT"]
    ET.fromstring(bundle.maps[0].svg)


def test_clipped_time_keeps_hard_gate_but_skips_lateness() -> None:
    graph = SiteGraph(
        cameras={"A": CameraDef(id="A"), "B": CameraDef(id="B")},
        edges=[EdgeDef(id="A-B", source="A", target="B", t_min=100, t_typ=120, t_max=180)],
    )
    first = Visit(id="a", camera="A", t_in=T0, t_out=T0, score=0.8, clipped_out=True)
    impossible = Visit(
        id="b-fast", camera="B", t_in=T0 + timedelta(seconds=50),
        t_out=T0 + timedelta(seconds=55), score=0.8,
    )
    _, _, reason = gate_visit_to_hypothesis(first, impossible, graph, RoutingParams(clock_tolerance_s=0), T0)
    assert reason and "hard gate" in reason

    late = impossible.model_copy(
        update={"id": "b-late", "t_in": T0 + timedelta(seconds=400), "t_out": T0 + timedelta(seconds=405)}
    )
    likelihood, _, reason = gate_visit_to_hypothesis(first, late, graph, RoutingParams(), T0)
    assert reason is None and likelihood == 0.0


def test_unknown_camera_is_skipped_and_done_empty_still_counts() -> None:
    graph = generate_ring_graph(3)
    params = RoutingParams()
    state = init_state(CaseInfo(case_id="c", target_id="t", start_time=T0), graph, params)
    state.pending_probes = [
        ProbeRequest(probe_id="p", camera_id="cam_0", t_from=T0, t_to=T0 + timedelta(seconds=30))
    ]
    invalid = ProbeResult(
        probe_id="p", status="DONE",
        tracklets=[_tracklet("bad", "missing-camera", T0)],
        candidates=[Candidate(candidate_id="bad-c", tracklet_id="bad", appearance_score=0.99)],
    )
    result = step(state, [invalid], graph, Anchors(), params, T0 + timedelta(seconds=30))
    assert not result.hypotheses
    assert result.state.probed_windows[0].found is False


def test_baseline_finishes_after_one_batch_round() -> None:
    graph = generate_ring_graph(3)
    params = RoutingParams(use_st=False)
    state = init_state(
        CaseInfo(
            case_id="c", target_id="t", start_time=T0,
            window_start=T0, window_end=T0 + timedelta(minutes=5),
        ),
        graph,
        params,
    )
    issued = step(state, [], graph, Anchors(), params, T0 + timedelta(minutes=5))
    assert len(issued.probes) == 3 and not issued.done
    results = [ProbeResult(probe_id=probe.probe_id, status="DONE") for probe in issued.probes]
    finished = step(issued.state, results, graph, Anchors(), params, T0 + timedelta(minutes=5))
    assert finished.done and finished.mode == SearchMode.PAST_SUMMARY


def test_stop_priority_operator_then_budget_then_gap() -> None:
    graph = generate_ring_graph(3)
    params = RoutingParams(camera_minute_budget=1, max_gap_s=10)
    state = init_state(CaseInfo(case_id="c", target_id="t", start_time=T0), graph, params)
    visit = Visit(id="v", camera="cam_0", t_in=T0, t_out=T0, score=0.8)
    state.hypotheses = [Hypothesis(id="h", visits=[visit], current_node="cam_0", status="ALIVE")]
    state.camera_minutes_used = 2
    closed = step(state, [], graph, Anchors(operator_closed=True), params, T0 + timedelta(seconds=20))
    assert closed.stop_reason.value == "OPERATOR_CLOSED"
    budget = step(state, [], graph, Anchors(), params, T0 + timedelta(seconds=20))
    assert budget.stop_reason.value == "BUDGET_SPENT"
    state.camera_minutes_used = 0
    state.search_frontier = SearchFrontier(
        last_observed_at=T0,
        reached_at=T0 + timedelta(seconds=10),
    )
    gap = step(state, [], graph, Anchors(), params, T0 + timedelta(seconds=20))
    assert gap.stop_reason.value == "MAX_GAP"


def test_done_empty_can_open_evidence_backed_pass_through_branch() -> None:
    graph = SiteGraph(
        cameras={key: CameraDef(id=key) for key in ("A", "B", "C")},
        edges=[
            EdgeDef(id="A-B", source="A", target="B", t_min=10, t_typ=20, t_max=30),
            EdgeDef(id="B-C", source="B", target="C", t_min=10, t_typ=20, t_max=30),
        ],
    )
    params = RoutingParams()
    state = init_state(CaseInfo(case_id="c", target_id="t", start_time=T0), graph, params)
    first = Visit(id="vA", camera="A", t_in=T0, t_out=T0, tracklet_ids=["known"], score=0.8)
    state.hypotheses = [Hypothesis(id="hA", visits=[first], current_node="A", status="ALIVE")]
    state.probed_windows = [
        ProbedWindow(
            camera_id="B", t_from=T0 + timedelta(seconds=10),
            t_to=T0 + timedelta(seconds=30), probe_id="empty-B", status="DONE",
            found=False,
        )
    ]
    expanded = step(state, [], graph, Anchors(), params, T0 + timedelta(seconds=70))
    pass_probe = next(probe for probe in expanded.probes if probe.camera == "C")
    assert "PASS_THROUGH" in pass_probe.reason

    sighting = ProbeResult(
        probe_id=pass_probe.probe_id,
        status="DONE",
        tracklets=[_tracklet("tC", "C", T0 + timedelta(seconds=42))],
        candidates=[Candidate(candidate_id="cC", tracklet_id="tC", appearance_score=0.85)],
    )
    updated = step(expanded.state, [sighting], graph, Anchors(), params, T0 + timedelta(seconds=70))
    branch = next(item for item in updated.hypotheses if any(v.camera == "C" for v in item.visits))
    assert branch.transitions[-1].edge_ids == ["A-B", "B-C"]
    assert "PASS_THROUGH" in branch.visits[-1].flags


def test_later_confirm_creates_gate_override_from_saved_review_evidence() -> None:
    graph = SiteGraph(
        cameras={"A": CameraDef(id="A"), "C": CameraDef(id="C")},
        edges=[EdgeDef(id="A-C", source="A", target="C", t_min=150, t_typ=180, t_max=240)],
    )
    params = RoutingParams(clock_tolerance_s=0)
    state = init_state(CaseInfo(case_id="c", target_id="t", start_time=T0), graph, params)
    known = Visit(id="known", camera="A", t_in=T0, t_out=T0, tracklet_ids=["known"], score=0.8)
    state.hypotheses = [Hypothesis(id="h-known", visits=[known], current_node="A", status="ALIVE")]
    result = ProbeResult(
        probe_id="external", status="DONE",
        tracklets=[_tracklet("too-fast", "C", T0 + timedelta(seconds=60))],
        candidates=[Candidate(candidate_id="too-fast-c", tracklet_id="too-fast", appearance_score=0.9)],
    )
    reviewed = step(state, [result], graph, Anchors(), params, T0 + timedelta(seconds=70))
    assert reviewed.state.pending_review_visits
    confirmed = step(
        reviewed.state, [], graph, Anchors(confirm_tracklet_ids=["too-fast"]), params,
        T0 + timedelta(seconds=71),
    )
    override = next(
        hypothesis for hypothesis in confirmed.hypotheses
        if hypothesis.transitions and hypothesis.transitions[-1].kind == "UNKNOWN"
    )
    assert "GATE_OVERRIDE" in override.visits[-1].flags


def test_candidate_below_probe_beam_remains_confirmable() -> None:
    graph = SiteGraph(cameras={"A": CameraDef(id="A")}, edges=[])
    params = RoutingParams(cands_per_probe=1)
    state = init_state(
        CaseInfo(
            case_id="c",
            target_id="t",
            start_time=T0,
            last_known_camera="A",
            last_known_time=T0,
        ),
        graph,
        params,
    )
    high = _tracklet("high", "A", T0 + timedelta(seconds=5)).model_copy(
        update={"probe_id": "p"}
    )
    low = _tracklet("low", "A", T0 + timedelta(seconds=30)).model_copy(
        update={"probe_id": "p"}
    )
    result = ProbeResult(
        probe_id="p",
        status="DONE",
        tracklets=[high, low],
        candidates=[
            Candidate(
                candidate_id="high-c",
                tracklet_id="high",
                appearance_score=0.95,
            ),
            Candidate(
                candidate_id="low-c",
                tracklet_id="low",
                appearance_score=0.2,
            ),
        ],
    )

    automatic = step(
        state,
        [result],
        graph,
        Anchors(),
        params,
        T0 + timedelta(seconds=50),
    )
    assert all(
        "low" not in visit.tracklet_ids
        for hypothesis in automatic.hypotheses
        for visit in hypothesis.visits
    )
    assert any(
        "low" in visit.tracklet_ids
        for visit in automatic.state.pending_review_visits
    )

    confirmed = step(
        automatic.state,
        [],
        graph,
        Anchors(confirm_tracklet_ids=["low"]),
        params,
        T0 + timedelta(seconds=51),
    )
    assert any(
        "low" in visit.tracklet_ids
        for hypothesis in confirmed.hypotheses
        for visit in hypothesis.visits
    )


def test_malformed_runtime_result_is_logged_and_skipped() -> None:
    graph = generate_ring_graph(3)
    params = RoutingParams()
    state = init_state(CaseInfo(case_id="c", target_id="t", start_time=T0), graph, params)
    result = step(
        state,
        [{"probe_id": "bad", "status": "DONE", "tracklets": [{"not": "a tracklet"}]}],
        graph,
        Anchors(),
        params,
        T0,
    )
    assert result.hypotheses == []


def test_blind_zone_last_known_immediately_opens_border_watch() -> None:
    graph = SiteGraph(
        cameras={"A": CameraDef(id="A"), "B": CameraDef(id="B")},
        blind_nodes=[BlindNodeDef(id="room", borders=["A", "B"])],
        edges=[
            EdgeDef(id="room-A", source="room", target="A", t_min=5, t_typ=10, t_max=20),
            EdgeDef(id="room-B", source="room", target="B", t_min=5, t_typ=10, t_max=20),
        ],
    )
    state = init_state(
        CaseInfo(
            case_id="c", target_id="t", start_time=T0,
            last_known_zone="room", last_known_time=T0,
        ),
        graph,
        RoutingParams(),
    )
    now = T0 + timedelta(seconds=1)
    result = step(state, [], graph, Anchors(), RoutingParams(), now)
    assert result.mode == SearchMode.CATCHING_UP
    assert {probe.camera for probe in result.probes} == {"A", "B"}
    assert all(probe.t_to is not None for probe in result.probes)

    live = step(
        result.state,
        [ProbeResult(probe_id=probe.probe_id, status="DONE") for probe in result.probes],
        graph,
        Anchors(),
        RoutingParams(),
        now,
    )
    assert live.mode == SearchMode.WAITING
    assert {probe.camera for probe in live.probes} == {"A", "B"}
    assert all(probe.t_to is None for probe in live.probes)


def test_direct_route_keeps_ring_path_alternative_and_probes_divergence() -> None:
    graph = SiteGraph(
        cameras={key: CameraDef(id=key) for key in ("A", "B", "C", "D")},
        edges=[
            EdgeDef(id="A-D", source="A", target="D", t_min=20, t_typ=50, t_max=90),
            EdgeDef(id="A-B", source="A", target="B", t_min=10, t_typ=20, t_max=40),
            EdgeDef(id="B-C", source="B", target="C", t_min=10, t_typ=20, t_max=40),
            EdgeDef(id="C-D", source="C", target="D", t_min=10, t_typ=20, t_max=40),
        ],
    )
    params = RoutingParams(K=6)
    state = init_state(CaseInfo(case_id="c", target_id="t", start_time=T0), graph, params)
    first = Visit(id="vA", camera="A", t_in=T0, t_out=T0, tracklet_ids=["known"], score=0.8)
    state.hypotheses = [Hypothesis(id="hA", visits=[first], current_node="A", status="ALIVE")]
    result = ProbeResult(
        probe_id="external", status="DONE",
        tracklets=[_tracklet("tD", "D", T0 + timedelta(seconds=60))],
        candidates=[Candidate(candidate_id="cD", tracklet_id="tD", appearance_score=0.85)],
    )
    routed = step(state, [result], graph, Anchors(), params, T0 + timedelta(seconds=70))
    branch = next(item for item in routed.hypotheses if item.current_node == "D")
    assert ["A-B", "B-C", "C-D"] in branch.transitions[-1].alternative_edge_paths
    assert {"B", "C"}.issubset({probe.camera for probe in routed.probes})
    assert any(ambiguity.diverge_cameras == ["B", "C"] for ambiguity in routed.ambiguities)


def test_worker_facade_returns_step_and_finished_maps() -> None:
    graph = generate_ring_graph(3)
    params = RoutingParams()
    state = init_state(CaseInfo(case_id="c", target_id="t", start_time=T0), graph, params)
    output = step_and_render(state, [], graph, Anchors(), params, T0, {})
    assert output.step_result.state.state_version == 1
    assert output.rendered_route.route_revision == 1
    assert output.rendered_route.maps[0].mime_type == "image/svg+xml"


def test_demo_viewer_only_displays_routing_svg(tmp_path) -> None:
    _write_viewer(tmp_path, ["round-001-1F.svg"])
    source = (tmp_path / "index.html").read_text(encoding="utf-8").lower()
    assert "<img" in source
    assert "<canvas" not in source
    assert "<svg" not in source
    assert "polyline" not in source
