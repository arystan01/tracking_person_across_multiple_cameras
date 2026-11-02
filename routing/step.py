"""Pure public Routing API."""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

from .contracts import (
    Anchors,
    CaseInfo,
    Hypothesis,
    HypothesisStatus,
    ProbeRequest,
    ProbeResult,
    SearchMode,
    SearchState,
    SiteGraph,
    StepResult,
    Visit,
)
from .contracts.params import RoutingParams
from .expand import _probe_id, expand_dormant_watch_probes, expand_hypotheses
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
) -> list[Hypothesis]:
    if at is None:
        return []
    if camera is not None:
        cameras = [camera] if camera in graph.cameras else []
        if not cameras:
            logger.warning("Ignoring last_known on unknown camera %s", camera)
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
    )
    return SearchState(
        case_id=case.case_id,
        profile=case.profile,
        site_version=graph.version,
        params_version=params.version,
        window_start=case.window_start,
        window_end=case.window_end,
        hypotheses=hypotheses,
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
            anchors.last_known_camera, anchors.last_known_zone, anchors.last_known_time, graph
        )

    previous_pending = {probe.probe_id for probe in state.pending_probes}
    hypotheses, rejected, state = update_hypotheses(
        state, valid_results, graph, anchors, params, now
    )
    retries = [probe for probe in state.pending_probes if probe.probe_id not in previous_pending]

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
            from .contracts import StopReason
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
    candidates = sorted(
        route_probes + watch_probes,
        key=lambda item: (-item.priority, item.camera, item.t_from, item.probe_id),
    )
    capacity = max(params.K - len(state.pending_probes), 0)
    new_probes = candidates[:capacity]
    state.pending_probes.extend(new_probes)
    state.pending_probes.sort(
        key=lambda item: (-item.priority, item.camera, item.t_from, item.probe_id)
    )
    emitted = sorted(
        retries + new_probes,
        key=lambda item: (-item.priority, item.camera, item.t_from, item.probe_id),
    )
    state.mode = determine_mode(state, hypotheses, now, graph)
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
