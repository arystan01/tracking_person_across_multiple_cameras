"""Pure public Routing API."""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

from javes_contracts import (
    Anchors,
    CaseInfo,
    Hypothesis,
    HypothesisStatus,
    MapPosition,
    ProbeRequest,
    ProbeResult,
    ProjectedTrajectoryPoint,
    SearchMode,
    SearchState,
    SiteGraph,
    StepResult,
    TrajectoryPoint,
    Visit,
)
from .params import RoutingParams
from .positioning import project_trajectory
from .expand import (
    _covered,
    _probe_id,
    expand_dormant_watch_probes,
    expand_hypotheses,
)
from .frontier import (
    advance_empty_shell,
    advance_finished_frontier,
    clamp_probe_to_shell,
    initialise_frontier,
    prepare_historical_shell,
    register_required_probes,
    sync_observation_frontier,
)
from .predict import (
    check_stop_conditions,
    compute_estimate,
    determine_mode,
    update_dormant_and_ended,
)
from .update import update_hypotheses

logger = logging.getLogger(__name__)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(timezone.utc)


def _stable_id(prefix: str, *parts: object) -> str:
    value = "\x1f".join(str(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]}"


def _anchor_hypotheses(
    camera: str | None,
    zone: str | None,
    at: datetime | None,
    graph: SiteGraph,
    params: RoutingParams,
    bbox_xywh: tuple[float, float, float, float] | None = None,
    footpoint_px: tuple[float, float] | None = None,
    map_position: MapPosition | None = None,
) -> list[Hypothesis]:
    if at is None:
        return []
    projected: list[ProjectedTrajectoryPoint] = []
    if map_position is not None:
        floor = next(
            (
                item
                for item in (graph.map.floors if graph.map is not None else [])
                if item.id == map_position.floor_id
            ),
            None,
        )
        if (
            map_position.error_m <= params.max_calibration_error_m
            and floor is not None
            and 0 <= map_position.x <= floor.size_px[0]
            and 0 <= map_position.y <= floor.size_px[1]
        ):
            projected = [
                ProjectedTrajectoryPoint(
                    timestamp=at,
                    x=map_position.x,
                    y=map_position.y,
                    floor_id=map_position.floor_id,
                    calibration_error_m=map_position.error_m,
                )
            ]
        else:
            logger.warning("Ignoring invalid last_known map position")
    if camera is not None:
        cameras = [camera] if camera in graph.cameras else []
        if not cameras:
            logger.warning("Ignoring last_known on unknown camera %s", camera)
        elif not projected:
            pixel = footpoint_px
            if pixel is None and bbox_xywh is not None:
                x, y, width, height = bbox_xywh
                pixel = (x + width / 2.0, y + height)
            if pixel is not None:
                projected, _ = project_trajectory(
                    graph.cameras[camera],
                    [
                        TrajectoryPoint(
                            timestamp=at, x_px=pixel[0], y_px=pixel[1]
                        )
                    ],
                    max_error_m=params.max_calibration_error_m,
                )
    elif zone in {node.id for node in graph.blind_nodes} or zone == "OUTSIDE":
        visit = Visit(
            id=_stable_id("v", "last-known", zone, at.isoformat()),
            camera=zone or "OUTSIDE",
            zone=zone,
            t_in=at,
            t_out=at,
            score=0.5,
            source="LAST_KNOWN",
            flags=["LAST_KNOWN"],
            projected_trajectory=projected,
            trajectory_approximate=not bool(projected),
        )
        return [
            Hypothesis(
                id=_stable_id("h", "last-known", zone, at.isoformat()),
                visits=[visit],
                current_node=zone or "OUTSIDE",
                status=HypothesisStatus.DORMANT,
            )
        ]
    else:
        cameras = sorted(item.id for item in graph.cameras.values() if item.zone == zone)
        if zone is not None and not cameras:
            logger.warning("Ignoring last_known on unknown/empty zone %s", zone)
    result: list[Hypothesis] = []
    for camera_id in cameras:
        visit = Visit(
            id=_stable_id("v", "last-known", camera_id, at.isoformat()),
            camera=camera_id,
            zone=graph.cameras[camera_id].zone or None,
            t_in=at,
            t_out=at,
            score=0.5,
            source="LAST_KNOWN",
            flags=["LAST_KNOWN"],
            projected_trajectory=projected,
            trajectory_approximate=not bool(projected),
        )
        result.append(
            Hypothesis(
                id=_stable_id("h", visit.id),
                visits=[visit],
                current_node=camera_id,
                status=HypothesisStatus.ALIVE,
            )
        )
    return result


