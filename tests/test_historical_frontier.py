from __future__ import annotations

from datetime import datetime, timedelta, timezone

from javes_contracts import (
    Anchors,
    BlindNodeDef,
    CameraDef,
    Candidate,
    CaseInfo,
    EdgeDef,
    OutsideDef,
    ProbeResult,
    SearchMode,
    SiteGraph,
    Tracklet,
)
from routing.contracts.params import RoutingParams
from routing.step import init_state, step


UTC = timezone.utc
T0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


def outside_graph(*, two_destinations: bool = False) -> SiteGraph:
    cameras = {
        "A": CameraDef(id="A"),
        "B": CameraDef(id="B"),
    }
    edges = [
        EdgeDef(
            id="A-B",
            source="A",
            target="B",
            t_min=10,
            t_typ=20,
            t_max=60,
        )
    ]
    if two_destinations:
        cameras["C"] = CameraDef(id="C")
        edges.append(
            EdgeDef(
                id="A-C",
                source="A",
                target="C",
                t_min=15,
                t_typ=25,
                t_max=70,
            )
        )
    return SiteGraph(
        cameras=cameras,
        edges=edges,
        outside=OutsideDef(entrances={"A": 1.0}),
    )


def historical_state(
    graph: SiteGraph,
    params: RoutingParams,
    *,
    window_end: datetime | None = None,
):
    return init_state(
        CaseInfo(
            case_id="historical-case",
            target_id="target",
            start_time=T0,
            last_known_camera="A",
            last_known_time=T0,
            window_start=T0,
            window_end=window_end,
        ),
        graph,
        params,
    )


def done(probe, *, coverage_ratio: float = 1.0) -> ProbeResult:
    duration = (
        None
        if probe.t_to is None
        else (probe.t_to - probe.t_from).total_seconds() * coverage_ratio
    )
    return ProbeResult(
        probe_id=probe.probe_id,
        status="DONE",
        coverage_ratio=coverage_ratio,
        processed_duration_s=duration,
    )


def test_ten_day_old_last_known_searches_finite_shells_before_max_gap() -> None:
    graph = outside_graph()
    params = RoutingParams(max_gap_s=900, historical_shell_s=300)
    now = T0 + timedelta(days=10)
    current = step(
        historical_state(graph, params), [], graph, Anchors(), params, now
    )
    emitted = list(current.probes)
    assert emitted and all(probe.t_to is not None for probe in emitted)
    assert not current.done

    for _ in range(8):
        current = step(
            current.state,
            [done(probe) for probe in current.probes],
            graph,
            Anchors(),
            params,
            now,
        )
        emitted.extend(current.probes)
        if current.done:
            break

    assert current.done
    assert current.stop_reason is not None
    assert current.stop_reason.value == "MAX_GAP"
    assert emitted and all(probe.t_to is not None for probe in emitted)
    assert current.state.search_frontier is not None
    assert current.state.search_frontier.reached_at == T0 + timedelta(seconds=900)


def test_frontier_waits_for_every_required_probe_in_shell() -> None:
    graph = outside_graph(two_destinations=True)
    params = RoutingParams(max_gap_s=900, historical_shell_s=300)
    now = T0 + timedelta(days=1)
    issued = step(
        historical_state(graph, params), [], graph, Anchors(), params, now
    )
    assert len(issued.probes) == 2
    frontier = issued.state.search_frontier
    assert frontier is not None
    assert len(frontier.required_probe_ids) == 2

    first_only = step(
        issued.state,
        [done(issued.probes[0])],
        graph,
        Anchors(),
        params,
        now,
    )
    assert first_only.state.search_frontier is not None
    assert first_only.state.search_frontier.reached_at == T0
    assert first_only.state.search_frontier.required_probe_ids

    completed = first_only
    for _ in range(4):
        completed = step(
            completed.state,
            [done(probe) for probe in completed.state.pending_probes],
            graph,
            Anchors(),
            params,
            now,
        )
        if (
            completed.state.search_frontier is not None
            and completed.state.search_frontier.reached_at >= T0 + timedelta(seconds=300)
        ):
            break
    assert completed.state.search_frontier is not None
    assert completed.state.search_frontier.reached_at >= T0 + timedelta(seconds=300)


def test_retry_exhaustion_advances_as_unobserved_without_looping() -> None:
    graph = outside_graph()
    params = RoutingParams(
        max_gap_s=600,
        historical_shell_s=300,
        max_retries=1,
    )
    now = T0 + timedelta(days=1)
    issued = step(
        historical_state(graph, params), [], graph, Anchors(), params, now
    )
    failed = step(
        issued.state,
        [ProbeResult(probe_id=issued.probes[0].probe_id, status="FAILED")],
        graph,
        Anchors(),
        params,
        now,
    )
    retry = next(probe for probe in failed.probes if probe.retry_count == 1)
    exhausted = step(
        failed.state,
        [ProbeResult(probe_id=retry.probe_id, status="TIMEOUT")],
        graph,
        Anchors(),
        params,
        now,
    )
    assert any(
        item["reason"] == "TIMEOUT"
        for item in exhausted.state.unobserved_windows
    )
    assert exhausted.state.search_frontier is not None
    assert exhausted.state.search_frontier.reached_at > T0

    current = exhausted
    for _ in range(5):
        if current.done:
            break
        current = step(
            current.state,
            [done(probe) for probe in current.probes],
            graph,
            Anchors(),
            params,
            now,
        )
    assert current.done
    assert current.stop_reason is not None
    assert current.stop_reason.value == "MAX_GAP"


