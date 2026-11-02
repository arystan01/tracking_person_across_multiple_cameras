"""Turn route hypotheses into deterministic Perception probes."""
from __future__ import annotations

import hashlib
import math
from datetime import datetime, timedelta

from .contracts import (
    Ambiguity,
    EdgeDef,
    Hypothesis,
    HypothesisStatus,
    ProbeRequest,
    ProbeStatus,
    SearchState,
    SiteGraph,
)
from .contracts.params import RoutingParams
from .timemodel import arrival_mass
from .update import _scale_edge


def _softmax(scores: list[float]) -> list[float]:
    if not scores:
        return []
    pivot = max(scores)
    values = [math.exp(score - pivot) for score in scores]
    total = sum(values)
    return [value / total for value in values]


def _probe_id(
    camera: str,
    t_from: datetime,
    t_to: datetime | None,
    reason: str,
    hypothesis_ids: list[str],
) -> str:
    value = "\x1f".join(
        (
            camera,
            t_from.isoformat(),
            "OPEN" if t_to is None else t_to.isoformat(),
            reason,
            ",".join(sorted(hypothesis_ids)),
        )
    )
    return f"p_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]}"


def _covered(
    state: SearchState,
    camera: str,
    t_from: datetime,
    t_to: datetime | None,
) -> bool:
    for probe in state.pending_probes:
        if probe.camera != camera:
            continue
        if probe.t_from <= t_from and (probe.t_to is None or (t_to is not None and probe.t_to >= t_to)):
            return True
    for window in state.probed_windows:
        if window.camera != camera or window.status != ProbeStatus.DONE:
            continue
        if window.coverage_ratio < 1.0:
            continue
        if window.t_from <= t_from and (window.t_to is None or (t_to is not None and window.t_to >= t_to)):
            return True
    return False


def _done_empty_covers(
    state: SearchState,
    camera: str,
    t_from: datetime,
    t_to: datetime,
) -> bool:
    return any(
        window.camera == camera
        and window.status == ProbeStatus.DONE
        and not window.found
        and window.coverage_ratio >= 1.0
        and window.t_from <= t_from
        and window.t_to is not None
        and window.t_to >= t_to
        for window in state.probed_windows
    )


def _path_window(
    edges: list[EdgeDef],
    t_out: datetime,
    now: datetime,
    tail_factor: float,
    tail: bool,
) -> tuple[datetime, datetime] | None:
    minimum = sum(edge.t_min for edge in edges)
    typical = sum(edge.t_typ for edge in edges)
    maximum = sum(edge.t_max for edge in edges)
    start_s, end_s = (
        (maximum, max(maximum, typical * tail_factor)) if tail else (minimum, maximum)
    )
    start = t_out + timedelta(seconds=start_s)
    end = min(t_out + timedelta(seconds=end_s), now)
    return None if end <= start else (start, end)


def _make_probe(
    state: SearchState,
    camera: str,
    t_from: datetime,
    t_to: datetime | None,
    priority: float,
    reason: str,
    hypothesis_ids: list[str],
) -> ProbeRequest | None:
    if _covered(state, camera, t_from, t_to):
        return None
    ids = sorted(set(hypothesis_ids))
    return ProbeRequest(
        probe_id=_probe_id(camera, t_from, t_to, reason, ids),
        camera=camera,
        t_from=t_from,
        t_to=t_to,
        priority=priority,
        reason=reason,
        hypothesis_ids=ids,
    )


def _merge(probes: list[ProbeRequest]) -> list[ProbeRequest]:
    """Merge overlapping windows on one camera, retaining evidence links."""
    by_camera: dict[str, list[ProbeRequest]] = {}
    for probe in probes:
        by_camera.setdefault(probe.camera, []).append(probe)
    result: list[ProbeRequest] = []
    for camera in sorted(by_camera):
        items = sorted(
            by_camera[camera],
            key=lambda item: (item.t_from, item.t_to is None, item.t_to or item.t_from, item.probe_id),
        )
        groups: list[list[ProbeRequest]] = []
        for item in items:
            if not groups:
                groups.append([item])
            elif groups[-1][-1].t_to is None or item.t_from <= groups[-1][-1].t_to:
                groups[-1].append(item)
            else:
                groups.append([item])
        for group in groups:
            t_from = min(item.t_from for item in group)
            t_to = None if any(item.t_to is None for item in group) else max(
                item.t_to for item in group if item.t_to is not None
            )
            priority = max(item.priority for item in group)
            hypothesis_ids = sorted({value for item in group for value in item.hypothesis_ids})
            reason = "+".join(sorted({item.reason for item in group}))
            result.append(
                ProbeRequest(
                    probe_id=_probe_id(camera, t_from, t_to, reason, hypothesis_ids),
                    camera=camera,
                    t_from=t_from,
                    t_to=t_to,
                    priority=priority,
                    reason=reason,
                    hypothesis_ids=hypothesis_ids,
                )
            )
    return sorted(result, key=lambda item: (-item.priority, item.camera, item.t_from, item.probe_id))