def init_state(case: CaseInfo, graph: SiteGraph, params: RoutingParams) -> SearchState:
    """Create a JSON-roundtrippable state and seed it from last-known evidence."""
    hypotheses = _anchor_hypotheses(
        case.last_known_camera,
        case.last_known_zone,
        case.last_known_time,
        graph,
        params,
        case.last_known_bbox_xywh,
        case.last_known_footpoint_px,
        case.last_known_map_position,
    )
    return SearchState(
        case_id=case.case_id,
        profile=case.profile,
        site_version=graph.version,
        params_version=params.version,
        window_start=case.window_start,
        window_end=case.window_end,
        hypotheses=hypotheses,
        search_frontier=initialise_frontier(hypotheses),
        mode=SearchMode.WAITING if hypotheses and hypotheses[0].status == HypothesisStatus.DORMANT else SearchMode.CATCHING_UP,
    )


def _best(hypotheses: list[Hypothesis]) -> Hypothesis | None:
    eligible = [item for item in hypotheses if not item.requires_confirmation]
    pool = eligible or hypotheses
    return min(pool, key=lambda item: (-item.score, item.id)) if pool else None


def _baseline_probes(state: SearchState, graph: SiteGraph, now: datetime) -> list[ProbeRequest]:
    start = state.window_start or now
    end = state.window_end or now
    return [
        ProbeRequest(
            probe_id=_probe_id(camera, start, end, "BASELINE", []),
            camera=camera,
            t_from=start,
            t_to=end,
            priority=1.0,
            reason="BASELINE",
        )
        for camera in sorted(graph.cameras)
    ]


def _finish(
    state: SearchState,
    hypotheses: list[Hypothesis],
    rejected,
    graph: SiteGraph,
    params: RoutingParams,
    now: datetime,
    reason,
) -> StepResult:
    state.done = True
    state.stop_reason = reason
    state.mode = SearchMode.PAST_SUMMARY
    state.pending_probes = []
    return StepResult(
        state=state,
        probes=[],
        hypotheses=hypotheses,
        best=_best(hypotheses),
        rejected=rejected,
        estimate=compute_estimate(hypotheses, graph, now, state, params),
        mode=SearchMode.PAST_SUMMARY,
        done=True,
        stop_reason=reason,
    )


