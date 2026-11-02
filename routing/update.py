"""Deterministic visit construction, gating and beam update."""
from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from .contracts import (
    Anchors,
    Candidate,
    EdgeDef,
    Hypothesis,
    HypothesisStatus,
    ProbeRequest,
    ProbeResult,
    ProbeStatus,
    ProbedWindow,
    RejectedCandidate,
    SearchState,
    SiteGraph,
    Tracklet,
    Transition,
    Visit,
)
from .contracts.params import RoutingParams
from .timemodel import (
    arrival_mass,
    blind_dwell_likelihood,
    edge_time_likelihood,
    outside_dwell_likelihood,
    time_likelihood,
)

logger = logging.getLogger(__name__)


def _make_id(prefix: str, *parts: object) -> str:
    """Stable content-derived identifier; never depends on process state."""
    content = "\x1f".join(str(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(content.encode('utf-8')).hexdigest()[:16]}"


def _reset_counter() -> None:
    """Compatibility no-op retained for old callers."""


def _point_in_polygon(px: float, py: float, polygon: list[tuple[float, float]]) -> bool:
    if len(polygon) < 3:
        return False
    inside = False
    j = len(polygon) - 1
    for i, (xi, yi) in enumerate(polygon):
        xj, yj = polygon[j]
        if ((yi > py) != (yj > py)) and (
            px < (xj - xi) * (py - yi) / (yj - yi) + xi
        ):
            inside = not inside
        j = i
    return inside


def bbox_to_xyxy(bbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    x, y, width, height = bbox
    return x, y, x + width, y + height


def _bbox_center(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    x1, y1, x2, y2 = bbox_to_xyxy(bbox)
    return (x1 + x2) / 2.0, (y1 + y2) / 2.0


def _edges_for_bbox(
    camera_id: str,
    bbox: tuple[float, float, float, float],
    graph: SiteGraph,
) -> list[str]:
    camera = graph.cameras.get(camera_id)
    if camera is None:
        return []
    all_edges = sorted(
        {edge.id for edge in graph.get_edges_from(camera_id)}
        | {edge.id for edge in graph.get_edges_to(camera_id)}
    )
    if not camera.regions:
        return all_edges
    center = _bbox_center(bbox)
    for region in sorted(camera.regions, key=lambda item: item.name):
        if _point_in_polygon(*center, region.polygon):
            return sorted(set(region.edges))
    return all_edges


def _spatially_close(first: Tracklet, second: Tracklet, fraction: float) -> bool:
    x1, y1 = _bbox_center(first.bbox_last)
    x2, y2 = _bbox_center(second.bbox_first)
    distance = math.hypot(x2 - x1, y2 - y1)
    points = [bbox_to_xyxy(first.bbox_last), bbox_to_xyxy(second.bbox_first)]
    width = max(point[2] for point in points) - min(point[0] for point in points)
    height = max(point[3] for point in points) - min(point[1] for point in points)
    local_diagonal = max(math.hypot(width, height), 1.0)
    return distance <= fraction * local_diagonal


def _deduplicate_tracklets(tracklets: Iterable[Tracklet]) -> list[Tracklet]:
    by_id: dict[str, Tracklet] = {}
    for tracklet in sorted(tracklets, key=lambda item: (item.id, item.t_out, item.open)):
        previous = by_id.get(tracklet.id)
        if previous is None:
            by_id[tracklet.id] = tracklet
            continue
        if previous.camera != tracklet.camera:
            logger.warning("Duplicate tracklet id %s appears on multiple cameras; skipping duplicate", tracklet.id)
            continue
        # A later event closes or lengthens an OPEN tracklet.
        if tracklet.t_out >= previous.t_out:
            by_id[tracklet.id] = tracklet
    return sorted(by_id.values(), key=lambda item: (item.t_in, item.camera, item.id))


def merge_tracklets_to_visits(
    tracklets: list[Tracklet],
    candidates: list[Candidate],
    graph: SiteGraph,
    params: RoutingParams,
    now: datetime,
) -> list[Visit]:
    """Create deterministic visits without merging different people in crowds."""
    valid_tracklets = [item for item in _deduplicate_tracklets(tracklets) if item.camera in graph.cameras]
    for item in tracklets:
        if item.camera not in graph.cameras:
            logger.warning("Skipping tracklet %s from unknown camera %s", item.id, item.camera)
    if not valid_tracklets:
        return []

    score_map: dict[str, list[float]] = {}
    association_map: dict[str, set[str]] = {}
    seen_candidates: set[tuple[str, str | None]] = set()
    seen_candidate_ids: set[str] = set()
    for candidate in sorted(
        candidates,
        key=lambda item: (item.id or "", item.tracklet_id, -item.score),
    ):
        if candidate.id is not None and candidate.id in seen_candidate_ids:
            logger.warning("Skipping duplicate candidate id %s", candidate.id)
            continue
        if candidate.id is not None:
            seen_candidate_ids.add(candidate.id)
        candidate_key = (candidate.tracklet_id, candidate.id)
        if candidate_key in seen_candidates:
            continue
        seen_candidates.add(candidate_key)
        score_map.setdefault(candidate.tracklet_id, []).append(candidate.score)
        if candidate.association_id is not None:
            association_map.setdefault(candidate.tracklet_id, set()).add(candidate.association_id)

    by_camera: dict[str, list[Tracklet]] = {}
    top_tracklets_by_probe: set[str] = set()
    by_probe: dict[str, list[tuple[float, str]]] = {}
    for tracklet in valid_tracklets:
        if tracklet.id in score_map:
            by_probe.setdefault(tracklet.probe_id or tracklet.id, []).append(
                (max(score_map[tracklet.id]), tracklet.id)
            )
    for values in by_probe.values():
        values.sort(key=lambda item: (-item[0], item[1]))
        top_tracklets_by_probe.update(tracklet_id for _, tracklet_id in values[: params.cands_per_probe])
    for tracklet in valid_tracklets:
        # Tracklets without a Candidate do not describe the target.
        if tracklet.id not in score_map:
            logger.info("Skipping tracklet %s without a target candidate", tracklet.id)
            continue
        if tracklet.id not in top_tracklets_by_probe:
            logger.info("Skipping tracklet %s below cands_per_probe limit", tracklet.id)
            continue
        by_camera.setdefault(tracklet.camera, []).append(tracklet)

    visits: list[Visit] = []
    for camera in sorted(by_camera):
        camera_tracklets = sorted(by_camera[camera], key=lambda item: (item.t_in, item.id))
        groups: list[list[Tracklet]] = []
        for tracklet in camera_tracklets:
            if not groups:
                groups.append([tracklet])
                continue
            previous = groups[-1][-1]
            gap = (tracklet.t_in - previous.t_out).total_seconds()
            associated = bool(
                association_map.get(previous.id, set())
                & association_map.get(tracklet.id, set())
            )
            if (
                0 <= gap <= params.merge_gap_s
                and associated
                and _spatially_close(previous, tracklet, params.merge_dist_frac)
            ):
                groups[-1].append(tracklet)
            else:
                groups.append([tracklet])

        for group in groups:
            first, last = group[0], group[-1]
            tracklet_ids = sorted(item.id for item in group)
            scores = sorted(
                (score for item in group for score in score_map.get(item.id, [])),
                reverse=True,
            )
            appearance = sum(scores[:2]) / min(len(scores), 2)
            t_out = now if last.open else last.t_out
            if t_out < first.t_in:
                logger.warning(
                    "Skipping future OPEN tracklet group on %s: t_start=%s now=%s",
                    camera,
                    first.t_in,
                    now,
                )
                continue
            visit_id = _make_id(
                "v",
                camera,
                first.t_in.isoformat(),
                t_out.isoformat(),
                ",".join(tracklet_ids),
            )
            visits.append(
                Visit(
                    id=visit_id,
                    camera=camera,
                    zone=graph.cameras[camera].zone or None,
                    t_in=first.t_in,
                    t_out=t_out,
                    tracklet_ids=tracklet_ids,
                    entry_edges=_edges_for_bbox(camera, first.bbox_first, graph),
                    exit_edges=_edges_for_bbox(camera, last.bbox_last, graph),
                    score=appearance,
                    open=last.open,
                    clipped_in=first.clipped_in,
                    clipped_out=last.clipped_out,
                )
            )
    return sorted(visits, key=lambda item: (item.t_in, item.t_out, item.camera, item.id))


def _scale_edge(edge: EdgeDef, params: RoutingParams, profile: str = "ADULT") -> EdgeDef:
    factors = params.profiles.get(profile, params.profiles["ADULT"])
    return edge.model_copy(
        update={
            "t_min": edge.t_min * factors["tmin_k"],
            "t_typ": edge.t_typ * factors["ttyp_k"],
            "t_max": edge.t_max * factors["tmax_k"],
        }
    )


@dataclass(frozen=True)
class GateResult:
    log_likelihood: float
    edge_ids: tuple[str, ...] = ()
    alternative_paths: tuple[tuple[str, ...], ...] = ()
    via_nodes: tuple[str, ...] = ()
    kind: str = "EDGE"
    reject_reason: str | None = None
    gate_margin_s: float | None = None


def _edge_allowed(edge: EdgeDef, exit_edges: list[str], entry_edges: list[str]) -> bool:
    return (not exit_edges or edge.id in exit_edges) and (not entry_edges or edge.id in entry_edges)


def gate_visit_details(
    last_visit: Visit | None,
    visit: Visit,
    graph: SiteGraph,
    params: RoutingParams,
    now: datetime,
    profile: str = "ADULT",
    probed_windows: list[ProbedWindow] | None = None,
) -> GateResult:
    if last_visit is None:
        return GateResult(0.0)
    dt = (visit.t_in - last_visit.t_out).total_seconds()
    if dt < 0:
        return GateResult(0.0, reject_reason="arrival before previous visit ended", gate_margin_s=dt)
    if visit.camera == last_visit.camera:
        return GateResult(0.0, kind="EDGE")

    skip_lateness = last_visit.clipped_out or visit.clipped_in
    candidates: list[tuple[float, tuple[str, ...], tuple[str, ...], str]] = []
    hard_gate_margins: list[float] = []

    for edge in sorted(graph.edges, key=lambda item: item.id):
        if edge.source != last_visit.camera or edge.target != visit.camera:
            continue
        if not _edge_allowed(edge, last_visit.exit_edges, visit.entry_edges):
            continue
        scaled = _scale_edge(edge, params, profile)
        margin = dt - scaled.t_min
        hard_gate_margins.append(margin)
        if dt < scaled.t_min - params.clock_tolerance_s:
            continue
        likelihood = 1.0 if skip_lateness else edge_time_likelihood(max(dt, scaled.t_min), scaled)
        source_floor, _ = graph.node_floor_xy(edge.source)
        target_floor, _ = graph.node_floor_xy(edge.target)
        kind = "VERTICAL" if edge.vertical or (
            source_floor is not None and target_floor is not None and source_floor != target_floor
        ) else "EDGE"
        candidates.append((max(likelihood, 1e-12), (edge.id,), (), kind))

    # Once a direct transition is physically possible, retain plausible longer
    # camera paths as hidden alternatives.  This does not rescue an impossible
    # direct transition: it only represents ambiguity around a valid sighting.
    if candidates:
        def walk_paths(node: str, visited: tuple[str, ...], path: tuple[EdgeDef, ...]):
            if len(path) >= params.max_hidden_edges:
                return
            for next_edge in sorted(graph.get_edges_from(node), key=lambda item: item.id):
                if next_edge.target == visit.camera:
                    if path:
                        yield path + (next_edge,)
                    continue
                if next_edge.target not in graph.cameras or next_edge.target in visited:
                    continue
                yield from walk_paths(
                    next_edge.target,
                    visited + (next_edge.target,),
                    path + (next_edge,),
                )

        for path in walk_paths(last_visit.camera, (last_visit.camera,), ()):
            if len(path) < 2:
                continue
            if not _edge_allowed(path[0], last_visit.exit_edges, []):
                continue
            if not _edge_allowed(path[-1], [], visit.entry_edges):
                continue
            scaled_path = [_scale_edge(edge, params, profile) for edge in path]
            minimum = sum(edge.t_min for edge in scaled_path)
            typical = sum(edge.t_typ for edge in scaled_path)
            maximum = sum(edge.t_max for edge in scaled_path)
            hard_gate_margins.append(dt - minimum)
            if dt < minimum - params.clock_tolerance_s:
                continue
            likelihood = 1.0 if skip_lateness else time_likelihood(
                max(dt, minimum), minimum, typical, maximum
            )
            intermediate_nodes = tuple(edge.target for edge in path[:-1])
            miss_factor = (1.0 - params.p_det) ** len(intermediate_nodes)
            candidates.append(
                (
                    max(likelihood * miss_factor, 1e-12),
                    tuple(edge.id for edge in path),
                    intermediate_nodes,
                    "GAP",
                )
            )

    # A camera can be traversed without yielding a target tracklet.  Permit a
    # two-edge hidden path only after a fully covered DONE-empty arrival window
    # at the intermediate camera; this is evidence-backed pass-through, not a
    # generic shortcut through the graph.
    if probed_windows:
        for first in sorted(graph.get_edges_from(last_visit.camera), key=lambda item: item.id):
            intermediate = first.target
            if intermediate not in graph.cameras or intermediate == visit.camera:
                continue
            if not _edge_allowed(first, last_visit.exit_edges, []):
                continue
            scaled_first = _scale_edge(first, params, profile)
            expected_from = last_visit.t_out + timedelta(seconds=scaled_first.t_min)
            expected_to = last_visit.t_out + timedelta(seconds=scaled_first.t_max)
            reliable_miss = any(
                window.camera == intermediate
                and window.status == ProbeStatus.DONE
                and not window.found
                and window.coverage_ratio >= 1.0
                and window.t_from <= expected_from
                and window.t_to is not None
                and window.t_to >= expected_to
                for window in probed_windows
            )
            if not reliable_miss:
                continue
            for second in sorted(graph.get_edges_from(intermediate), key=lambda item: item.id):
                if second.target != visit.camera or not _edge_allowed(second, [], visit.entry_edges):
                    continue
                scaled_second = _scale_edge(second, params, profile)
                minimum = scaled_first.t_min + scaled_second.t_min
                typical = scaled_first.t_typ + scaled_second.t_typ
                maximum = scaled_first.t_max + scaled_second.t_max
                hard_gate_margins.append(dt - minimum)
                if dt < minimum - params.clock_tolerance_s:
                    continue
                likelihood = 1.0 if skip_lateness else time_likelihood(
                    max(dt, minimum), minimum, typical, maximum
                )
                candidates.append(
                    (
                        max(likelihood, 1e-12),
                        (first.id, second.id),
                        (intermediate,),
                        "GAP",
                    )
                )

    blind_ids = {node.id for node in graph.blind_nodes}
    for first in sorted(graph.get_edges_from(last_visit.camera), key=lambda item: item.id):
        if first.target not in blind_ids:
            continue
        node = next(item for item in graph.blind_nodes if item.id == first.target)
        for second in sorted(graph.get_edges_from(node.id), key=lambda item: item.id):
            if second.target != visit.camera:
                continue
            scaled_first = _scale_edge(first, params, profile)
            scaled_second = _scale_edge(second, params, profile)
            minimum = scaled_first.t_min + scaled_second.t_min
            hard_gate_margins.append(dt - minimum)
            if dt < minimum - params.clock_tolerance_s:
                continue
            if skip_lateness:
                likelihood = 1.0
            else:
                dwell = max(dt - scaled_first.t_typ - scaled_second.t_typ, 0.1)
                likelihood = (
                    edge_time_likelihood(scaled_first.t_typ, scaled_first)
                    * blind_dwell_likelihood(dwell, node)
                    * edge_time_likelihood(scaled_second.t_typ, scaled_second)
                )
            candidates.append((max(likelihood, 1e-12), (first.id, second.id), (node.id,), "BLIND"))

    if graph.outside is not None:
        outside_paths: list[tuple[EdgeDef, EdgeDef]] = []
        for first in graph.get_edges_from(last_visit.camera):
            if first.target != "OUTSIDE":
                continue
            for second in graph.get_edges_from("OUTSIDE"):
                if second.target == visit.camera:
                    outside_paths.append((first, second))
        for first, second in sorted(outside_paths, key=lambda pair: (pair[0].id, pair[1].id)):
            scaled_first = _scale_edge(first, params, profile)
            scaled_second = _scale_edge(second, params, profile)
            minimum = scaled_first.t_min + scaled_second.t_min
            hard_gate_margins.append(dt - minimum)
            if dt < minimum - params.clock_tolerance_s:
                continue
            dwell = max(dt - scaled_first.t_typ - scaled_second.t_typ, 0.1)
            likelihood = 1.0 if skip_lateness else (
                edge_time_likelihood(scaled_first.t_typ, scaled_first)
                * outside_dwell_likelihood(dwell, graph.outside)
                * edge_time_likelihood(scaled_second.t_typ, scaled_second)
            )
            candidates.append((max(likelihood, 1e-12), (first.id, second.id), ("OUTSIDE",), "OUTSIDE"))
        # The architecture permits implicit OUTSIDE links between entrances.
        if (
            last_visit.camera in graph.outside.entrances
            and visit.camera in graph.outside.entrances
            and dt >= -params.clock_tolerance_s
        ):
            likelihood = 1.0 if skip_lateness else outside_dwell_likelihood(max(dt, 0.1), graph.outside)
            candidates.append((max(likelihood, 1e-12), (), ("OUTSIDE",), "OUTSIDE"))

    if not candidates:
        margin = max(hard_gate_margins) if hard_gate_margins else None
        if margin is not None and margin < -params.clock_tolerance_s:
            reason = (
                f"hard gate: {last_visit.camera}->{visit.camera} "
                f"dt={dt:.1f}s, margin={margin:.1f}s"
            )
        else:
            reason = f"no path from {last_visit.camera} to {visit.camera} (dt={dt:.1f}s)"
        return GateResult(0.0, reject_reason=reason, gate_margin_s=margin)

    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    best = candidates[0]
    alternatives = tuple(item[1] for item in candidates[1:] if item[1] != best[1])
    return GateResult(
        log_likelihood=math.log(best[0]),
        edge_ids=best[1],
        alternative_paths=alternatives,
        via_nodes=best[2],
        kind=best[3],
    )


def gate_visit_to_hypothesis(
    last_visit: Visit | None,
    visit: Visit,
    graph: SiteGraph,
    params: RoutingParams,
    now: datetime,
    profile: str = "ADULT",
) -> tuple[float, str | None, str | None]:
    result = gate_visit_details(last_visit, visit, graph, params, now, profile)
    via = result.via_nodes[0] if result.via_nodes else None
    return result.log_likelihood, via, result.reject_reason


def compute_appearance_log_lik(score: float, clip_min: float = 0.01) -> float:
    appearance = max(min(score, 1.0 - clip_min), clip_min)
    return math.log(appearance / (1.0 - appearance))


def compute_miss_log_lik(p_det: float, f_mass: float = 1.0) -> float:
    return math.log(max(1.0 - p_det * min(max(f_mass, 0.0), 1.0), 1e-12))


def _arrival_mass(
    t_out: datetime,
    edge: EdgeDef,
    probed_windows: list[ProbedWindow],
    now: datetime,
) -> float:
    """Union-weighted probability mass covered by successful probe windows."""
    intervals: list[tuple[float, float, float]] = []
    for window in probed_windows:
        if window.camera != edge.target or window.status != ProbeStatus.DONE:
            continue
        start = max((window.t_from - t_out).total_seconds(), edge.t_min)
        # A terminal result certifies the window reported by Perception.  Do not
        # truncate it to the caller's clock: replayed results may be delivered
        # with a historical/logical ``now``.
        end_time = now if window.t_to is None else window.t_to
        end = (end_time - t_out).total_seconds()
        if end > start:
            intervals.append((start, end, window.coverage_ratio))
    if not intervals:
        return 0.0
    boundaries = sorted({value for start, end, _ in intervals for value in (start, end)})
    mass = 0.0
    for left, right in zip(boundaries, boundaries[1:]):
        coverage = max(
            (ratio for start, end, ratio in intervals if start <= left and right <= end),
            default=0.0,
        )
        if coverage:
            mass += coverage * arrival_mass(left, right, edge)
    return min(max(mass, 0.0), 1.0)


def recompute_hypothesis_score(
    hypothesis: Hypothesis,
    graph: SiteGraph,
    params: RoutingParams,
    probed_windows: list[ProbedWindow],
    now: datetime,
    profile: str = "ADULT",
) -> float:
    score = hypothesis.miss_count * compute_miss_log_lik(params.p_det)
    for index, visit in enumerate(hypothesis.visits):
        if visit.source == "PERCEPTION":
            score += compute_appearance_log_lik(visit.score, params.score_clip_min)
        if index:
            gate = gate_visit_details(
                hypothesis.visits[index - 1], visit, graph, params, now, profile,
                probed_windows,
            )
            if gate.reject_reason is None:
                score += gate.log_likelihood
            elif "GATE_OVERRIDE" in visit.flags:
                score += math.log(max(params.leak_prior, 1e-12))
    if hypothesis.visits and hypothesis.status == HypothesisStatus.ALIVE:
        last = hypothesis.visits[-1]
        if not last.open:
            for edge in graph.get_edges_from(last.camera):
                if edge.target not in graph.cameras:
                    continue
                scaled = _scale_edge(edge, params, profile)
                covered = _arrival_mass(last.t_out, scaled, probed_windows, now)
                if covered:
                    camera_p_det = graph.cameras[edge.target].p_det
                    p_det = params.p_det if camera_p_det is None else camera_p_det
                    score += compute_miss_log_lik(p_det, covered)
    return score


def _result_fingerprint(result: ProbeResult) -> str:
    return hashlib.sha256(result.model_dump_json().encode("utf-8")).hexdigest()


def _retry_probe(probe: ProbeRequest) -> ProbeRequest:
    retry_count = probe.retry_count + 1
    root = probe.retry_of or probe.probe_id
    probe_id = _make_id("p", root, retry_count, probe.camera, probe.t_from.isoformat(), probe.t_to)
    return probe.model_copy(
        update={"probe_id": probe_id, "retry_count": retry_count, "retry_of": root}
    )


def _process_probe_results(
    state: SearchState,
    probe_results: list[ProbeResult],
    graph: SiteGraph,
    params: RoutingParams,
) -> tuple[list[Tracklet], list[Candidate], list[ProbeRequest]]:
    tracklets: list[Tracklet] = []
    candidates: list[Candidate] = []
    retry_probes: list[ProbeRequest] = []
    pending_by_id = {probe.probe_id: probe for probe in state.pending_probes}
    processed = set(state.processed_result_ids)
    finished_probe_ids: set[str] = set()

    for result in sorted(probe_results, key=lambda item: item.probe_id):
        fingerprint = _result_fingerprint(result)
        if fingerprint in processed:
            logger.info("Ignoring duplicate ProbeResult %s", result.probe_id)
            continue
        processed.add(fingerprint)
        matching = pending_by_id.get(result.probe_id)
        valid_result_tracklets: list[Tracklet] = []
        for tracklet in result.tracklets:
            if tracklet.camera not in graph.cameras:
                logger.warning(
                    "Skipping tracklet %s from unknown camera %s",
                    tracklet.id,
                    tracklet.camera,
                )
                continue
            if matching is not None and tracklet.camera != matching.camera:
                logger.warning(
                    "Skipping tracklet %s: camera %s does not match probe camera %s",
                    tracklet.id,
                    tracklet.camera,
                    matching.camera,
                )
                continue
            valid_result_tracklets.append(tracklet)
        valid_tracklet_ids = {tracklet.id for tracklet in valid_result_tracklets}
        valid_result_candidates = [
            candidate for candidate in result.candidates
            if candidate.tracklet_id in valid_tracklet_ids
        ]
        if matching is None:
            logger.warning("ProbeResult %s has no pending probe; accepting observations only", result.probe_id)
        else:
            found = bool(valid_result_candidates)
            duration = result.processed_duration_s
            if duration is None and matching.t_to is not None:
                duration = max((matching.t_to - matching.t_from).total_seconds(), 0.0) * result.coverage_ratio
            miss_coverage = result.coverage_ratio
            if (
                result.status == ProbeStatus.DONE
                and graph.cameras[matching.camera].busy
                and result.n_tracklets_total == 0
                and duration is not None
                and duration >= params.quiet_window_s
            ):
                miss_coverage *= 0.5
            if result.status == ProbeStatus.DONE and result.coverage_ratio < params.min_coverage:
                miss_coverage = 0.0
                state.unobserved_windows.append(
                    {
                        "camera_id": matching.camera,
                        "t_from": matching.t_from.isoformat(),
                        "t_to": "OPEN" if matching.t_to is None else matching.t_to.isoformat(),
                        "reason": "LOW_COVERAGE",
                        "coverage_ratio": result.coverage_ratio,
                    }
                )
            state.probed_windows.append(
                ProbedWindow(
                    camera=matching.camera,
                    t_from=matching.t_from,
                    t_to=matching.t_to,
                    probe_id=result.probe_id,
                    status=result.status,
                    coverage_ratio=miss_coverage,
                    found=found,
                )
            )
            if duration:
                previous_duration = state.processed_duration_s_by_probe.get(result.probe_id, 0.0)
                chargeable_duration = max(duration - previous_duration, 0.0)
                state.camera_minutes_used += chargeable_duration / 60.0
                state.processed_duration_s_by_probe[result.probe_id] = max(
                    previous_duration, duration
                )

            if result.status in (ProbeStatus.FAILED, ProbeStatus.TIMEOUT):
                if matching.retry_count < params.max_retries:
                    retry_probes.append(_retry_probe(matching))
                else:
                    state.unobserved_windows.append(
                        {
                            "camera_id": matching.camera,
                            "t_from": matching.t_from.isoformat(),
                            "t_to": "OPEN" if matching.t_to is None else matching.t_to.isoformat(),
                            "reason": result.status.value,
                        }
                    )
                finished_probe_ids.add(matching.probe_id)
            elif result.status == ProbeStatus.UNAVAILABLE:
                state.unobserved_windows.append(
                    {
                        "camera_id": matching.camera,
                        "t_from": matching.t_from.isoformat(),
                        "t_to": "OPEN" if matching.t_to is None else matching.t_to.isoformat(),
                        "reason": ProbeStatus.UNAVAILABLE.value,
                    }
                )
                finished_probe_ids.add(matching.probe_id)
            elif matching.t_to is not None or result.stream_closed:
                finished_probe_ids.add(matching.probe_id)

        if result.status == ProbeStatus.DONE:
            tracklets.extend(valid_result_tracklets)
            candidates.extend(valid_result_candidates)

    state.processed_result_ids = sorted(processed)
    state.pending_probes = [
        probe for probe in state.pending_probes if probe.probe_id not in finished_probe_ids
    ]
    state.pending_probes.extend(retry_probes)
    state.pending_probes.sort(key=lambda item: (-item.priority, item.camera, item.t_from, item.probe_id))
    return tracklets, candidates, retry_probes


def _all_tracklet_ids(hypothesis: Hypothesis) -> set[str]:
    return {tracklet_id for visit in hypothesis.visits for tracklet_id in visit.tracklet_ids}


def _transition_from_gate(previous: Visit, visit: Visit, gate: GateResult) -> Transition:
    return Transition(
        from_visit_id=previous.id,
        to_visit_id=visit.id,
        edge_ids=list(gate.edge_ids),
        alternative_edge_paths=[list(path) for path in gate.alternative_paths],
        via_nodes=list(gate.via_nodes),
        kind=gate.kind,  # type: ignore[arg-type]
        flags=list(visit.flags),
    )


def _update_existing_tracklet(hypothesis: Hypothesis, visit: Visit) -> Hypothesis | None:
    incoming = set(visit.tracklet_ids)
    for index, existing in enumerate(hypothesis.visits):
        if incoming and incoming == set(existing.tracklet_ids):
            visits = list(hypothesis.visits)
            # Stable visit identity keeps adjacent Transition references valid
            # while an OPEN tracklet lengthens and eventually closes.
            visits[index] = visit.model_copy(update={"id": existing.id})
            return hypothesis.model_copy(
                update={
                    "visits": visits,
                    "current_node": visits[index].camera if index == len(visits) - 1 else hypothesis.current_node,
                    "status": HypothesisStatus.ALIVE if visits[index].open else hypothesis.status,
                }
            )
    return None


def _branch_with_visit(
    hypothesis: Hypothesis,
    visit: Visit,
    gate: GateResult,
    params: RoutingParams,
    confirmed: set[str],
) -> Hypothesis:
    branch_visit = visit
    requires_confirmation = hypothesis.requires_confirmation
    if hypothesis.status == HypothesisStatus.DORMANT:
        flags = sorted(set(visit.flags) | {"LONG_GAP"})
        branch_visit = visit.model_copy(update={"long_gap": True, "flags": flags})
        requires_confirmation = not bool(set(visit.tracklet_ids) & confirmed)
    elif gate.kind == "GAP":
        branch_visit = visit.model_copy(
            update={"flags": sorted(set(visit.flags) | {"PASS_THROUGH"})}
        )
    transition = _transition_from_gate(hypothesis.visits[-1], branch_visit, gate)
    visit_key = ",".join(item.id for item in hypothesis.visits + [branch_visit])
    hypothesis_id = _make_id("h", visit_key, ",".join(transition.edge_ids), hypothesis.id)
    return Hypothesis(
        id=hypothesis_id,
        visits=hypothesis.visits + [branch_visit],
        transitions=hypothesis.transitions + [transition],
        current_node=branch_visit.camera,
        status=HypothesisStatus.ALIVE,
        score=hypothesis.score,
        via_alternatives=hypothesis.via_alternatives + [list(path) for path in gate.alternative_paths],
        miss_count=hypothesis.miss_count,
        requires_confirmation=requires_confirmation,
    )


def _apply_anchors(
    hypotheses: list[Hypothesis],
    state: SearchState,
    anchors: Anchors,
) -> tuple[list[Hypothesis], list[RejectedCandidate]]:
    confirmed = set(state.confirmed_tracklet_ids) | set(anchors.confirm)
    rejected_ids = set(state.rejected_tracklet_ids) | set(anchors.reject)
    kept: list[Hypothesis] = []
    rejected: list[RejectedCandidate] = []
    for hypothesis in hypotheses:
        ids = _all_tracklet_ids(hypothesis)
        banned = sorted(ids & rejected_ids)
        if banned:
            rejected.append(
                RejectedCandidate(
                    tracklet_id=banned[0],
                    hypothesis_id=hypothesis.id,
                    reject_reason=f"contains rejected tracklet(s): {','.join(banned)}",
                )
            )
            continue
        if confirmed and not confirmed.issubset(ids):
            rejected.append(
                RejectedCandidate(
                    hypothesis_id=hypothesis.id,
                    reject_reason="does not contain all confirmed tracklets",
                )
            )
            continue
        if hypothesis.requires_confirmation and confirmed.intersection(ids):
            hypothesis = hypothesis.model_copy(update={"requires_confirmation": False})
        kept.append(hypothesis)
    state.confirmed_tracklet_ids = sorted(confirmed)
    state.rejected_tracklet_ids = sorted(rejected_ids)
    return kept, rejected


def _merge_hypotheses(hypotheses: list[Hypothesis]) -> list[Hypothesis]:
    groups: dict[tuple[str, ...], list[Hypothesis]] = {}
    for hypothesis in hypotheses:
        key = tuple(tracklet_id for visit in hypothesis.visits for tracklet_id in visit.tracklet_ids)
        if not key:
            key = tuple(visit.id for visit in hypothesis.visits)
        groups.setdefault(key, []).append(hypothesis)
    merged: list[Hypothesis] = []
    for key in sorted(groups):
        group = sorted(groups[key], key=lambda item: (-item.score, item.id))
        best = group[0]
        if len(group) > 1 and best.transitions:
            transitions = list(best.transitions)
            last = transitions[-1]
            alternatives = {tuple(path) for path in last.alternative_edge_paths}
            for other in group[1:]:
                if other.transitions:
                    alternatives.add(tuple(other.transitions[-1].edge_ids))
            transitions[-1] = last.model_copy(
                update={"alternative_edge_paths": [list(path) for path in sorted(alternatives) if path]}
            )
            best = best.model_copy(update={"transitions": transitions})
        merged.append(best)
    return merged


def update_hypotheses(
    state: SearchState,
    probe_results: list[ProbeResult],
    graph: SiteGraph,
    anchors: Anchors,
    params: RoutingParams,
    now: datetime,
) -> tuple[list[Hypothesis], list[RejectedCandidate], SearchState]:
    """Apply new evidence and return a deterministic beam."""
    state = state.model_copy(deep=True)
    tracklets, candidates, _ = _process_probe_results(state, probe_results, graph, params)
    calibration_versions = sorted(
        {candidate.calibration_version for candidate in candidates if candidate.calibration_version}
    )
    if state.calibration_version is None and calibration_versions:
        state.calibration_version = calibration_versions[0]
        if len(calibration_versions) > 1:
            logger.warning(
                "Multiple calibration versions in one round; pinning %s and skipping the rest",
                state.calibration_version,
            )
    if state.calibration_version is not None:
        candidates = [
            candidate for candidate in candidates
            if candidate.calibration_version in (None, state.calibration_version)
        ]
    banned = set(state.rejected_tracklet_ids) | set(anchors.reject)
    confirmed = set(state.confirmed_tracklet_ids) | set(anchors.confirm)
    visits = merge_tracklets_to_visits(tracklets, candidates, graph, params, now)
    review_visits = list(state.pending_review_visits)
    for review_visit in review_visits:
        if set(review_visit.tracklet_ids) & confirmed:
            visits.append(review_visit)
    state.pending_review_visits = [
        visit for visit in review_visits
        if not (set(visit.tracklet_ids) & (confirmed | banned))
    ]
    visits = sorted(
        {visit.id: visit for visit in visits}.values(),
        key=lambda item: (item.t_in, item.t_out, item.camera, item.id),
    )
    visits = [visit for visit in visits if not (set(visit.tracklet_ids) & banned)]
    profile = state.profile
    hypotheses = list(state.hypotheses)
    rejected: list[RejectedCandidate] = []

    if not params.use_st:
        for visit in visits:
            hypotheses.append(
                Hypothesis(
                    id=_make_id("h", visit.id),
                    visits=[visit],
                    current_node=visit.camera,
                    status=HypothesisStatus.ALIVE,
                    score=compute_appearance_log_lik(visit.score, params.score_clip_min),
                )
            )
        hypotheses, anchor_rejected = _apply_anchors(hypotheses, state, anchors)
        rejected.extend(anchor_rejected)
        hypotheses = _merge_hypotheses(hypotheses)
        hypotheses = sorted(hypotheses, key=lambda item: (-item.score, item.id))[: params.B]
        state.hypotheses = hypotheses
        return hypotheses, rejected, state

    for visit in visits:
        snapshot = list(hypotheses)
        branches: list[Hypothesis] = []
        replaced_hypothesis_ids: set[str] = set()
        any_compatible = False
        gated_against_existing = False
        for hypothesis in snapshot:
            if hypothesis.status == HypothesisStatus.ENDED or not hypothesis.visits:
                continue
            updated = _update_existing_tracklet(hypothesis, visit)
            if updated is not None:
                branches.append(updated)
                replaced_hypothesis_ids.add(hypothesis.id)
                any_compatible = True
                continue
            if set(visit.tracklet_ids) & _all_tracklet_ids(hypothesis):
                continue
            gate = gate_visit_details(
                hypothesis.visits[-1], visit, graph, params, now, profile,
                state.probed_windows,
            )
            if gate.reject_reason is not None:
                gated_against_existing = True
                tracklet_id = visit.tracklet_ids[0] if visit.tracklet_ids else None
                rejected.append(
                    RejectedCandidate(
                        tracklet_id=tracklet_id,
                        camera_id=visit.camera,
                        appearance_score=visit.score,
                        reject_reason=gate.reject_reason,
                        gate_margin_s=gate.gate_margin_s,
                        hypothesis_id=hypothesis.id,
                    )
                )
                if tracklet_id in confirmed:
                    override_visit = visit.model_copy(update={"flags": sorted(set(visit.flags) | {"GATE_OVERRIDE"})})
                    override_gate = GateResult(
                        log_likelihood=math.log(max(params.leak_prior, 1e-12)),
                        kind="UNKNOWN",
                        via_nodes=("UNKNOWN",),
                    )
                    branches.append(_branch_with_visit(hypothesis, override_visit, override_gate, params, confirmed))
                    any_compatible = True
                continue
            branches.append(_branch_with_visit(hypothesis, visit, gate, params, confirmed))
            any_compatible = True

        hypotheses = [
            hypothesis for hypothesis in hypotheses
            if hypothesis.id not in replaced_hypothesis_ids
        ]
        hypotheses.extend(branches)
        if not any_compatible:
            initial_score = compute_appearance_log_lik(visit.score, params.score_clip_min) + compute_miss_log_lik(params.p_det)
            if initial_score >= params.new_hyp_floor:
                hypotheses.append(
                    Hypothesis(
                        id=_make_id("h", visit.id, "reentry"),
                        visits=[visit],
                        current_node=visit.camera,
                        status=HypothesisStatus.ALIVE,
                        score=initial_score,
                        miss_count=1,
                    )
                )
            else:
                rejected.append(
                    RejectedCandidate(
                        tracklet_id=visit.tracklet_ids[0] if visit.tracklet_ids else None,
                        camera_id=visit.camera,
                        appearance_score=visit.score,
                        reject_reason=f"score {initial_score:.2f} below new_hyp_floor {params.new_hyp_floor}",
                    )
                )
            if gated_against_existing and not (set(visit.tracklet_ids) & confirmed):
                pending_ids = {
                    tracklet_id
                    for pending in state.pending_review_visits
                    for tracklet_id in pending.tracklet_ids
                }
                if not (set(visit.tracklet_ids) & pending_ids):
                    state.pending_review_visits.append(visit)

    hypotheses, anchor_rejected = _apply_anchors(hypotheses, state, anchors)
    rejected.extend(anchor_rejected)
    scored = [
        hypothesis.model_copy(
            update={
                "score": recompute_hypothesis_score(
                    hypothesis, graph, params, state.probed_windows, now, profile
                )
            }
        )
        for hypothesis in hypotheses
    ]
    scored = _merge_hypotheses(scored)
    scored.sort(key=lambda item: (-item.score, item.id))
    if len(scored) > params.B:
        for hypothesis in scored[params.B :]:
            rejected.append(
                RejectedCandidate(
                    hypothesis_id=hypothesis.id,
                    reject_reason=f"pruned: rank > {params.B}",
                )
            )
    state.hypotheses = scored[: params.B]
    state.pending_review_visits.sort(
        key=lambda item: (item.t_in, item.t_out, item.camera, item.id)
    )
    rejected.sort(
        key=lambda item: (
            -(item.appearance_score if item.appearance_score is not None else -1.0),
            item.tracklet_id or "",
            item.hypothesis_id or "",
            item.reject_reason,
        )
    )
    return state.hypotheses, rejected[: params.rejected_top_n], state