def _camera_paths(graph: SiteGraph, source: str) -> list[tuple[str, list[EdgeDef], str]]:
    paths: list[tuple[str, list[EdgeDef], str]] = []
    blind_ids = {node.id for node in graph.blind_nodes} | {"OUTSIDE"}
    for first in sorted(graph.get_edges_from(source), key=lambda item: item.id):
        if first.target in graph.cameras:
            paths.append((first.target, [first], "ROUTE_EXPANSION"))
        elif first.target in blind_ids:
            for second in sorted(graph.get_edges_from(first.target), key=lambda item: item.id):
                if second.target in graph.cameras:
                    paths.append((second.target, [first, second], "BLIND_EXIT"))
    return paths


def _divergence(first: Hypothesis, second: Hypothesis) -> tuple[list[str], list[str]]:
    first_cameras = [visit.camera for visit in first.visits]
    second_cameras = [visit.camera for visit in second.visits]
    common = 0
    for left, right in zip(first_cameras, second_cameras):
        if left != right:
            break
        common += 1
    cameras = sorted(set(first_cameras[common:]) ^ set(second_cameras[common:]))
    first_edges = {edge for transition in first.transitions[common:] for edge in transition.edge_ids}
    second_edges = {edge for transition in second.transitions[common:] for edge in transition.edge_ids}
    return cameras, sorted(first_edges ^ second_edges)