def test_partial_coverage_is_unobserved_not_full_negative_evidence() -> None:
    graph = outside_graph()
    params = RoutingParams(max_gap_s=600, historical_shell_s=300)
    now = T0 + timedelta(days=1)
    issued = step(
        historical_state(graph, params), [], graph, Anchors(), params, now
    )
    partial = step(
        issued.state,
        [done(issued.probes[0], coverage_ratio=0.75)],
        graph,
        Anchors(),
        params,
        now,
    )
    assert any(
        item["reason"] == "PARTIAL_COVERAGE"
        for item in partial.state.unobserved_windows
    )
    assert partial.hypotheses[0].miss_count == 0


def test_window_end_before_gap_horizon_finishes_all_ended() -> None:
    graph = outside_graph()
    params = RoutingParams(max_gap_s=900, historical_shell_s=300)
    window_end = T0 + timedelta(seconds=450)
    now = T0 + timedelta(days=1)
    current = step(
        historical_state(graph, params, window_end=window_end),
        [],
        graph,
        Anchors(),
        params,
        now,
    )
    for _ in range(6):
        current = step(
            current.state,
            [done(probe) for probe in current.probes],
            graph,
            Anchors(),
            params,
            now,
        )
        if current.done:
            break
    assert current.done
    assert current.stop_reason is not None
    assert current.stop_reason.value == "ALL_ENDED"
    assert current.state.search_frontier is not None
    assert current.state.search_frontier.reached_at == window_end


def test_historical_finite_probe_transitions_to_open_and_tracks_duration() -> None:
    graph = SiteGraph(
        cameras={"A": CameraDef(id="A")},
        edges=[],
        blind_nodes=[BlindNodeDef(id="room", borders=["A"])],
    )
    params = RoutingParams(max_gap_s=900, historical_shell_s=300)
    state = init_state(
        CaseInfo(
            case_id="blind-history",
            target_id="target",
            start_time=T0,
            last_known_zone="room",
            last_known_time=T0,
        ),
        graph,
        params,
    )
    now = T0 + timedelta(seconds=100)
    finite = step(state, [], graph, Anchors(), params, now)
    assert finite.mode == SearchMode.CATCHING_UP
    assert finite.probes and all(probe.t_to is not None for probe in finite.probes)

    live = step(
        finite.state,
        [done(finite.probes[0])],
        graph,
        Anchors(),
        params,
        now,
    )
    assert live.probes and all(probe.t_to is None for probe in live.probes)
    assert all(probe.t_from == now for probe in live.probes)
    assert live.mode == SearchMode.WAITING

    progressed = step(
        live.state,
        [
            ProbeResult(
                probe_id=live.probes[0].probe_id,
                status="DONE",
                processed_duration_s=50,
            )
        ],
        graph,
        Anchors(),
        params,
        now + timedelta(seconds=50),
    )
    assert progressed.state.search_frontier is not None
    assert progressed.state.search_frontier.reached_at == now + timedelta(seconds=50)


def test_new_sighting_resets_frontier_from_observation_time() -> None:
    graph = outside_graph()
    params = RoutingParams(max_gap_s=900, historical_shell_s=300)
    now = T0 + timedelta(days=1)
    issued = step(
        historical_state(graph, params), [], graph, Anchors(), params, now
    )
    sighting_start = T0 + timedelta(seconds=500)
    sighting_end = sighting_start + timedelta(seconds=10)
    sighting = ProbeResult(
        probe_id=issued.probes[0].probe_id,
        status="DONE",
        coverage_ratio=1.0,
        processed_duration_s=(
            issued.probes[0].t_to - issued.probes[0].t_from
        ).total_seconds(),
        tracklets=[
            Tracklet(
                id="new-sighting",
                camera="B",
                t_in=sighting_start,
                t_out=sighting_end,
                bbox_first=(0, 0, 10, 20),
                bbox_last=(5, 0, 10, 20),
            )
        ],
        candidates=[
            Candidate(
                id="new-sighting-candidate",
                tracklet_id="new-sighting",
                score=0.95,
            )
        ],
    )
    updated = step(
        issued.state,
        [sighting],
        graph,
        Anchors(),
        params,
        now,
    )
    assert updated.state.search_frontier is not None
    assert updated.state.search_frontier.last_observed_at == sighting_end
    assert updated.state.search_frontier.reached_at >= sighting_end
    assert updated.state.search_frontier.reached_at <= sighting_end + timedelta(
        seconds=params.historical_shell_s
    )


def test_frontier_json_round_trip_and_step_are_deterministic() -> None:
    graph = outside_graph()
    params = RoutingParams(max_gap_s=900, historical_shell_s=300)
    now = T0 + timedelta(days=1)
    state = historical_state(graph, params)
    before = state.model_dump_json()
    first = step(state, [], graph, Anchors(), params, now)
    second = step(state, [], graph, Anchors(), params, now)
    assert first.model_dump_json() == second.model_dump_json()
    assert state.model_dump_json() == before
    restored = type(first.state).model_validate_json(first.state.model_dump_json())
    assert restored == first.state
