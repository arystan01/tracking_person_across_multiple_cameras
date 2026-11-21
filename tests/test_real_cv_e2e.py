"""Inspectable API-first E2E using real CV and the NVIDIA MTMC floor map.

This is intentionally opt-in: it loads the tracked YOLO/OSNet weights and
processes real video. Generated output is kept under ``artifacts/`` so a human
can open the exact SVG returned through Controls after the test finishes.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import xml.etree.ElementTree as ET
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

import pytest

from tests.support.real_cv_e2e import (
    ReportVideo,
    SyncASGIClient as _SyncASGIClient,
    clip_video_window as _clip_video_window,
    nvidia_pixel_to_map_homographies,
    publish_inspectable_report,
    require_real_cv as _require_real_cv,
)


pytestmark = pytest.mark.real_cv

REAL_CV_SAMPLE_FPS = 5.0
REAL_CV_IMGSZ = 1280
REAL_CV_MIN_CROP_HEIGHT = 28
REAL_CV_EMBED_EVERY = 3


@dataclass(frozen=True)
class FullMinuteTarget:
    slug: str
    output_directory: str
    case_id: str
    target_id: str
    label: str
    cameras: tuple[str, str]
    evidence_cameras: tuple[str, ...]
    reference_time_s: float
    reference_xyxy: tuple[int, int, int, int]
    min_route_span_s: float
    visible_at_interval_end: bool
    expected_last_source_cameras: tuple[str, ...]


BURGUNDY_TARGET = FullMinuteTarget(
    slug="burgundy-woman",
    output_directory="full-minute",
    case_id="real-cv-e2e",
    target_id="nvidia-burgundy-woman",
    label="woman in burgundy top and light-blue jeans",
    cameras=("Cam001", "Cam003"),
    evidence_cameras=("Cam001", "Cam003"),
    reference_time_s=5.0,
    reference_xyxy=(260, 720, 410, 1000),
    min_route_span_s=58.0,
    visible_at_interval_end=True,
    expected_last_source_cameras=("Cam003",),
)


BLUE_SHIRT_TARGET = FullMinuteTarget(
    slug="blue-shirt-man",
    output_directory="full-minute-blue-shirt",
    case_id="real-cv-blue-shirt-e2e",
    target_id="nvidia-blue-shirt-man",
    label="man in a light-blue shirt and olive trousers",
    cameras=("Cam001", "Cam003"),
    evidence_cameras=("Cam001", "Cam003"),
    reference_time_s=0.0,
    reference_xyxy=(1340, 160, 1460, 370),
    min_route_span_s=59.0,
    visible_at_interval_end=True,
    expected_last_source_cameras=("Cam001",),
)


RED_DRESS_TARGET = FullMinuteTarget(
    slug="red-dress-woman",
    output_directory="full-minute-red-dress-cam001-cam002",
    case_id="real-cv-red-dress-e2e",
    target_id="nvidia-red-dress-woman",
    label="woman in a bright red dress",
    cameras=("Cam001", "Cam002"),
    evidence_cameras=("Cam001", "Cam002"),
    reference_time_s=30.0,
    reference_xyxy=(535, 30, 595, 180),
    min_route_span_s=40.0,
    visible_at_interval_end=False,
    expected_last_source_cameras=("Cam001",),
)


YELLOW_TOP_TARGET = FullMinuteTarget(
    slug="yellow-top-woman",
    output_directory="full-minute-yellow-top-cam001-cam002",
    case_id="real-cv-yellow-top-e2e",
    target_id="nvidia-yellow-top-woman",
    label="woman in a yellow top and black trousers",
    cameras=("Cam001", "Cam002"),
    evidence_cameras=("Cam001",),
    reference_time_s=30.0,
    reference_xyxy=(805, 755, 890, 1010),
    min_route_span_s=18.0,
    visible_at_interval_end=False,
    expected_last_source_cameras=("Cam001",),
)


def _nvidia_pixel_to_map_homographies(
    repository_root: Path,
) -> dict[str, list[list[float]]]:
    fixture = repository_root / "data/nvidia_mtmc/mtmc_4cam"
    return nvidia_pixel_to_map_homographies(
        fixture, [f"Cam{index:03d}" for index in range(1, 5)]
    )


def _manual_annotations(repository_root: Path, target_slug: str) -> dict:
    path = repository_root / "tests/fixtures/real_cv_full_minute_annotations.json"
    return json.loads(path.read_text(encoding="utf-8"))[target_slug]


def _project_manual_points(
    annotations: list[dict],
    homographies: dict[str, list[list[float]]],
) -> list[dict]:
    import numpy as np

    projected = []
    for annotation in annotations:
        x_px, y_px = annotation["foot_point_px"]
        transform = np.asarray(homographies[annotation["camera"]], dtype=float)
        point = transform @ np.asarray([x_px, y_px, 1.0], dtype=float)
        point /= point[2]
        projected.append(
            {
                **annotation,
                "map_xy": [float(point[0]), float(point[1])],
            }
        )
    return projected


def _checkpoint_errors_m(
    checkpoints: list[dict],
    observed_points: list[dict],
    base: datetime,
    m_per_px: float,
    *,
    inferred_transitions: list[dict] | None = None,
    time_tolerance_s: float = 0.41,
) -> list[float]:
    def point_along_path(
        points: list[list[float]], fraction: float
    ) -> tuple[float, float]:
        lengths = [
            ((right[0] - left[0]) ** 2 + (right[1] - left[1]) ** 2)
            ** 0.5
            for left, right in zip(points, points[1:])
        ]
        remaining = min(max(fraction, 0.0), 1.0) * sum(lengths)
        for (left, right), length in zip(zip(points, points[1:]), lengths):
            if remaining <= length:
                ratio = remaining / length if length else 0.0
                return (
                    left[0] + (right[0] - left[0]) * ratio,
                    left[1] + (right[1] - left[1]) * ratio,
                )
            remaining -= length
        return float(points[-1][0]), float(points[-1][1])

    errors = []
    for checkpoint in checkpoints:
        expected_time = base + timedelta(seconds=checkpoint["time_s"])
        candidates = [
            point
            for point in observed_points
            if abs(
                (
                    datetime.fromisoformat(point["timestamp"]) - expected_time
                ).total_seconds()
            )
            <= time_tolerance_s
        ]
        for transition in inferred_transitions or []:
            geometry = transition.get("geometry")
            if not geometry or len(geometry.get("points", [])) < 2:
                continue
            t_from = datetime.fromisoformat(transition["t_from"])
            t_to = datetime.fromisoformat(transition["t_to"])
            if not (t_from <= expected_time <= t_to) or t_to <= t_from:
                continue
            fraction = (expected_time - t_from).total_seconds() / (
                t_to - t_from
            ).total_seconds()
            x, y = point_along_path(geometry["points"], fraction)
            candidates.append(
                {
                    "timestamp": expected_time.isoformat(),
                    "x": x,
                    "y": y,
                    "source": "INFERRED_GAP",
                }
            )
        assert candidates, (
            f"no observed or inferred route evidence near manual checkpoint "
            f"recorded by {checkpoint['camera']}@{checkpoint['time_s']:.1f}s"
        )
        expected_x, expected_y = checkpoint["map_xy"]
        error = min(
            (
                (point["x"] - expected_x) ** 2
                + (point["y"] - expected_y) ** 2
            )
            ** 0.5
            * m_per_px
            for point in candidates
        )
        errors.append(error)
    return errors


def _write_site_registry(
    run_root: Path,
    repository_root: Path,
    evidence_cameras: tuple[str, str],
) -> Path:
    import yaml

    site_root = run_root / "site-config"
    site_dir = site_root / "sites" / "nvidia-mtmc-4cam" / "real-cv-e2e-v1"
    params_dir = site_root / "routing_params"
    site_dir.mkdir(parents=True)
    params_dir.mkdir(parents=True)
    shutil.copyfile(
        repository_root / "data/nvidia_mtmc/mtmc_4cam/map.png",
        site_dir / "map.png",
    )
    # NVIDIA provides world-to-camera matrices and a world-to-floor-map
    # transform. Their composition gives the immutable pixel-to-map
    # calibration used by Routing for real detector foot-points.
    homographies = _nvidia_pixel_to_map_homographies(repository_root)
    camera_positions = {
        "Cam001": ("warehouse-west", [583.1, 454.2]),
        "Cam002": ("warehouse-west", [668.2, 506.0]),
        "Cam003": ("warehouse-south", [520.9, 735.2]),
        "Cam004": ("warehouse-south", [729.1, 786.3]),
    }
    cameras = {
        camera: {
            "zone": zone,
            "floor": "warehouse",
            "map_xy": map_xy,
            "calibration": {
                "frame_size_px": [1920, 1080],
                "homography": homographies[camera],
                "floor_id": "warehouse",
                "error_m": 0.5,
            },
        }
        for camera, (zone, map_xy) in camera_positions.items()
    }
    left_camera, right_camera = evidence_cameras
    cameras[left_camera]["overlap_camera_ids"] = [right_camera]
    cameras[right_camera]["overlap_camera_ids"] = [left_camera]
    site = {
        "site": "nvidia-mtmc-4cam",
        "version": "real-cv-e2e-v1",
        "timezone": "UTC",
        "map": {
            "floors": [
                {
                    "id": "warehouse",
                    "image": "map.png",
                    "size_px": [1920, 1080],
                    # T_ov2px is pixels per foot; convert map pixels to metres.
                    "m_per_px": 0.3048 / ((9.781818 + 9.785714) / 2.0),
                }
            ]
        },
        "cameras": cameras,
        # Camera edges are for probe planning only. The two evidence cameras
        # overlap, so there is deliberately no pedestrian transition between
        # them.
        "edges": [],
        "walk_graph": {
            "nodes": {
                # This is a site-owned pedestrian centreline, not geometry
                # learned from either target.  It follows the open aisle to
                # the west of the shelving and the open lower cross-aisle.
                "west-100": {"floor_id": "warehouse", "map_xy": [740, 100]},
                "west-180": {"floor_id": "warehouse", "map_xy": [740, 180]},
                "west-260": {"floor_id": "warehouse", "map_xy": [740, 260]},
                "west-340": {"floor_id": "warehouse", "map_xy": [740, 340]},
                "west-420": {"floor_id": "warehouse", "map_xy": [740, 420]},
                "west-500": {"floor_id": "warehouse", "map_xy": [740, 500]},
                "west-580": {"floor_id": "warehouse", "map_xy": [740, 580]},
                "west-660": {"floor_id": "warehouse", "map_xy": [740, 660]},
                "west-740": {"floor_id": "warehouse", "map_xy": [740, 740]},
                "lower-700": {"floor_id": "warehouse", "map_xy": [700, 740]},
                "lower-650": {"floor_id": "warehouse", "map_xy": [650, 740]},
                "lower-600": {"floor_id": "warehouse", "map_xy": [600, 740]},
            },
            "edges": [
                {"id": "west-100-180", "from": "west-100", "to": "west-180", "floor_id": "warehouse", "bidirectional": True},
                {"id": "west-180-260", "from": "west-180", "to": "west-260", "floor_id": "warehouse", "bidirectional": True},
                {"id": "west-260-340", "from": "west-260", "to": "west-340", "floor_id": "warehouse", "bidirectional": True},
                {"id": "west-340-420", "from": "west-340", "to": "west-420", "floor_id": "warehouse", "bidirectional": True},
                {"id": "west-420-500", "from": "west-420", "to": "west-500", "floor_id": "warehouse", "bidirectional": True},
                {"id": "west-500-580", "from": "west-500", "to": "west-580", "floor_id": "warehouse", "bidirectional": True},
                {"id": "west-580-660", "from": "west-580", "to": "west-660", "floor_id": "warehouse", "bidirectional": True},
                {"id": "west-660-740", "from": "west-660", "to": "west-740", "floor_id": "warehouse", "bidirectional": True},
                {"id": "lower-700-west", "from": "lower-700", "to": "west-740", "floor_id": "warehouse", "bidirectional": True},
                {"id": "lower-650-700", "from": "lower-650", "to": "lower-700", "floor_id": "warehouse", "bidirectional": True},
                {"id": "lower-600-650", "from": "lower-600", "to": "lower-650", "floor_id": "warehouse", "bidirectional": True},
            ],
        },
        "max_gap_s": 2400,
    }
    (site_dir / "site.yaml").write_text(
        yaml.safe_dump(site, sort_keys=False), encoding="utf-8"
    )
    (params_dir / "real-cv-e2e-params-v1.yaml").write_text(
        "version: real-cv-e2e-params-v1\n"
        "K: 4\nB: 5\n"
        "anchor_probe_window_s: 60\n"
        "overlap_time_tolerance_s: 1\n"
        "overlap_position_tolerance_m: 0.75\n"
        "max_walk_snap_m: 3\n"
        "observed_gap_s: 2\n"
        "gap_bridge_max_s: 10\n",
        encoding="utf-8",
    )
    return site_root


def _reference_jpeg(video: Path, target: FullMinuteTarget) -> bytes:
    import cv2

    capture = cv2.VideoCapture(str(video))
    capture.set(cv2.CAP_PROP_POS_MSEC, target.reference_time_s * 1000)
    ok, frame = capture.read()
    capture.release()
    assert ok, f"cannot read reference frame from {video}"
    # A target-only crop prevents the gallery builder from choosing an
    # unrelated large bbox in the crowded synthetic warehouse.
    x1, y1, x2, y2 = target.reference_xyxy
    crop = frame[y1:y2, x1:x2]
    assert crop.shape[:2] == (y2 - y1, x2 - x1)
    encoded_ok, encoded = cv2.imencode(".jpg", crop)
    assert encoded_ok
    return encoded.tobytes()


def _verification_jpeg(
    videos: dict[str, Path],
    evidence_cameras: tuple[str, str],
    map_path: Path,
    tracklets,
    selected_tracklet_ids: set[str],
    observed_segments: list[list[dict]],
    inferred_paths: list[list[tuple[float, float]]],
    manual_checkpoints: list[dict],
    negative_checkpoints: list[dict],
) -> bytes:
    """Build a human-checkable video/map contact sheet for this exact run."""
    import cv2
    import numpy as np

    colours = {
        evidence_cameras[0]: (255, 180, 0),
        evidence_cameras[1]: (0, 220, 255),
    }
    panels = []
    for camera_id in evidence_cameras:
        capture = cv2.VideoCapture(str(videos[camera_id]))
        capture.set(cv2.CAP_PROP_POS_MSEC, 7500)
        ok, frame = capture.read()
        capture.release()
        assert ok
        camera_paths = [
            [
                (int(round(point.x_px)), int(round(point.y_px)))
                for point in (tracklet.trajectory or [])
            ]
            for tracklet in tracklets
            if tracklet.camera == camera_id
            and tracklet.id in selected_tracklet_ids
        ]
        for camera_points in camera_paths:
            if len(camera_points) >= 2:
                cv2.polylines(
                    frame,
                    [np.asarray(camera_points, dtype=np.int32)],
                    False,
                    colours[camera_id],
                    8,
                    cv2.LINE_AA,
                )
            for point in camera_points:
                cv2.circle(frame, point, 8, colours[camera_id], -1, cv2.LINE_AA)
        cv2.putText(
            frame,
            f"{camera_id}: real target foot-points",
            (35, 55),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.25,
            (20, 20, 20),
            5,
            cv2.LINE_AA,
        )
        cv2.putText(
            frame,
            f"{camera_id}: real target foot-points",
            (35, 55),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.25,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
        panels.append(cv2.resize(frame, (640, 360)))

    floor_map = cv2.imread(str(map_path))
    assert floor_map is not None
    for segment in observed_segments:
        map_points = [
            (int(round(point["x"])), int(round(point["y"])))
            for point in segment
        ]
        if len(map_points) >= 2:
            cv2.polylines(
                floor_map,
                [np.asarray(map_points, dtype=np.int32)],
                False,
                (255, 190, 0),
                8,
                cv2.LINE_AA,
            )
        for point in map_points:
            cv2.circle(floor_map, point, 7, (0, 0, 220), -1, cv2.LINE_AA)
    for path in inferred_paths:
        points = np.asarray(
            [(int(round(x)), int(round(y))) for x, y in path],
            dtype=np.int32,
        )
        if len(points) >= 2:
            for left, right in zip(points, points[1:]):
                delta = right - left
                length = float(np.linalg.norm(delta))
                if length <= 0:
                    continue
                direction = delta / length
                offset = 0.0
                while offset < length:
                    start = left + direction * offset
                    end = left + direction * min(offset + 16.0, length)
                    cv2.line(
                        floor_map,
                        tuple(np.rint(start).astype(int)),
                        tuple(np.rint(end).astype(int)),
                        (0, 140, 255),
                        7,
                        cv2.LINE_AA,
                    )
                    offset += 28.0
    for checkpoint in manual_checkpoints:
        x, y = (int(round(value)) for value in checkpoint["map_xy"])
        cv2.circle(floor_map, (x, y), 14, (40, 190, 40), 3, cv2.LINE_AA)
    for checkpoint in negative_checkpoints:
        x, y = (int(round(value)) for value in checkpoint["map_xy"])
        cv2.line(floor_map, (x - 12, y - 12), (x + 12, y + 12), (0, 0, 230), 4)
        cv2.line(floor_map, (x - 12, y + 12), (x + 12, y - 12), (0, 0, 230), 4)
    cv2.putText(
        floor_map,
        "observed path + orange gaps + manual checkpoints",
        (35, 55),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.25,
        (255, 255, 255),
        3,
        cv2.LINE_AA,
    )
    panels.append(cv2.resize(floor_map, (640, 360)))
    contact_sheet = np.concatenate(panels, axis=1)
    encoded_ok, encoded = cv2.imencode(".jpg", contact_sheet)
    assert encoded_ok
    return encoded.tobytes()


@pytest.mark.parametrize(
    "target",
    [
        BURGUNDY_TARGET,
        BLUE_SHIRT_TARGET,
        RED_DRESS_TARGET,
        pytest.param(
            YELLOW_TOP_TARGET,
            marks=pytest.mark.skip(
                reason=(
                    "quarantined: the current yellow-top oracle/result is "
                    "known to be invalid and is not acceptance evidence"
                )
            ),
        ),
    ],
    ids=lambda item: item.slug,
)
def test_real_cv_http_pipeline_renders_nvidia_route(
    target: FullMinuteTarget,
) -> None:
    _require_real_cv()

    from fastapi import Body, FastAPI, Header, HTTPException
    from javes_contracts import (
        ArtifactRef,
        CaseCommandBody,
        CaseWindow,
        CreateCaseBody,
        LastKnown,
        VideoAvailability,
        VideoSegmentRequest,
        VideoSegmentResult,
    )
    from javes_main.adapters.artifacts import FileArtifactStore
    from javes_main.adapters.http_routing import HttpRoutingAdapter
    from javes_main.adapters.remote_only import RemoteWorkerOnlyPort
    from javes_main.adapters.system import ContentIdGenerator, FixedClock
    from javes_main.api import create_app
    from javes_main.application import AuthService, CaseManager
    from javes_main.persistence import InMemoryRepository
    from javes_main.scheduler import RoutingTriggerDispatcher
    from javes_perception import CameraSource, PerceptionSettings, PerceptionWorker
    from javes_perception.runtime.archive_api import HttpVideoArchive
    from javes_perception.runtime.runner import PerceptionTaskRunner
    from javes_perception.runtime.state import PerceptionStateStore
    from javes_routing.service import create_routing_app
    from javes_site_config import FilesystemSiteConfigRegistry, create_site_config_app
    from javes_site_config.client import HttpSiteConfigClient
    from javes_workers import HttpWorkerClient

    repository_root = Path(__file__).resolve().parents[1]
    custom_output = os.environ.get("JAVES_REAL_CV_E2E_OUT")
    output_root = (
        Path(custom_output) / target.slug
        if custom_output
        else repository_root
        / "artifacts"
        / "real-cv-e2e"
        / target.output_directory
    ).resolve()
    run_id = datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%S-%fZ")
    run_root = output_root / "runs" / run_id
    run_root.mkdir(parents=True)
    site_root = _write_site_registry(
        run_root, repository_root, target.cameras
    )
    artifact_store = FileArtifactStore(run_root / "artifact-store")
    base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=2)

    videos = {
        "Cam001": repository_root
        / "data/nvidia_mtmc/mtmc_4cam/videos/Warehouse_Synthetic_Cam001.mp4",
        "Cam002": repository_root
        / "data/nvidia_mtmc/mtmc_4cam/videos/Warehouse_Synthetic_Cam002.mp4",
        "Cam003": repository_root
        / "data/nvidia_mtmc/mtmc_4cam/videos/Warehouse_Synthetic_Cam003.mp4",
        "Cam004": repository_root
        / "data/nvidia_mtmc/mtmc_4cam/videos/Warehouse_Synthetic_Cam004.mp4",
    }
    assert all(path.is_file() for path in videos.values())
    assert (repository_root / "models/yolo26n.pt").is_file()
    assert (repository_root / "models/osnet_ain_x1_0_msmt17.pt").is_file()
    archive_clip_cache: dict[str, Path] = {}

    archive_api = FastAPI(title="read-only NVIDIA CCTV fixture")

    @archive_api.post("/v1/video-segments", response_model=VideoSegmentResult)
    async def video_segment(
        raw_body: dict = Body(...),
        authorization: str | None = Header(default=None),
    ) -> VideoSegmentResult:
        if authorization != "Bearer archive-secret":
            raise HTTPException(status_code=401)
        body = VideoSegmentRequest.model_validate(raw_body)
        if body.camera_id not in videos or not isinstance(body.t_to, datetime):
            return VideoSegmentResult(
                request_id=body.request_id,
                camera_id=body.camera_id,
                availability=VideoAvailability.UNAVAILABLE,
                requested_from=body.t_from,
                requested_to=body.t_to,
                coverage_ratio=0.0,
            )
        start_s = max((body.t_from - base).total_seconds(), 0.0)
        duration_s = max((body.t_to - body.t_from).total_seconds(), 0.0)
        clip_key = hashlib.sha256(
            (
                f"{body.camera_id}\x1f{start_s:.6f}\x1f{duration_s:.6f}"
            ).encode()
        ).hexdigest()[:20]
        clip_path = archive_clip_cache.get(clip_key)
        if clip_path is None:
            clip_path = run_root / "archive-clips" / f"{clip_key}.mp4"
            _clip_video_window(
                videos[body.camera_id],
                clip_path,
                start_s=start_s,
                duration_s=duration_s,
            )
            archive_clip_cache[clip_key] = clip_path
        return VideoSegmentResult(
            request_id=body.request_id,
            camera_id=body.camera_id,
            availability=VideoAvailability.AVAILABLE,
            requested_from=body.t_from,
            requested_to=body.t_to,
            covered_from=body.t_from,
            covered_to=body.t_to,
            coverage_ratio=1.0,
            source_ref=ArtifactRef(uri=clip_path.resolve().as_uri()),
        )

    site_registry = FilesystemSiteConfigRegistry(site_root)
    site_api = create_site_config_app(site_registry, service_token="site-secret")

    with ExitStack():
        archive_http = _SyncASGIClient(archive_api, base_url="http://archive")
        site_http = _SyncASGIClient(site_api, base_url="http://site")
        site_client = HttpSiteConfigClient(
            "http://site",
            "site-secret",
            default_params_version="real-cv-e2e-params-v1",
            client=site_http,
        )
        routing_api = create_routing_app(site_client, service_token="routing-secret")
        routing_http = _SyncASGIClient(routing_api, base_url="http://routing")
        routing = HttpRoutingAdapter(
            "http://routing", "routing-secret", client=routing_http
        )

        clock = FixedClock(base + timedelta(seconds=60))
        repository = InMemoryRepository()
        ids = ContentIdGenerator()
        manager = CaseManager(
            repository=repository,
            artifacts=artifact_store,
            sites=site_client,
            routing=routing,
            perception=RemoteWorkerOnlyPort("perception"),
            clip=RemoteWorkerOnlyPort("clip"),
            retention=RemoteWorkerOnlyPort("retention"),
            live_feed=artifact_store,
            clock=clock,
            ids=ids,
            # Two complete one-minute clips are decoded serially on CPU.  Keep
            # the production default out of this opt-in performance test so
            # the second finite probe cannot expire while the first runs.
            job_deadline_s=900,
        )
        auth = AuthService(repository=repository, clock=clock, ids=ids)
        root_user = auth.bootstrap_root("root", "real-cv-e2e-password")
        routing_dispatcher = RoutingTriggerDispatcher(
            repository=repository,
            manager=manager,
            clock=clock,
            worker_id="real-cv-e2e-routing",
        )
        main_api = create_app(
            manager,
            auth=auth,
            worker_tokens={
                "perception": "perception-secret",
                "clip": "clip-secret",
                "retention": "retention-secret",
            },
            worker_lease_secret="real-cv-e2e-lease-secret",
        )
        main_http = _SyncASGIClient(main_api, base_url="http://main")

        login = main_http.post(
            "/v1/auth/login",
            json={"username": "root", "password": "real-cv-e2e-password"},
        )
        assert login.status_code == 200
        access_token = login.json()["access_token"]
        operator_headers = {
            "authorization": f"Bearer {access_token}",
        }

        case_id = target.case_id
        reference_camera = target.cameras[0]
        expected_cameras = set(target.cameras)
        expected_evidence_cameras = set(target.evidence_cameras)
        reference_bytes = _reference_jpeg(videos[reference_camera], target)
        (run_root / "reference.jpg").write_bytes(reference_bytes)
        uploaded = main_http.post(
            f"/v1/cases/{case_id}/reference-image",
            headers={
                **operator_headers,
                "idempotency-key": f"{case_id}-reference-upload",
            },
            files={"file": ("reference.jpg", reference_bytes, "image/jpeg")},
        )
        assert uploaded.status_code == 201, uploaded.text
        reference_ref = ArtifactRef.model_validate(
            uploaded.json()["reference_image_ref"]
        )
        created = main_http.post(
            "/v1/cases",
            headers={
                **operator_headers,
                "idempotency-key": f"{target.case_id}-create",
            },
            json=CreateCaseBody(
                case_id=case_id,
                operator_id=root_user.user_id,
                target_id=target.target_id,
                reference_image_ref=reference_ref,
                last_known=LastKnown(
                    camera_id=reference_camera,
                    time=base,
                ),
                window=CaseWindow(
                    start=base,
                    end=base + timedelta(seconds=60),
                ),
                site_id="nvidia-mtmc-4cam",
                site_version="real-cv-e2e-v1",
            ).model_dump(mode="json"),
        )
        assert created.status_code == 201, created.text

        archive = HttpVideoArchive(
            "http://archive",
            "archive-secret",
            camera_ids=set(videos),
            client=archive_http,
        )
        archive_contract = archive_http.post(
            "/v1/video-segments",
            headers={"authorization": "Bearer archive-secret"},
            json=VideoSegmentRequest(
                request_id="real-cv-contract-check",
                camera_id=target.cameras[1],
                t_from=base,
                t_to=base + timedelta(seconds=60),
            ).model_dump(mode="json"),
        )
        assert archive_contract.status_code == 200, archive_contract.text
        perception_settings = PerceptionSettings(
            cameras={
                camera: CameraSource(
                    path=str(path), start=base, archive_id=f"nvidia-{camera}"
                )
                for camera, path in videos.items()
            },
            detector_weights=str(repository_root / "models/yolo26n.pt"),
            reid_weights=str(
                repository_root / "models/osnet_ain_x1_0_msmt17.pt"
            ),
            device="auto",
            sample_fps=REAL_CV_SAMPLE_FPS,
            track_lost_buffer_s=8.0,
            imgsz=REAL_CV_IMGSZ,
            min_track_frames=3,
            min_crop_height=REAL_CV_MIN_CROP_HEIGHT,
            embed_every=REAL_CV_EMBED_EVERY,
            score_floor=0.1,
        )
        perception_state = PerceptionStateStore(run_root / "perception-state.sqlite")
        perception_worker = PerceptionWorker(
            perception_settings,
            artifact_store,
            archive=archive,
            state_store=perception_state,
        )
        worker_client = HttpWorkerClient(
            "http://main",
            "perception",
            "perception-secret",
            "real-cv-e2e-worker",
            client=main_http,
        )
        runner = PerceptionTaskRunner(
            perception_worker, worker_client, perception_state
        )
        try:
            assert runner.run_once(limit=1, lease_seconds=600) == 1
            gallery_dispatch = routing_dispatcher.drain()
            assert gallery_dispatch.completed == 1
            pending_jobs = repository.list_jobs(case_id, states={"PENDING"})
            assert len(pending_jobs) == 2
            assert {
                job.payload.probe.camera for job in pending_jobs
            } == expected_cameras
            assert {
                job.payload.probe.reason for job in pending_jobs
            } == {"ANCHOR_EVIDENCE"}
            assert all(job.payload.probe.t_from == base for job in pending_jobs)
            assert all(
                job.payload.probe.t_to == base + timedelta(seconds=60)
                for job in pending_jobs
            )

            assert runner.run_once(limit=4, lease_seconds=600) == 2
            probe_jobs = repository.list_jobs(case_id)
            assert all(job.state == "DONE" for job in probe_jobs), [
                (job.job_id, job.state, job.attempt, job.last_error)
                for job in probe_jobs
            ]
            probe_dispatch = routing_dispatcher.drain()
            assert probe_dispatch.completed == 2

        finally:
            perception_state.close()

        closed = main_http.post(
            f"/v1/cases/{case_id}/commands/close",
            headers={
                "authorization": f"Bearer {access_token}",
                "idempotency-key": f"{target.case_id}-close",
            },
            json=CaseCommandBody(
                command_id=f"{target.case_id}-close",
                action="CLOSE",
                operator_id=root_user.user_id,
                occurred_at=base + timedelta(seconds=60),
            ).model_dump(mode="json"),
        )
        assert closed.status_code == 200, closed.text
        close_dispatch = routing_dispatcher.drain()
        # At the exact case-window boundary Routing may already have produced
        # its terminal summary while consuming the second result.  CLOSE is
        # intentionally idempotent in that state and therefore has no trigger
        # left for the scheduler to claim.
        assert close_dispatch.failed == 0
        assert close_dispatch.completed in {0, 1}

        probe_records = repository.list_probe_results(case_id)
        assert probe_records
        real_results = [record.result for record in probe_records]
        assert all(result.status == "DONE" for result in real_results)
        assert all(result.coverage_ratio == 1.0 for result in real_results)
        assert all(
            result.processed_duration_s is not None
            and result.processed_duration_s >= 59.0
            for result in real_results
        )
        assert any(result.tracklets for result in real_results)
        candidate_scores = [
            candidate.score
            for result in real_results
            for candidate in result.candidates
        ]
        assert candidate_scores and max(candidate_scores) > 0.8
        real_tracklets = [
            tracklet for result in real_results for tracklet in result.tracklets
        ]
        assert real_tracklets
        assert all(tracklet.trajectory for tracklet in real_tracklets)
        assert all(len(tracklet.trajectory or []) <= 256 for tracklet in real_tracklets)

        dashboard = main_http.get(
            f"/v1/cases/{case_id}/view",
            headers={"authorization": f"Bearer {access_token}"},
        )
        assert dashboard.status_code == 200, dashboard.text
        view = dashboard.json()
        assert view["lifecycle"] == "CLOSED"
        assert view["mode"] == "PAST_SUMMARY"
        assert view["best_hypothesis"] is not None
        route_cameras = [
            visit["camera_id"] for visit in view["best_hypothesis"]["visits"]
        ]
        assert expected_evidence_cameras.issubset(route_cameras)
        projected_visits = [
            visit
            for visit in view["best_hypothesis"]["visits"]
            if visit["projected_trajectory"]
        ]
        assert {
            visit["camera_id"] for visit in projected_visits
        } == expected_evidence_cameras
        transitions = view["best_hypothesis"]["transitions"]
        observed_gaps = view["best_hypothesis"]["observed_gaps"]
        assert all(
            transition["geometry"] is None
            for transition in transitions
            if transition["kind"] == "OVERLAP"
        )
        observed_segments = view["best_hypothesis"]["observed_segments"]
        assert observed_segments
        assert {
            camera_id
            for segment in observed_segments
            for camera_id in segment["camera_ids"]
        } == expected_evidence_cameras
        observed_point_segments = [
            segment["points"] for segment in observed_segments
        ]
        observed_points = [
            point for segment in observed_point_segments for point in segment
        ]
        assert len(observed_points) >= 3
        observed_gaps_s = [
            (
                datetime.fromisoformat(right["points"][0]["timestamp"])
                - datetime.fromisoformat(left["points"][-1]["timestamp"])
            ).total_seconds()
            for left, right in zip(observed_segments, observed_segments[1:])
        ]
        assert all(gap > 2.0 for gap in observed_gaps_s)
        gap_transitions = [
            transition
            for transition in [*transitions, *observed_gaps]
            if transition["kind"] == "GAP"
            and transition["geometry"] is not None
        ]
        assert all(
            (
                datetime.fromisoformat(transition["t_to"])
                - datetime.fromisoformat(transition["t_from"])
            ).total_seconds()
            <= 10.0
            for transition in gap_transitions
        )
        assert all(
            transition["geometry"]["source"] == "WALK_GRAPH"
            and transition["geometry"]["walk_edge_ids"]
            and transition["geometry"]["approximate"]
            for transition in gap_transitions
        )
        selected_tracklet_ids = {
            tracklet_id
            for segment in observed_segments
            for tracklet_id in segment["tracklet_ids"]
        }
        selected_tracklets = [
            tracklet
            for tracklet in real_tracklets
            if tracklet.id in selected_tracklet_ids
        ]
        route_started_at = min(item.t_in for item in selected_tracklets)
        route_ended_at = max(item.t_out for item in selected_tracklets)
        assert (route_ended_at - route_started_at).total_seconds() >= target.min_route_span_s
        candidate_score_by_tracklet = {
            candidate.tracklet_id: candidate.score
            for result in real_results
            for candidate in result.candidates
        }
        assert min(
            candidate_score_by_tracklet[tracklet_id]
            for tracklet_id in selected_tracklet_ids
        ) >= 0.9

        camera_points = {
            camera_id: [
                point
                for visit in projected_visits
                if visit["camera_id"] == camera_id
                for point in visit["projected_trajectory"]
            ]
            for camera_id in target.evidence_cameras
        }
        aligned_distances_m = []
        m_per_px = 0.3048 / ((9.781818 + 9.785714) / 2.0)
        median_alignment_error_m: float | None = None
        if len(target.evidence_cameras) == 2:
            left_camera, right_camera = target.evidence_cameras
            for left in camera_points[left_camera]:
                left_time = datetime.fromisoformat(left["timestamp"])
                candidates = [
                    right
                    for right in camera_points[right_camera]
                    if abs(
                        (
                            datetime.fromisoformat(right["timestamp"])
                            - left_time
                        ).total_seconds()
                    )
                    <= 0.2
                ]
                if not candidates:
                    continue
                right = min(
                    candidates,
                    key=lambda item: abs(
                        (
                            datetime.fromisoformat(item["timestamp"])
                            - left_time
                        ).total_seconds()
                    ),
                )
                aligned_distances_m.append(
                    (
                        (left["x"] - right["x"]) ** 2
                        + (left["y"] - right["y"]) ** 2
                    )
                    ** 0.5
                    * m_per_px
                )
            assert aligned_distances_m
            aligned_distances_m.sort()
            median_alignment_error_m = aligned_distances_m[
                len(aligned_distances_m) // 2
            ]
            assert median_alignment_error_m <= 0.25

        xs = [point["x"] for point in observed_points]
        ys = [point["y"] for point in observed_points]
        observed_distance_m = sum(
            sum(
                (
                    (right["x"] - left["x"]) ** 2
                    + (right["y"] - left["y"]) ** 2
                )
                ** 0.5
                * m_per_px
                for left, right in zip(segment, segment[1:])
            )
            for segment in observed_point_segments
        )

        annotations = _manual_annotations(repository_root, target.slug)
        homographies = _nvidia_pixel_to_map_homographies(repository_root)
        manual_checkpoints = _project_manual_points(
            annotations["checkpoints"], homographies
        )
        negative_checkpoints = _project_manual_points(
            annotations["negative_checkpoints"], homographies
        )
        checkpoint_errors_m = _checkpoint_errors_m(
            manual_checkpoints,
            observed_points,
            base,
            m_per_px,
            inferred_transitions=gap_transitions,
        )
        assert median(checkpoint_errors_m) <= 0.75
        assert max(checkpoint_errors_m) <= 1.5
        negative_distances_m = _checkpoint_errors_m(
            negative_checkpoints,
            observed_points,
            base,
            m_per_px,
            inferred_transitions=gap_transitions,
        )
        assert min(negative_distances_m) >= 0.75

        source_tracklet = next(
            tracklet
            for tracklet in real_tracklets
            if tracklet.id in selected_tracklet_ids
        )
        projected_visit = next(
            visit
            for visit in projected_visits
            if visit["camera_id"] == source_tracklet.camera
            and source_tracklet.id in visit["tracklet_ids"]
        )
        source_point = (source_tracklet.trajectory or [])[0]
        import numpy as np

        homography = np.asarray(
            _nvidia_pixel_to_map_homographies(repository_root)[
                source_tracklet.camera
            ]
        )
        expected = homography @ np.asarray(
            [source_point.x_px, source_point.y_px, 1.0]
        )
        expected /= expected[2]
        projected_point = projected_visit["projected_trajectory"][0]
        projection_error_px = float(
            np.hypot(
                projected_point["x"] - expected[0],
                projected_point["y"] - expected[1],
            )
        )
        allowed_projection_error_px = 0.01
        assert projection_error_px <= allowed_projection_error_px
        assert view["rendered_route_maps"]
        presentation = view["route_presentation"]
        assert presentation["position_state"] == "LAST_SEEN"
        assert set(target.expected_last_source_cameras).issubset(
            presentation["source_camera_ids"]
        )
        assert (
            presentation["visible_at_interval_end"]
            is target.visible_at_interval_end
        )
        assert presentation["search_area_as_of"] is None
        expected_layers = {"CAMERAS", "LAST_POSITION", "OBSERVED"}
        if gap_transitions:
            expected_layers.add("INFERRED")
        assert set(presentation["shown_layers"]) == expected_layers
        assert view["estimate"]["zones_operator_display"] == "TECHNICAL_ONLY"
        route_map = view["rendered_route_maps"][0]
        route_response = main_http.get(
            route_map["url"],
            headers={"authorization": f"Bearer {access_token}"},
        )
        assert route_response.status_code == 200
        route_svg = route_response.content
        assert b'data-role="floor-plan"' in route_svg
        assert b'data-role="best-route"' in route_svg
        assert b'data-role="observed-path"' in route_svg
        assert b'data-role="reachable-area"' not in route_svg
        assert b'data-role="last-confirmed-position"' in route_svg
        assert b'data-role="route-status"' in route_svg
        assert b'data-role="estimate"' not in route_svg
        assert b'data-role="legend"' in route_svg
        svg_root = ET.fromstring(route_svg)
        rendered_observed_paths = [
            element
            for element in svg_root.iter()
            if element.attrib.get("data-role") == "observed-path"
        ]
        rendered_inferred_paths = [
            element
            for element in svg_root.iter()
            if element.attrib.get("data-role") == "inferred-path"
        ]
        rendered_alternative_paths = [
            element
            for element in svg_root.iter()
            if element.attrib.get("data-role") == "alternative-path"
        ]
        assert len(rendered_observed_paths) == len(observed_segments)
        assert len(rendered_inferred_paths) == len(gap_transitions)
        assert not rendered_alternative_paths
        assert all(
            element.attrib.get("data-kind") == "GAP"
            and element.attrib.get("data-approximate") == "true"
            and element.attrib.get("stroke") == "#d97706"
            and element.attrib.get("stroke-dasharray")
            for element in rendered_inferred_paths
        )
        assert all(
            element.attrib.get("stroke") == "#0891b2"
            and not element.attrib.get("stroke-dasharray")
            for element in rendered_observed_paths
        )
        visible_text = " ".join(
            text.strip()
            for text in svg_root.itertext()
            if text and text.strip()
        )
        camera_labels = {
            " ".join(text.strip() for text in element.itertext() if text.strip())
            for element in svg_root.iter()
            if element.attrib.get("data-role") == "camera"
        }
        evidence_camera_labels = {
            f"{camera} · video evidence"
            for camera in target.evidence_cameras
        }
        candidate_review_camera_labels = {
            f"{camera} · candidate review"
            for camera in expected_cameras - expected_evidence_cameras
        }
        assert evidence_camera_labels.issubset(camera_labels)
        assert camera_labels <= (
            evidence_camera_labels | candidate_review_camera_labels
        )
        candidate_review_cameras = sorted(
            label.removesuffix(" · candidate review")
            for label in camera_labels
            if label.endswith(" · candidate review")
        )
        assert "%" not in visible_text
        assert "Seen on video" in visible_text
        assert "Last confirmed position" in visible_text
        assert (
            "Estimated between sightings" in visible_text
        ) is bool(gap_transitions)
        assert "Possible area after last sighting" not in visible_text

        result_payloads = [
            result.model_dump(mode="json", by_alias=True) for result in real_results
        ]
        report = {
            "case_id": case_id,
            "lifecycle": view["lifecycle"],
            "mode": view["mode"],
            "stop_reason": view["stop_reason"],
            "route": route_cameras,
            "max_real_reid_score": max(candidate_scores),
            "tracklet_count": sum(len(item.tracklets) for item in real_results),
            "candidate_count": len(candidate_scores),
            "route_revision": view["route_revision"],
            "route_api_url": route_map["url"],
            "map_source": "data/nvidia_mtmc/mtmc_4cam/map.png",
            "trajectory_points": sum(
                len(tracklet.trajectory or []) for tracklet in real_tracklets
            ),
            "projection_error_px": projection_error_px,
            "allowed_projection_error_px": allowed_projection_error_px,
            "median_cross_camera_alignment_error_m": median_alignment_error_m,
            "allowed_cross_camera_alignment_error_m": (
                0.25 if median_alignment_error_m is not None else None
            ),
            "observed_distance_m": observed_distance_m,
            "observed_segment_count": len(observed_segments),
            "gap_bridge_count": len(gap_transitions),
            "manual_checkpoint_median_error_m": median(checkpoint_errors_m),
            "manual_checkpoint_max_error_m": max(checkpoint_errors_m),
            "negative_checkpoint_min_distance_m": min(negative_distances_m),
            "route_evidence_span_s": (
                route_ended_at - route_started_at
            ).total_seconds(),
            "observed_bounds_px": {
                "x": [min(xs), max(xs)],
                "y": [min(ys), max(ys)],
            },
            "positioning_source": "NVIDIA homography + real YOLO foot-points",
            "target": target.label,
            "searched_cameras": list(target.cameras),
            "evidence_cameras": list(target.evidence_cameras),
            "candidate_review_cameras": candidate_review_cameras,
            "checked_without_accepted_evidence_cameras": sorted(
                (expected_cameras - expected_evidence_cameras)
                - set(candidate_review_cameras)
            ),
            "source_videos": {
                camera: str(path.resolve())
                for camera, path in videos.items()
                if camera in expected_cameras
            },
            "source_video_window_s": [0.0, 60.0],
            "run_directory": str(run_root.resolve()),
        }
        verification_jpeg = _verification_jpeg(
            videos,
            target.cameras,
            repository_root / "data/nvidia_mtmc/mtmc_4cam/map.png",
            real_tracklets,
            selected_tracklet_ids,
            observed_point_segments,
            [transition["geometry"]["points"] for transition in gap_transitions],
            manual_checkpoints,
            negative_checkpoints,
        )
        timeline = [
            {
                "time_s": round((tracklet.t_in - base).total_seconds(), 3),
                "camera": tracklet.camera,
                "event": "target tracklet",
                "details": (
                    f"{(tracklet.t_out - tracklet.t_in).total_seconds():.2f}s · "
                    f"{len(tracklet.trajectory or [])} trajectory points"
                ),
            }
            for tracklet in real_tracklets
            if tracklet.id in selected_tracklet_ids
        ]
        last_confirmed = presentation["last_confirmed_position"]
        timeline.extend(
            {
                "time_s": checkpoint["time_s"],
                "camera": checkpoint["camera"],
                "event": "manual validation checkpoint",
                "details": f"independent bbox foot-point · error {error:.2f} m",
            }
            for checkpoint, error in zip(manual_checkpoints, checkpoint_errors_m)
        )
        timeline.extend(
            [
                {
                    "time_s": round(
                        (
                            datetime.fromisoformat(last_confirmed["timestamp"])
                            - base
                        ).total_seconds(),
                        3,
                    ),
                    "camera": ", ".join(presentation["source_camera_ids"]),
                    "event": "last confirmed position",
                    "details": "projected from real foot-points",
                },
                {
                    "time_s": 60.0,
                    "camera": "—",
                    "event": "search closed",
                    "details": view["stop_reason"],
                },
            ]
        )
        publish_inspectable_report(
            output_root,
            run_root,
            route_svg=route_svg,
            case_view=view,
            probe_results=result_payloads,
            report=report,
            videos=[
                ReportVideo(
                    camera,
                    videos[camera],
                    (
                        "evidence"
                        if camera in expected_evidence_cameras
                        else "candidate-review (not accepted evidence)"
                        if camera in candidate_review_cameras
                        else "checked-no-accepted-evidence"
                    ),
                    note="full 60 s",
                )
                for camera in target.cameras
            ],
            timeline=timeline,
            extra_files={"verification.jpg": verification_jpeg},
        )
        print(f"real-CV E2E output: {output_root}")