def expand_hypotheses(
    state: SearchState,
    graph: SiteGraph,
    params: RoutingParams,
    now: datetime,
) -> tuple[list[ProbeRequest], list[Ambiguity]]:
    alive = sorted(
        (item for item in state.hypotheses if item.status == HypothesisStatus.ALIVE),
        key=lambda item: (-item.score, item.id),
    )
    weights = _softmax([item.score for item in alive])
    probes: list[ProbeRequest] = []

    for hypothesis, weight in zip(alive, weights):
        if not hypothesis.visits:
            continue
        visit = hypothesis.visits[-1]
        if visit.open:
            current = _make_probe(
                state, visit.camera, visit.t_in, None,
                weight * params.live_current_priority, "LIVE_CURRENT", [hypothesis.id],
            )
            if current:
                probes.append(current)
        for target, path, reason in _camera_paths(graph, visit.camera):
            scaled = [_scale_edge(edge, params, state.profile) for edge in path]
            if visit.open:
                t_from = visit.t_out + timedelta(seconds=sum(edge.t_min for edge in scaled))
                candidate = _make_probe(
                    state, target, t_from, None, weight, "LIVE_NEIGHBOUR", [hypothesis.id]
                )
                if candidate:
                    probes.append(candidate)
                continue
            window = _path_window(scaled, visit.t_out, now, params.tail_factor, tail=False)
            if len(scaled) == 1 and window is not None and _done_empty_covers(state, target, *window):
                for next_edge in sorted(graph.get_edges_from(target), key=lambda item: item.id):
                    if next_edge.target not in graph.cameras or next_edge.target == visit.camera:
                        continue
                    composite = scaled + [_scale_edge(next_edge, params, state.profile)]
                    pass_window = _path_window(
                        composite, visit.t_out, now, params.tail_factor, tail=False
                    )
                    if pass_window is None:
                        continue
                    pass_probe = _make_probe(
                        state,
                        next_edge.target,
                        pass_window[0],
                        pass_window[1],
                        weight * params.tail_eps,
                        "PASS_THROUGH",
                        [hypothesis.id],
                    )
                    if pass_probe:
                        probes.append(pass_probe)
            tail = False
            if window is None or _covered(state, target, *window):
                window = _path_window(scaled, visit.t_out, now, params.tail_factor, tail=True)
                tail = True
            if window is None:
                continue
            t_from, t_to = window
            if _covered(state, target, t_from, t_to):
                continue
            if len(scaled) == 1:
                mass = arrival_mass(
                    max((t_from - visit.t_out).total_seconds(), scaled[0].t_min),
                    (t_to - visit.t_out).total_seconds(), scaled[0],
                )
            else:
                mass = params.blind_path_priority
            candidate = _make_probe(
                state, target, t_from, t_to,
                weight * max(mass, params.tail_eps if tail else 0.01),
                "TAIL" if tail else reason, [hypothesis.id],
            )
            if candidate:
                probes.append(candidate)

    ambiguities: list[Ambiguity] = []
    for hypothesis, weight in zip(alive, weights):
        if not hypothesis.transitions or len(hypothesis.visits) < 2:
            continue
        transition = hypothesis.transitions[-1]
        alternative_cameras: set[str] = set()
        alternative_edges: set[str] = set()
        for path in transition.alternative_edge_paths:
            alternative_edges.update(path)
            for edge_id in path[:-1]:
                edge = next((item for item in graph.edges if item.id == edge_id), None)
                if edge is not None and edge.target in graph.cameras:
                    alternative_cameras.add(edge.target)
        if alternative_cameras:
            ambiguities.append(
                Ambiguity(
                    hypothesis_ids=[hypothesis.id],
                    diverge_cameras=sorted(alternative_cameras),
                    diverge_edge_ids=sorted(alternative_edges),
                    score_delta=0.0,
                )
            )
            previous, current = hypothesis.visits[-2:]
            for camera in sorted(alternative_cameras):
                probe = _make_probe(
                    state,
                    camera,
                    previous.t_out,
                    current.t_in,
                    weight + params.amb_priority_bonus,
                    "AMBIGUITY",
                    [hypothesis.id],
                )
                if probe:
                    probes.append(probe)
    if len(alive) >= 2:
        first, second = alive[:2]
        delta = abs(first.score - second.score)
        if delta < params.amb_delta:
            cameras, edges = _divergence(first, second)
            if cameras or edges:
                ambiguities.append(
                    Ambiguity(
                        hypothesis_ids=sorted([first.id, second.id]),
                        diverge_cameras=cameras,
                        diverge_edge_ids=edges,
                        score_delta=delta,
                    )
                )
                probes = [
                    probe.model_copy(update={"priority": probe.priority + params.amb_priority_bonus})
                    if probe.camera in cameras else probe
                    for probe in probes
                ]
    return _merge(probes)[: params.K], ambiguities


def expand_dormant_watch_probes(
    state: SearchState,
    graph: SiteGraph,
    params: RoutingParams,
    now: datetime,
) -> list[ProbeRequest]:
    # Long-lived subscriptions must not occupy every K slot while a concrete
    # route is still ALIVE. They are opened once active historical expansion
    # is exhausted and WAITING becomes the operative mode.
    if any(
        hypothesis.status == HypothesisStatus.ALIVE
        for hypothesis in state.hypotheses
    ):
        return []
    probes: list[ProbeRequest] = []
    for hypothesis in sorted(state.hypotheses, key=lambda item: item.id):
        if hypothesis.status != HypothesisStatus.DORMANT or not hypothesis.visits:
            continue
        last = hypothesis.visits[-1]
        if hypothesis.current_node == "OUTSIDE" and graph.outside is not None:
            cameras = sorted(graph.outside.entrances.items(), key=lambda item: (-item[1], item[0]))
            for camera, probability in cameras:
                if camera not in graph.cameras:
                    continue
                priority = probability * (params.dormant_watch_boost if camera == last.camera else 1.0)
                probe = _make_probe(
                    state, camera, last.t_out, None, priority,
                    "OUTSIDE_REENTRY", [hypothesis.id],
                )
                if probe:
                    probes.append(probe)
        else:
            borders: set[str] = set()
            node = next((item for item in graph.blind_nodes if item.id == hypothesis.current_node), None)
            if node is not None:
                borders.update(camera for camera in node.borders if camera in graph.cameras)
            borders.update(
                edge.target for edge in graph.get_edges_from(hypothesis.current_node)
                if edge.target in graph.cameras
            )
            for camera in sorted(borders):
                probe = _make_probe(
                    state, camera, last.t_out, None, params.dormant_border_priority,
                    "BLIND_REENTRY", [hypothesis.id],
                )
                if probe:
                    probes.append(probe)
    return _merge(probes)
