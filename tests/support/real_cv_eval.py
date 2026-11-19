"""Schemas, metrics and reports for the NVIDIA 12-camera evaluation."""
from __future__ import annotations

import html
import json
import math
import os
import shutil
import time
from collections import Counter
from pathlib import Path
from statistics import median
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from javes_contracts import Estimate, SiteGraph
from javes_routing.core.ground import floor_scale, point_to_polyline_distance_m
from javes_routing.core.site import validate_corridor_geometry


ALL_NVIDIA_CAMERAS = tuple(f"Cam{index:03d}" for index in range(1, 13))
EVALUATION_REPORT_SCHEMA_VERSION = "javes-real-cv-evaluation.v1"


class EvalModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvaluationProfile(EvalModel):
    sample_fps: float = 5.0
    image_size: int = 1280
    detector_weights: str = "models/yolo26n.pt"
    reid_weights: str = "models/osnet_ain_x1_0_msmt17.pt"
    tracker: str = "bytetrack.yaml"
    track_lost_buffer_s: float = 8.0

    @model_validator(mode="after")
    def _fixed_acceptance_profile(self) -> EvaluationProfile:
        if self.sample_fps != 5.0 or self.image_size != 1280:
            raise ValueError("real-CV acceptance requires the shared 5 FPS / 1280 px profile")
        return self


class ReferenceCrop(EvalModel):
    camera_id: str
    time_s: float = Field(ge=0.0)
    bbox_xyxy: tuple[int, int, int, int]
    coordinate_frame_px: tuple[int, int]

    @model_validator(mode="after")
    def _ordered_bbox(self) -> ReferenceCrop:
        x1, y1, x2, y2 = self.bbox_xyxy
        width, height = self.coordinate_frame_px
        if width <= 0 or height <= 0:
            raise ValueError("reference coordinate frame must be positive")
        if x2 <= x1 or y2 <= y1:
            raise ValueError("reference bbox must have positive area")
        if x1 < 0 or y1 < 0 or x2 > width or y2 > height:
            raise ValueError("reference bbox must fit its coordinate frame")
        return self

    def bbox_for_frame(
        self, frame_size_px: tuple[int, int]
    ) -> tuple[int, int, int, int]:
        source_width, source_height = self.coordinate_frame_px
        target_width, target_height = frame_size_px
        sx, sy = target_width / source_width, target_height / source_height
        x1, y1, x2, y2 = self.bbox_xyxy
        return (
            round(x1 * sx),
            round(y1 * sy),
            round(x2 * sx),
            round(y2 * sy),
        )


class ScenarioSystemInput(EvalModel):
    reference: ReferenceCrop
    last_known_camera: str
    last_known_time_s: float = Field(ge=0.0)
    window_start_s: float = Field(ge=0.0)
    window_end_s: float = Field(gt=0.0)
    available_cameras: list[str]
    unavailable_cameras: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _valid_input(self) -> ScenarioSystemInput:
        if self.window_end_s <= self.window_start_s:
            raise ValueError("scenario window must be ordered")
        available = set(self.available_cameras)
        unavailable = set(self.unavailable_cameras)
        if available & unavailable:
            raise ValueError("camera cannot be both available and unavailable")
        if self.reference.camera_id not in available:
            raise ValueError("reference camera must be available")
        if self.last_known_camera not in available:
            raise ValueError("last-known camera must be available")
        self.available_cameras = sorted(available)
        self.unavailable_cameras = sorted(unavailable)
        return self


class OracleSighting(EvalModel):
    camera_id: str
    t_in_s: float = Field(ge=0.0)
    t_out_s: float = Field(ge=0.0)

    @model_validator(mode="after")
    def _ordered(self) -> OracleSighting:
        if self.t_out_s < self.t_in_s:
            raise ValueError("oracle sighting must be ordered")
        return self


class OracleCheckpoint(EvalModel):
    camera_id: str
    time_s: float = Field(ge=0.0)
    bbox_xyxy: tuple[int, int, int, int] | None = None
    foot_point_px: tuple[float, float]
    coordinate_frame_px: tuple[int, int]
    hidden_from_system: bool = False
    calibration_error_m: float = Field(default=0.5, ge=0.0)

    @model_validator(mode="after")
    def _valid_coordinate_frame(self) -> OracleCheckpoint:
        width, height = self.coordinate_frame_px
        if width <= 0 or height <= 0:
            raise ValueError("checkpoint coordinate frame must be positive")
        x, y = self.foot_point_px
        if x < 0 or y < 0 or x > width or y > height:
            raise ValueError("checkpoint foot point must fit its coordinate frame")
        return self


