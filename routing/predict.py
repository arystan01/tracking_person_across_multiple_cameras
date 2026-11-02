"""Location estimate, search mode and terminal-state rules."""
from __future__ import annotations

from datetime import datetime, timedelta

from .contracts import (
    Estimate,
    Hypothesis,
    HypothesisStatus,
    ProbeStatus,
    SearchMode,
    SearchState,
    SiteGraph,
    StopReason,
    ZoneEstimate,
)
from .contracts.params import RoutingParams
from .expand import _camera_paths, _softmax
from .update import _scale_edge


def _best(hypotheses: list[Hypothesis]) -> Hypothesis | None:
    eligible = [item for item in hypotheses if not item.requires_confirmation]
    pool = eligible or hypotheses
    return min(pool, key=lambda item: (-item.score, item.id)) if pool else None


def compute_estimate(
    hypotheses: list[Hypothesis],
    graph: SiteGraph,
    now: datetime,
    state: SearchState | None = None,
    params: RoutingParams | None = None,
) -> Estimate:
    """Build a relative (not calibrated) probability distribution."""
    params = params or RoutingParams()
    active = [item for item in hypotheses if item.status != HypothesisStatus.ENDED]
    pool = active or list(hypotheses)
    weights = _softmax([item.score for item in pool])
    probabilities: dict[str, float] = {}
    for hypothesis, weight in zip(pool, weights):
        node = hypothesis.current_node
        if hypothesis.status == HypothesisStatus.DORMANT or node == "OUTSIDE" or any(
            blind.id == node for blind in graph.blind_nodes
        ):
            probabilities[node] = probabilities.get(node, 0.0) + weight
            continue
        paths = _camera_paths(graph, node)
        checked = {
            window.camera for window in (state.probed_windows if state else [])
            if window.status == ProbeStatus.DONE
        }
        neighbours = sorted(
            {camera for camera, _, _ in paths if camera not in checked}
            | {
                edge.target for edge in graph.get_edges_from(node)
                if edge.target == "OUTSIDE" or any(blind.id == edge.target for blind in graph.blind_nodes)
            }
        )
        share = params.estimate_neighbour_share if neighbours else 0.0
        probabilities[node] = probabilities.get(node, 0.0) + weight * (1.0 - share)
        if neighbours:
            per_camera = weight * share / len(neighbours)
            for camera in neighbours:
                probabilities[camera] = probabilities.get(camera, 0.0) + per_camera
    total = sum(probabilities.values())
    zones = [
        ZoneEstimate(zone=node, probability=value / total)
        for node, value in sorted(probabilities.items(), key=lambda item: (-item[1], item[0]))
    ] if total else []
    best = _best(pool)
    last = best.visits[-1] if best and best.visits else None
    return Estimate(
        zones=zones,
        valid_until=None if state and state.done else now + timedelta(seconds=params.heartbeat_s),
        last_observed_camera=last.camera if last else None,
        last_observed_time=last.t_out if last else None,
        unobserved_windows=list(state.unobserved_windows) if state else [],
    )


def determine_mode(
    state: SearchState,
    hypotheses: list[Hypothesis],
    now: datetime,
    graph: SiteGraph,
) -> SearchMode:
    if state.done:
        return SearchMode.PAST_SUMMARY
    best = _best([item for item in hypotheses if item.status != HypothesisStatus.ENDED])
    if best is None:
        return SearchMode.CATCHING_UP
    if best.status == HypothesisStatus.DORMANT or best.current_node == "OUTSIDE" or any(
        node.id == best.current_node for node in graph.blind_nodes
    ):
        return SearchMode.WAITING
    if best.visits and best.visits[-1].open:
        return SearchMode.LIVE_TRACKING
    return SearchMode.CATCHING_UP


def check_stop_conditions(
    state: SearchState,
    hypotheses: list[Hypothesis],
    params: RoutingParams,
    now: datetime,
    operator_closed: bool = False,
) -> tuple[bool, StopReason | None]:
    """Apply the contractually fixed stop-priority order."""
    if operator_closed:
        return True, StopReason.OPERATOR_CLOSED
    if state.camera_minutes_used >= params.camera_minute_budget:
        return True, StopReason.BUDGET_SPENT
    observed = [visit.t_out for hypothesis in hypotheses for visit in hypothesis.visits]
    if observed and (now - max(observed)).total_seconds() >= params.max_gap_s:
        return True, StopReason.MAX_GAP
    if hypotheses and all(item.status == HypothesisStatus.ENDED for item in hypotheses):
        return True, StopReason.ALL_ENDED
    return False, None


def _done_covers(
    state: SearchState,
    camera: str,
    t_from: datetime,
    t_to: datetime,
) -> bool:
    return any(
        window.camera == camera
        and window.status == ProbeStatus.DONE
        and window.coverage_ratio >= 1.0
        and window.t_from <= t_from
        and (window.t_to is None or window.t_to >= t_to)
        for window in state.probed_windows
    )


def update_dormant_and_ended(
    hypotheses: list[Hypothesis],
    state: SearchState,
    graph: SiteGraph,
    params: RoutingParams,
    now: datetime,
) -> list[Hypothesis]:
    """Move an exhausted camera route to a blind node/OUTSIDE watch state."""
    result: list[Hypothesis] = []
    blind_ids = {node.id for node in graph.blind_nodes}
    for hypothesis in hypotheses:
        if hypothesis.status == HypothesisStatus.ENDED or not hypothesis.visits:
            result.append(hypothesis)
            continue
        last = hypothesis.visits[-1]
        if last.open or hypothesis.status == HypothesisStatus.DORMANT:
            result.append(hypothesis)
            continue
        camera_paths = _camera_paths(graph, last.camera)
        exhausted = bool(camera_paths)
        for camera, path, _ in camera_paths:
            scaled = [_scale_edge(edge, params, state.profile) for edge in path]
            start = last.t_out + timedelta(seconds=sum(edge.t_min for edge in scaled))
            end_s = max(sum(edge.t_max for edge in scaled), sum(edge.t_typ for edge in scaled) * params.tail_factor)
            end = last.t_out + timedelta(seconds=end_s)
            if now < end or not _done_covers(state, camera, start, end):
                exhausted = False
                break
        blind_targets = sorted(
            edge.target for edge in graph.get_edges_from(last.camera)
            if edge.target in blind_ids or edge.target == "OUTSIDE"
        )
        if graph.outside is not None and last.camera in graph.outside.entrances:
            blind_targets.append("OUTSIDE")
            blind_targets = sorted(set(blind_targets))
        if exhausted and blind_targets:
            hypothesis = hypothesis.model_copy(
                update={"status": HypothesisStatus.DORMANT, "current_node": blind_targets[0]}
            )
        elif exhausted and not blind_targets:
            hypothesis = hypothesis.model_copy(update={"status": HypothesisStatus.ENDED})
        result.append(hypothesis)
    return result