def step(
    state: SearchState,
    probe_results: list[ProbeResult | dict],
    graph: SiteGraph,
    anchors: Anchors,
    params: RoutingParams,
    now: datetime,
) -> StepResult:
    """Advance one deterministic search round without mutating any input."""
    now = _aware_utc(now)
    valid_results: list[ProbeResult] = []
    for raw_result in probe_results:
        try:
            valid_results.append(ProbeResult.model_validate(raw_result))
        except Exception as exc:  # runtime data is noisy; the site config remains strict
            logger.warning("Skipping invalid ProbeResult: %s", exc)
    state = state.model_copy(deep=True)
    original_stop_reason = state.stop_reason
    if state.site_version != graph.version:
        raise ValueError(
            f"SearchState site_version={state.site_version!r} does not match graph version={graph.version!r}"
        )
    if state.params_version != params.version:
        raise ValueError(
            f"SearchState params_version={state.params_version!r} does not match params version={params.version!r}"
        )
    state.round_number += 1
    state.state_version += 1

    if not state.hypotheses and anchors.last_known_time is not None:
        state.hypotheses = _anchor_hypotheses(
            anchors.last_known_camera,
            anchors.last_known_zone,
            anchors.last_known_time,
            graph,
            params,
            anchors.last_known_bbox_xywh,
            anchors.last_known_footpoint_px,
            anchors.last_known_map_position,
        )

    previous_pending = {probe.probe_id for probe in state.pending_probes}
    hypotheses, rejected, state = update_hypotheses(
        state, valid_results, graph, anchors, params, now
    )
    sync_observation_frontier(state, hypotheses)
    advance_finished_frontier(state)
    retries = [probe for probe in state.pending_probes if probe.probe_id not in previous_pending]

    if anchors.review_only:
        from javes_contracts import StopReason

        return _finish(
            state,
            hypotheses,
            rejected,
            graph,
            params,
            now,
            original_stop_reason or StopReason.OPERATOR_CLOSED,
        )

    done, reason = check_stop_conditions(
        state, hypotheses, params, now, operator_closed=anchors.operator_closed
    )
    if done:
        return _finish(state, hypotheses, rejected, graph, params, now, reason)

    if not params.use_st:
        if not state.baseline_started:
            probes = _baseline_probes(state, graph, now)
            state.baseline_started = True
            state.pending_probes = probes
        elif not state.pending_probes:
            from javes_contracts import StopReason
            return _finish(
                state, hypotheses, rejected, graph, params, now, StopReason.ALL_ENDED
            )
        else:
            probes = retries
        state.hypotheses = hypotheses
        state.mode = SearchMode.CATCHING_UP
        return StepResult(
            state=state,
            probes=probes,
            hypotheses=hypotheses,
            best=_best(hypotheses),
            rejected=rejected,
            estimate=compute_estimate(hypotheses, graph, now, state, params),
            mode=state.mode,
        )

    hypotheses = update_dormant_and_ended(hypotheses, state, graph, params, now)
    state.hypotheses = hypotheses
    done, reason = check_stop_conditions(
        state, hypotheses, params, now, operator_closed=anchors.operator_closed
    )
    if done:
        return _finish(state, hypotheses, rejected, graph, params, now, reason)

    route_probes, ambiguities = expand_hypotheses(state, graph, params, now)
    watch_probes = expand_dormant_watch_probes(state, graph, params, now)
    historical_active = prepare_historical_shell(state, params, now)
    clamped_candidates: list[ProbeRequest] = []
    for probe in route_probes + watch_probes:
        clamped = clamp_probe_to_shell(probe, state)
        if clamped is None:
            continue
        if clamped.t_from != probe.t_from or clamped.t_to != probe.t_to:
            clamped = clamped.model_copy(
                update={
                    "probe_id": _probe_id(
                        clamped.camera,
                        clamped.t_from,
                        clamped.t_to,
                        clamped.reason,
                        clamped.hypothesis_ids,
                    )
                }
            )
        # Expansion checks coverage before the historical frontier clamps a
        # probe. A wider raw window can look new even when its clamped form is
        # exactly the in-flight probe. Re-check the actual wire identity so a
        # heartbeat cannot duplicate it and strand the frontier.
        if _covered(
            state,
            clamped.camera,
            clamped.t_from,
            clamped.t_to,
        ):
            continue
        clamped_candidates.append(clamped)
    candidates = sorted(
        {probe.probe_id: probe for probe in clamped_candidates}.values(),
        key=lambda item: (-item.priority, item.camera, item.t_from, item.probe_id),
    )
    capacity = max(params.K - len(state.pending_probes), 0)
    new_probes = candidates[:capacity]
    register_required_probes(state, new_probes)
    state.pending_probes.extend(new_probes)
    state.pending_probes.sort(
        key=lambda item: (-item.priority, item.camera, item.t_from, item.probe_id)
    )
    emitted = sorted(
        retries + new_probes,
        key=lambda item: (-item.priority, item.camera, item.t_from, item.probe_id),
    )
    if historical_active and not new_probes and not retries:
        advance_empty_shell(state)
    state.mode = (
        SearchMode.CATCHING_UP
        if historical_active
        else determine_mode(state, hypotheses, now, graph)
    )
    return StepResult(
        state=state,
        probes=emitted,
        hypotheses=hypotheses,
        best=_best(hypotheses),
        ambiguities=ambiguities,
        rejected=rejected,
        estimate=compute_estimate(hypotheses, graph, now, state, params),
        mode=state.mode,
    )