class OracleBranchOutcome(EvalModel):
    lost_at_s: float = Field(ge=0.0)
    checkpoint_times_s: list[float]
    true_edge_ids: list[str] = Field(
        description=(
            "Permitted first walk-graph edges after the last observed position; "
            "downstream edges on the same route are not separate branch choices."
        )
    )
    kind: Literal["BLIND_GAP", "NO_RETURN", "REENTRY", "OUTSIDE"]


class ScenarioOracle(EvalModel):
    global_identity: str
    sightings: list[OracleSighting]
    checkpoints: list[OracleCheckpoint]
    negative_checkpoints: list[OracleCheckpoint] = Field(default_factory=list)
    branch_outcomes: list[OracleBranchOutcome] = Field(default_factory=list)

    @model_validator(mode="after")
    def _has_independent_evidence(self) -> ScenarioOracle:
        if not self.sightings or not self.checkpoints:
            raise ValueError("oracle requires sightings and fixed checkpoints")
        return self


class EvaluationScenario(EvalModel):
    id: str
    split: Literal["development", "holdout"]
    system_input: ScenarioSystemInput
    oracle: ScenarioOracle
    tags: list[str] = Field(default_factory=list)
    quarantined: bool = False
    quarantine_reason: str | None = None

    @model_validator(mode="after")
    def _valid_quarantine(self) -> EvaluationScenario:
        if self.quarantined and not self.quarantine_reason:
            raise ValueError("quarantined scenario requires a reason")
        self.tags = sorted(set(self.tags))
        return self


class EvaluationManifest(EvalModel):
    manifest_version: Literal["nvidia-12cam-eval.v1"]
    dataset: Literal["NVIDIA-MTMC-12cam"]
    profile: EvaluationProfile
    scenarios: list[EvaluationScenario]

    @model_validator(mode="after")
    def _acceptance_coverage(self) -> EvaluationManifest:
        ids = [item.id for item in self.scenarios]
        if len(ids) != len(set(ids)):
            raise ValueError("evaluation scenario ids must be unique")
        active = [item for item in self.scenarios if not item.quarantined]
        splits = Counter(item.split for item in active)
        if splits != {"development": 8, "holdout": 4}:
            raise ValueError("manifest requires exactly 8 development and 4 holdout targets")
        identities = {item.oracle.global_identity for item in active}
        if len(identities) < 12:
            raise ValueError("manifest requires at least 12 independent people")
        covered = {
            sighting.camera_id
            for item in active
            for sighting in item.oracle.sightings
        }
        if covered != set(ALL_NVIDIA_CAMERAS):
            missing = sorted(set(ALL_NVIDIA_CAMERAS) - covered)
            raise ValueError(f"oracle sightings must cover all 12 cameras; missing {missing}")
        outcomes = Counter(
            outcome.kind
            for item in active
            for outcome in item.oracle.branch_outcomes
        )
        if outcomes["BLIND_GAP"] < 4:
            raise ValueError("manifest requires at least four blind-gap outcomes")
        if outcomes["NO_RETURN"] < 2 or outcomes["REENTRY"] < 2:
            raise ValueError("manifest requires two no-return and two re-entry outcomes")
        return self


def load_evaluation_manifest(path: Path) -> EvaluationManifest:
    return EvaluationManifest.model_validate_json(path.read_text(encoding="utf-8"))


def system_payload(scenario: EvaluationScenario) -> dict:
    """The only scenario data allowed to cross into Main/Perception/Routing."""
    return scenario.system_input.model_dump(mode="json")


class ReachabilityMetric(EvalModel):
    contained: int
    total: int
    negative_rejected: int
    negative_total: int
    probability_mass: float
    true_branch_rank: int | None = None
    geometry_errors: list[str] = Field(default_factory=list)
    early_arrival_errors: list[str] = Field(default_factory=list)

    @property
    def containment_ratio(self) -> float:
        return self.contained / self.total if self.total else 0.0


def checkpoint_map_position(
    checkpoint: OracleCheckpoint,
    graph: SiteGraph,
) -> tuple[float, float, str, float]:
    camera = graph.cameras[checkpoint.camera_id]
    calibration = camera.calibration
    if calibration is None:
        raise ValueError(f"{checkpoint.camera_id} has no immutable calibration")
    source_width, source_height = checkpoint.coordinate_frame_px
    target_width, target_height = calibration.frame_size_px
    x_px = checkpoint.foot_point_px[0] * target_width / source_width
    y_px = checkpoint.foot_point_px[1] * target_height / source_height
    matrix = calibration.homography
    denominator = matrix[2][0] * x_px + matrix[2][1] * y_px + matrix[2][2]
    if abs(denominator) < 1e-12:
        raise ValueError("oracle checkpoint projects to infinity")
    x = (matrix[0][0] * x_px + matrix[0][1] * y_px + matrix[0][2]) / denominator
    y = (matrix[1][0] * x_px + matrix[1][1] * y_px + matrix[1][2]) / denominator
    return x, y, calibration.floor_id, checkpoint.calibration_error_m


def checkpoint_in_reachable_area(
    checkpoint: OracleCheckpoint,
    estimate: Estimate,
    graph: SiteGraph,
) -> bool:
    x, y, floor_id, error_m = checkpoint_map_position(checkpoint, graph)
    scale = floor_scale(graph, floor_id)
    if scale is None:
        return False
    floor = next(
        (item for item in graph.map.floors if item.id == floor_id),
        None,
    ) if graph.map is not None else None
    if floor is not None and any(
        _point_in_polygon((x, y), polygon)
        for polygon in floor.blocked_polygons
    ):
        return False
    for segment in estimate.reachable_segments:
        if segment.floor_id != floor_id:
            continue
        distance = point_to_polyline_distance_m((x, y), segment.points, scale)
        if distance is not None and distance <= segment.width_m / 2.0 + error_m:
            return True
    return False


def reachable_geometry_errors(
    estimate: Estimate,
    graph: SiteGraph,
) -> list[str]:
    """Return any emitted corridor that violates immutable floor geometry."""
    floors = {
        floor.id: floor
        for floor in (graph.map.floors if graph.map is not None else [])
    }
    errors: list[str] = []
    for segment in estimate.reachable_segments:
        if len(segment.points) < 2:
            errors.append(f"{segment.edge_id}: corridor has fewer than two points")
            continue
        if segment.floor_id is None or segment.floor_id not in floors:
            errors.append(f"{segment.edge_id}: unknown floor {segment.floor_id!r}")
            continue
        try:
            validate_corridor_geometry(
                f"Reachable segment '{segment.edge_id}'",
                segment.points,
                segment.width_m,
                floors[segment.floor_id],
            )
        except ValueError as exc:
            errors.append(str(exc))
    return errors


def early_arrival_errors(
    checkpoints: list[OracleCheckpoint],
    estimate: Estimate,
    graph: SiteGraph,
    *,
    max_speed_mps: float,
) -> list[str]:
    """Reject corridors that reach a checkpoint faster than a straight-line bound."""
    origin = estimate.reachable_origin
    horizon_s = estimate.reachable_horizon_s
    if origin is None or horizon_s is None or max_speed_mps <= 0.0:
        return []
    scale = floor_scale(graph, origin.floor_id)
    if scale is None:
        return []
    errors: list[str] = []
    for checkpoint in checkpoints:
        if not checkpoint_in_reachable_area(checkpoint, estimate, graph):
            continue
        x, y, floor_id, checkpoint_error_m = checkpoint_map_position(
            checkpoint, graph
        )
        if floor_id != origin.floor_id:
            continue
        lower_bound_m = max(
            math.hypot(x - origin.x, y - origin.y) * scale
            - origin.error_m
            - checkpoint_error_m,
            0.0,
        )
        minimum_s = lower_bound_m / max_speed_mps
        if horizon_s + 1e-6 < minimum_s:
            errors.append(
                f"{checkpoint.camera_id}@{checkpoint.time_s:.3f}s appears after "
                f"{horizon_s:.3f}s but requires at least {minimum_s:.3f}s"
            )
    return errors


def _point_in_polygon(
    point: tuple[float, float], polygon: list[tuple[float, float]]
) -> bool:
    x, y = point
    inside = False
    previous = polygon[-1]
    for current in polygon:
        x1, y1 = previous
        x2, y2 = current
        if (y1 > y) != (y2 > y):
            boundary_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < boundary_x:
                inside = not inside
        previous = current
    return inside


def evaluate_reachability(
    estimate: Estimate,
    graph: SiteGraph,
    positive: list[OracleCheckpoint],
    negative: list[OracleCheckpoint],
    *,
    true_edge_ids: list[str] | None = None,
    max_speed_mps: float = 2.0,
) -> ReachabilityMetric:
    mass = sum(item.probability for item in estimate.reachable_nodes)
    if estimate.reachable_nodes and not math.isclose(mass, 1.0, abs_tol=1e-6):
        raise AssertionError(f"reachable probability mass is {mass}, expected 1")
    contained = sum(
        checkpoint_in_reachable_area(item, estimate, graph) for item in positive
    )
    negative_rejected = sum(
        not checkpoint_in_reachable_area(item, estimate, graph) for item in negative
    )
    geometry_errors = reachable_geometry_errors(estimate, graph)
    timing_errors = early_arrival_errors(
        positive,
        estimate,
        graph,
        max_speed_mps=max_speed_mps,
    )
    rank = None
    if true_edge_ids:
        ranked = sorted(
            estimate.reachable_segments,
            key=lambda item: (-item.probability, item.edge_id),
        )
        ranks = [
            index + 1
            for index, segment in enumerate(ranked)
            if segment.edge_id in set(true_edge_ids)
        ]
        rank = min(ranks) if ranks else None
    return ReachabilityMetric(
        contained=contained,
        total=len(positive),
        negative_rejected=negative_rejected,
        negative_total=len(negative),
        probability_mass=mass,
        true_branch_rank=rank,
        geometry_errors=geometry_errors,
        early_arrival_errors=timing_errors,
    )


class ScenarioEvaluation(EvalModel):
    scenario_id: str
    split: Literal["development", "holdout"]
    passed: bool
    positional_errors_m: list[float] = Field(default_factory=list)
    false_associations: int = 0
    route_associations: int = 0
    reachability: list[ReachabilityMetric] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
    cache_hit: bool = False
    cache_hits: int = Field(default=0, ge=0)
    cache_misses: int = Field(default=0, ge=0)
    duration_s: float = Field(default=0.0, ge=0.0)


class EvaluationSummary(EvalModel):
    schema_version: Literal["javes-real-cv-evaluation.v1"] = (
        EVALUATION_REPORT_SCHEMA_VERSION
    )
    scenarios: list[ScenarioEvaluation]
    active_scenarios: int = Field(ge=0)
    passed_scenarios: int = Field(ge=0)
    failed_scenarios: int = Field(ge=0)
    quarantined_scenarios: int = Field(default=0, ge=0)
    extraction_cache_hits: int = Field(default=0, ge=0)
    extraction_cache_misses: int = Field(default=0, ge=0)
    duration_s: float = Field(default=0.0, ge=0.0)
    median_position_error_m: float
    p95_position_error_m: float
    false_identity_rate: float
    hidden_point_containment: float
    top2_branch_recall: float
    passed: bool


def aggregate_evaluation(
    results: list[ScenarioEvaluation],
    *,
    quarantined_scenarios: int = 0,
    extraction_cache_hits: int | None = None,
    extraction_cache_misses: int | None = None,
    duration_s: float | None = None,
) -> EvaluationSummary:
    errors = sorted(
        error for result in results for error in result.positional_errors_m
    )
    p95_index = max(math.ceil(len(errors) * 0.95) - 1, 0)
    median_error = median(errors) if errors else math.inf
    p95_error = errors[p95_index] if errors else math.inf
    false_count = sum(item.false_associations for item in results)
    association_count = sum(item.route_associations for item in results)
    reachability = [metric for item in results for metric in item.reachability]
    contained = sum(metric.contained for metric in reachability)
    checkpoints = sum(metric.total for metric in reachability)
    ranked = [metric for metric in reachability if metric.true_branch_rank is not None]
    top2 = sum((metric.true_branch_rank or 99) <= 2 for metric in ranked)
    containment = contained / checkpoints if checkpoints else 0.0
    top2_recall = top2 / len(ranked) if ranked else 0.0
    false_rate = false_count / association_count if association_count else 0.0
    reachable_geometry_valid = all(
        not metric.geometry_errors and not metric.early_arrival_errors
        for metric in reachability
    )
    passed = bool(results) and all(item.passed for item in results) and all(
        (
            median_error <= 0.75,
            p95_error <= 1.5,
            false_rate <= 0.05,
            containment >= 0.90,
            top2_recall >= 0.90,
            reachable_geometry_valid,
        )
    )
    passed_scenarios = sum(item.passed for item in results)
    return EvaluationSummary(
        scenarios=results,
        active_scenarios=len(results),
        passed_scenarios=passed_scenarios,
        failed_scenarios=len(results) - passed_scenarios,
        quarantined_scenarios=quarantined_scenarios,
        extraction_cache_hits=(
            sum(item.cache_hits for item in results)
            if extraction_cache_hits is None
            else extraction_cache_hits
        ),
        extraction_cache_misses=(
            sum(item.cache_misses for item in results)
            if extraction_cache_misses is None
            else extraction_cache_misses
        ),
        duration_s=(
            sum(item.duration_s for item in results)
            if duration_s is None
            else duration_s
        ),
        median_position_error_m=median_error,
        p95_position_error_m=p95_error,
        false_identity_rate=false_rate,
        hidden_point_containment=containment,
        top2_branch_recall=top2_recall,
        passed=passed,
    )


def write_evaluation_report(output: Path, summary: EvaluationSummary) -> None:
    output.mkdir(parents=True, exist_ok=True)
    rows = "".join(
        "<tr>"
        f"<td><a href='{html.escape(item.scenario_id)}/index.html'>{html.escape(item.scenario_id)}</a></td>"
        f"<td>{item.split}</td><td>{'PASS' if item.passed else 'FAIL'}</td>"
        f"<td>{'hit' if item.cache_hit else 'cold'}</td>"
        f"<td>{html.escape('; '.join(item.failures))}</td></tr>"
        for item in summary.scenarios
    )
    payload = json.dumps(summary.model_dump(mode="json"), indent=2)
    page = f"""<!doctype html><html><head><meta charset='utf-8'><title>JAVES real-CV evaluation</title>
<style>body{{font:14px system-ui;max-width:1400px;margin:auto;padding:24px}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #bbb;padding:7px;text-align:left}}pre{{background:#f1f5f9;padding:16px}}</style></head>
<body><h1>NVIDIA 12-camera real-CV evaluation</h1><p>Overall: <strong>{'PASS' if summary.passed else 'FAIL'}</strong></p>
<table><thead><tr><th>Scenario</th><th>Split</th><th>Result</th><th>Extraction</th><th>Failures</th></tr></thead><tbody>{rows}</tbody></table>
<h2>Metrics</h2><pre>{html.escape(payload)}</pre></body></html>"""
    (output / "report.json").write_text(payload, encoding="utf-8")
    (output / "index.html").write_text(page, encoding="utf-8")


def _reference_jpeg(video: Path, reference: ReferenceCrop) -> bytes:
    import cv2

    capture = cv2.VideoCapture(str(video))
    capture.set(cv2.CAP_PROP_POS_MSEC, reference.time_s * 1000.0)
    ok, frame = capture.read()
    capture.release()
    if not ok:
        raise AssertionError(f"cannot read reference from {video}")
    height, width = frame.shape[:2]
    x1, y1, x2, y2 = reference.bbox_for_frame((width, height))
    crop = frame[y1:y2, x1:x2]
    if crop.size == 0:
        raise AssertionError(f"empty reference crop for {reference.camera_id}")
    encoded, jpeg = cv2.imencode(".jpg", crop)
    if not encoded:
        raise AssertionError("cannot encode reference crop")
    return jpeg.tobytes()


def _route_checkpoint_errors(
    result,
    scenario: EvaluationScenario,
    graph: SiteGraph,
    base: datetime,
) -> tuple[list[float], int, int]:
    best = result.best
    if best is None:
        return [math.inf] * len(scenario.oracle.checkpoints), 0, 0
    errors: list[float] = []
    route_visits = [
        visit for visit in best.visits if visit.tracklet_ids
    ]
    associations = len(route_visits)
    false_associations = sum(
        not any(
            sighting.camera_id == visit.camera
            and visit.t_out
            >= base + timedelta(seconds=sighting.t_in_s - 0.25)
            and visit.t_in
            <= base + timedelta(seconds=sighting.t_out_s + 0.25)
            for sighting in scenario.oracle.sightings
        )
        for visit in route_visits
    )
    for checkpoint in scenario.oracle.checkpoints:
        if checkpoint.hidden_from_system:
            continue
        x, y, floor_id, _ = checkpoint_map_position(checkpoint, graph)
        scale = floor_scale(graph, floor_id)
        candidates = [
            point
            for visit in best.visits
            if visit.camera == checkpoint.camera_id
            for point in visit.projected_trajectory
            if point.floor_id == floor_id
            and abs((point.timestamp - base).total_seconds() - checkpoint.time_s)
            <= 2.0
        ]
        if not candidates or scale is None:
            errors.append(math.inf)
            continue
        error = min(math.hypot(point.x - x, point.y - y) * scale for point in candidates)
        errors.append(error)
    return errors, false_associations, associations


def run_real_cv_evaluation(
    repository_root: Path,
    manifest: EvaluationManifest,
    output: Path,
) -> EvaluationSummary:
    """Run the cached target-independent CV and pure Routing evaluation.

    The existing API-first cold E2E remains the nightly transport test.  This
    runner is deliberately target-dense: extraction cache entries are shared
    between scenarios, while gallery scoring and Routing state never are.
    """
    import cv2

    evaluation_started = time.monotonic()

    from javes_contracts import (
        Anchors,
        ArtifactRef,
        CaseInfo,
        PerceptionProbeJob,
        ProbeResult,
        ProbeStatus,
        TargetGalleryCommand,
    )
    from javes_archive import NvidiaArchiveRegistry
    from javes_main.adapters.artifacts import InMemoryArtifactStore
    from javes_perception import CameraSource, PerceptionSettings, PerceptionWorker
    from javes_perception.core.extraction import FilesystemExtractionCache
    from javes_perception.runtime.video import LocalVideoArchive
    from javes_routing.core.params import RoutingParams
    from javes_routing.core.predict import compute_estimate
    from javes_routing.core.render import render_route_maps
    from javes_routing.core.site import parse_site
    from javes_routing.core.step import init_state, step
    from tests.support.nvidia_12cam_site import (
        PARAMS_VERSION,
        calibration_checksums,
        canonical_site,
    )
    from tests.support.real_cv_e2e import ReportVideo, publish_inspectable_report

    fixture = repository_root / "data/nvidia_mtmc/mtmc_12cam"
    archive_registry = NvidiaArchiveRegistry(fixture)
    video_paths = {
        camera: fixture / archive_registry.manifest.cameras[camera].relative_path
        for camera in ALL_NVIDIA_CAMERAS
    }
    if not all(path.is_file() for path in video_paths.values()):
        raise AssertionError("NVIDIA 12-camera videos are incomplete")
    site_raw = canonical_site(repository_root)
    graph = parse_site(site_raw)
    base = archive_registry.manifest.timeline_origin
    cameras = {
        camera: CameraSource(path=str(path), start=base, archive_id=f"nvidia:{camera}")
        for camera, path in video_paths.items()
    }
    profile = manifest.profile
    settings = PerceptionSettings(
        cameras=cameras,
        detector_weights=str(repository_root / profile.detector_weights),
        reid_weights=str(repository_root / profile.reid_weights),
        tracker=profile.tracker,
        sample_fps=profile.sample_fps,
        imgsz=profile.image_size,
        track_lost_buffer_s=profile.track_lost_buffer_s,
        min_crop_height=28,
        embed_every=3,
        camera_calibration_checksums=calibration_checksums(site_raw),
    )
    refresh = os.environ.get("JAVES_REAL_CV_REFRESH") == "1"
    cache = FilesystemExtractionCache(
        output.parent / "extraction-cache", refresh=refresh
    )
    artifacts = InMemoryArtifactStore()
    worker = PerceptionWorker(
        settings,
        artifacts,
        archive=LocalVideoArchive(cameras),
        extraction_cache=cache,
    )
    params = RoutingParams(
        version=PARAMS_VERSION,
        K=12,
        B=5,
        max_retries=0,
        historical_shell_s=60.0,
        max_gap_s=30.0,
        max_walk_snap_m=3.0,
    )
    map_bytes = (fixture / "map.png").read_bytes()
    scenario_results: list[ScenarioEvaluation] = []
    for scenario in manifest.scenarios:
        if scenario.quarantined:
            continue
        scenario_started = time.monotonic()
        before_hits = worker.extraction_cache_hits
        before_misses = worker.extraction_cache_misses
        system = scenario.system_input
        case_id = f"eval-{scenario.id}"
        reference_bytes = _reference_jpeg(
            video_paths[system.reference.camera_id], system.reference
        )
        reference_key = f"cases/{case_id}/reference.jpg"
        artifacts.put(reference_key, reference_bytes, "image/jpeg")
        gallery = worker.update_gallery(
            TargetGalleryCommand(
                command_id=f"gallery-{scenario.id}",
                case_id=case_id,
                source="reference",
                image_ref=ArtifactRef(object_key=reference_key),
                model_version=settings.model_version,
            )
        )
        calibration = graph.cameras[system.reference.camera_id].calibration
        if calibration is None:
            raise AssertionError("reference camera requires immutable calibration")
        reference_bbox = system.reference.bbox_for_frame(calibration.frame_size_px)
        footpoint = (
            (reference_bbox[0] + reference_bbox[2]) / 2.0,
            float(reference_bbox[3]),
        )
        case = CaseInfo(
            case_id=case_id,
            target_id=scenario.oracle.global_identity,
            start_time=base + timedelta(seconds=system.window_start_s),
            window_start=base + timedelta(seconds=system.window_start_s),
            window_end=base + timedelta(seconds=system.window_end_s),
            last_known_camera=system.last_known_camera,
            last_known_time=base + timedelta(seconds=system.last_known_time_s),
            last_known_footpoint_px=footpoint,
        )
        state = init_state(case, graph, params)
        anchors = Anchors(
            last_known_camera=system.last_known_camera,
            last_known_time=base + timedelta(seconds=system.last_known_time_s),
            last_known_footpoint_px=footpoint,
        )
        now = base + timedelta(seconds=system.window_end_s)
        result = step(state, [], graph, anchors, params, now)
        result_history = [result]
        all_probe_results: list[ProbeResult] = []
        failures: list[str] = []
        for _round in range(80):
            if result.done:
                break
            if not result.probes:
                result = step(result.state, [], graph, anchors, params, now)
                result_history.append(result)
                continue
            round_results: list[ProbeResult] = []
            for probe in result.probes:
                if probe.camera not in system.available_cameras:
                    round_results.append(
                        ProbeResult(
                            probe_id=probe.probe_id,
                            status=ProbeStatus.UNAVAILABLE,
                            coverage_ratio=0.0,
                            processed_duration_s=0.0,
                            n_tracklets_total=0,
                            error_code="VIDEO_UNAVAILABLE",
                        )
                    )
                    continue
                job = PerceptionProbeJob(
                    job_id=f"{case_id}:{probe.probe_id}",
                    case_id=case_id,
                    target_gallery_ref=gallery.gallery_ref,
                    probe=probe,
                    deadline=now + timedelta(minutes=30),
                )
                probe_result = worker.execute(job, now)
                round_results.append(probe_result)
            # Main may trigger Routing after each independent worker callback.
            # Replay the same immutable results on a throw-away state in
            # observation-completion order so the evaluator can inspect the
            # transient search area before a later sighting closes it.  The
            # authoritative E2E state below still consumes the original batch.
            callback_result = result
            for callback in sorted(
                round_results,
                key=lambda item: (
                    min(
                        (tracklet.t_out for tracklet in item.tracklets),
                        default=now + timedelta(days=1),
                    ),
                    item.probe_id,
                ),
            ):
                if callback_result.done:
                    break
                callback_result = step(
                    callback_result.state,
                    [callback],
                    graph,
                    anchors,
                    params,
                    now,
                )
                result_history.append(callback_result)
            all_probe_results.extend(round_results)
            result = step(result.state, round_results, graph, anchors, params, now)
            result_history.append(result)
        else:
            failures.append("routing exceeded 80 rounds")

        errors, false_count, association_count = _route_checkpoint_errors(
            result, scenario, graph, base
        )
        reachability_metrics = []
        reachability_snapshots = []
        reachability_debug = []
        if result.estimate is not None:
            hidden = [
                item for item in scenario.oracle.checkpoints if item.hidden_from_system
            ]
            hidden_negative = [
                item
                for item in scenario.oracle.negative_checkpoints
                if item.hidden_from_system
            ]
            for outcome in scenario.oracle.branch_outcomes:
                matching = [
                    item
                    for item in hidden
                    if any(abs(item.time_s - value) <= 0.51 for value in outcome.checkpoint_times_s)
                ]
                matching_negative = [
                    item
                    for item in hidden_negative
                    if any(
                        abs(item.time_s - value) <= 0.51
                        for value in outcome.checkpoint_times_s
                    )
                ]
                candidates = [
                    snapshot
                    for snapshot in result_history
                    if snapshot.estimate is not None
                    and snapshot.estimate.reachable_segments
                    and snapshot.estimate.last_observed_time is not None
                ]
                snapshot = min(
                    candidates,
                    key=lambda item: (
                        abs(
                            (
                                item.estimate.last_observed_time
                                - (base + timedelta(seconds=outcome.lost_at_s))
                            ).total_seconds()
                        ),
                        item.state.state_version,
                    ),
                    default=None,
                )
                if snapshot is None:
                    failures.append(
                        f"no reachable snapshot for {outcome.kind} at {outcome.lost_at_s:.1f}s"
                    )
                    continue
                snapshot_delta_s = abs(
                    (
                        snapshot.estimate.last_observed_time
                        - (base + timedelta(seconds=outcome.lost_at_s))
                    ).total_seconds()
                )
                if snapshot_delta_s > 0.6:
                    failures.append(
                        f"no reachable snapshot aligned with {outcome.kind} "
                        f"loss at {outcome.lost_at_s:.1f}s"
                    )
                    continue
                if not matching:
                    failures.append(
                        f"{outcome.kind} at {outcome.lost_at_s:.1f}s has no hidden oracle checkpoint"
                    )
                    continue
                checkpoint_now = base + timedelta(
                    seconds=max(outcome.checkpoint_times_s)
                )
                timed_estimate = compute_estimate(
                    snapshot.state.hypotheses,
                    graph,
                    checkpoint_now,
                    snapshot.state,
                    params,
                )
                timed_snapshot = snapshot.model_copy(
                    update={"estimate": timed_estimate}
                )
                reachability_snapshots.append(timed_snapshot)
                reachability_debug.append(
                    {
                        "kind": outcome.kind,
                        "lost_at_s": outcome.lost_at_s,
                        "checkpoint_times_s": outcome.checkpoint_times_s,
                        "snapshot_last_observed_time": (
                            timed_estimate.last_observed_time.isoformat()
                            if timed_estimate.last_observed_time
                            else None
                        ),
                        "top_segments": [
                            {
                                "edge_id": segment.edge_id,
                                "probability": segment.probability,
                                "progress": segment.progress,
                            }
                            for segment in sorted(
                                timed_estimate.reachable_segments,
                                key=lambda item: (-item.probability, item.edge_id),
                            )[:8]
                        ],
                    }
                )
                metric = evaluate_reachability(
                    timed_estimate,
                    graph,
                    matching,
                    matching_negative,
                    true_edge_ids=outcome.true_edge_ids,
                    max_speed_mps=params.max_speed,
                )
                reachability_metrics.append(metric)
                if metric.containment_ratio < 0.90:
                    failures.append(
                        f"hidden checkpoint outside reachable area at {outcome.checkpoint_times_s}"
                    )
                if metric.true_branch_rank is None or metric.true_branch_rank > 2:
                    failures.append(
                        f"true branch is not top-2 at {outcome.checkpoint_times_s}"
                    )
                if metric.negative_rejected != metric.negative_total:
                    failures.append(
                        f"negative checkpoint included at {outcome.checkpoint_times_s}"
                    )
                if metric.geometry_errors:
                    failures.extend(metric.geometry_errors)
                if metric.early_arrival_errors:
                    failures.extend(metric.early_arrival_errors)
        finite_errors = [item for item in errors if math.isfinite(item)]
        if len(finite_errors) != len(errors):
            failures.append("one or more oracle checkpoints were not associated")
        if finite_errors and median(finite_errors) > 0.75:
            failures.append("median positional error exceeds 0.75 m")
        expects_real_tracklet = any(
            sighting.camera_id in system.available_cameras
            and sighting.t_out_s > system.last_known_time_s + 0.5
            for sighting in scenario.oracle.sightings
        )
        if expects_real_tracklet and association_count == 0:
            failures.append("best route contains no real Perception tracklet")
        if false_count / association_count > 0.05 if association_count else False:
            failures.append("false identity association exceeds 5%")
        presentation_result = (
            max(
                reachability_snapshots,
                key=lambda item: item.estimate.last_observed_time,
            )
            if reachability_snapshots
            else result
        )
        rendered = render_route_maps(
            presentation_result, graph, {"warehouse": map_bytes}
        )
        route_svg = rendered.maps[0].svg.encode()
        scenario_output = output / scenario.id
        run_root = scenario_output / "runs" / "evaluation"
        run_root.mkdir(parents=True, exist_ok=True)
        report = {
            "scenario": scenario.id,
            "split": scenario.split,
            "done": result.done,
            "stop_reason": result.stop_reason,
            "cache_hits": worker.extraction_cache_hits - before_hits,
            "cache_misses_total": worker.extraction_cache_misses,
            "positional_errors_m": errors,
            "reachability_snapshot_state_version": (
                presentation_result.state.state_version
                if presentation_result is not result
                else None
            ),
            "reachability_outcomes": reachability_debug,
            "failures": failures,
        }
        report_videos = [
            ReportVideo(
                camera,
                video_paths[camera],
                "validation-only"
                if camera in system.unavailable_cameras
                else "system input",
            )
            for camera in sorted(
                set(system.available_cameras) | set(system.unavailable_cameras)
            )
        ]
        publish_inspectable_report(
            scenario_output,
            run_root,
            route_svg=route_svg,
            case_view=presentation_result.model_dump(mode="json"),
            probe_results=[item.model_dump(mode="json") for item in all_probe_results],
            report=report,
            videos=report_videos,
            timeline=[
                {
                    "time_s": item.time_s,
                    "camera": item.camera_id,
                    "event": "hidden oracle" if item.hidden_from_system else "oracle checkpoint",
                    "details": "used only after system output",
                }
                for item in scenario.oracle.checkpoints
            ],
        )
        scenario_results.append(
            ScenarioEvaluation(
                scenario_id=scenario.id,
                split=scenario.split,
                passed=not failures,
                positional_errors_m=errors,
                false_associations=false_count,
                route_associations=association_count,
                reachability=reachability_metrics,
                failures=failures,
                cache_hit=worker.extraction_cache_hits > before_hits,
                cache_hits=worker.extraction_cache_hits - before_hits,
                cache_misses=worker.extraction_cache_misses - before_misses,
                duration_s=time.monotonic() - scenario_started,
            )
        )
    summary = aggregate_evaluation(
        scenario_results,
        quarantined_scenarios=sum(item.quarantined for item in manifest.scenarios),
        extraction_cache_hits=worker.extraction_cache_hits,
        extraction_cache_misses=worker.extraction_cache_misses,
        duration_s=time.monotonic() - evaluation_started,
    )
    write_evaluation_report(output, summary)
    return summary
