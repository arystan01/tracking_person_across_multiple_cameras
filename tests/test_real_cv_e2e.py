"""Inspectable API-first E2E using real CV and the NVIDIA MTMC floor map.

This is intentionally opt-in: it loads the tracked YOLO/OSNet weights and
processes real video. Generated output is kept under ``artifacts/`` so a human
can open the exact SVG returned through Controls after the test finishes.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import importlib.util
import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


pytestmark = pytest.mark.real_cv


class _SyncASGIClient:
    """Small sync facade over httpx's ASGI transport.

    The repository's production HTTP adapters are synchronous. Each request
    gets its own worker thread so nested Main -> Routing -> Site Config calls
    can still execute their ASGI applications through real HTTP contracts.
    """

    def __init__(self, app, *, base_url: str) -> None:
        self.app = app
        self.base_url = base_url

    def request(self, method: str, url: str, **kwargs):
        import httpx

        async def send():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url=self.base_url,
            ) as client:
                return await client.request(method, url, **kwargs)

        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(lambda: asyncio.run(send())).result(timeout=60)

    def get(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self.request("POST", url, **kwargs)


def _require_real_cv() -> None:
    if os.environ.get("JAVES_RUN_REAL_CV") != "1":
        pytest.skip("set JAVES_RUN_REAL_CV=1 in the real-CV environment")
    # Do not import the ML stacks before the TestClient portals are running.
    # In CPU-only environments PyTorch/TensorFlow initialise native thread
    # pools during import, which can deadlock a subsequently-created AnyIO
    # portal. Perception imports the real models lazily once all APIs are up.
    missing = [
        module
        for module in ("cv2", "ultralytics", "torchreid")
        if importlib.util.find_spec(module) is None
    ]
    if missing:
        pytest.skip(f"missing real-CV dependencies: {', '.join(missing)}")


def _nvidia_pixel_to_map_homographies(
    repository_root: Path,
) -> dict[str, list[list[float]]]:
    import numpy as np
    import yaml

    fixture = repository_root / "data/nvidia_mtmc/mtmc_4cam"
    transforms = yaml.safe_load((fixture / "transforms.yml").read_text())
    world_to_map = np.asarray(transforms["T_ov2px"], dtype=float).reshape(3, 3)
    result: dict[str, list[list[float]]] = {}
    for index in range(1, 5):
        camera = f"Cam{index:03d}"
        raw = yaml.safe_load(
            (
                fixture
                / "camInfo"
                / f"Warehouse_Synthetic_{camera}.yml"
            ).read_text()
        )
        projection = np.asarray(
            raw["projectionMatrix_3x4_w2p"], dtype=float
        ).reshape(3, 4)
        ground_plane_to_pixel = projection[:, [0, 1, 3]]
        pixel_to_map = world_to_map @ np.linalg.inv(ground_plane_to_pixel)
        pixel_to_map /= pixel_to_map[2, 2]
        result[camera] = pixel_to_map.tolist()
    return result


def _write_site_registry(run_root: Path, repository_root: Path) -> Path:
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
    cameras["Cam001"]["overlap_camera_ids"] = ["Cam003"]
    cameras["Cam003"]["overlap_camera_ids"] = ["Cam001"]
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
        # Camera edges are for probe planning only. Cam001 and Cam003 overlap,
        # so there is deliberately no pedestrian transition between them.
        "edges": [],
        "walk_graph": {
            "nodes": {
                "route-0": {"floor_id": "warehouse", "map_xy": [645, 505]},
                "route-1": {"floor_id": "warehouse", "map_xy": [665, 507]},
                "route-2": {"floor_id": "warehouse", "map_xy": [685, 500]},
                "route-3": {"floor_id": "warehouse", "map_xy": [710, 490]},
                "route-4": {"floor_id": "warehouse", "map_xy": [740, 480]},
            },
            "edges": [
                {"id": "route-0-1", "from": "route-0", "to": "route-1", "floor_id": "warehouse"},
                {"id": "route-1-2", "from": "route-1", "to": "route-2", "floor_id": "warehouse"},
                {"id": "route-2-3", "from": "route-2", "to": "route-3", "floor_id": "warehouse"},
                {"id": "route-3-4", "from": "route-3", "to": "route-4", "floor_id": "warehouse"},
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
        "anchor_probe_window_s: 5\n"
        "overlap_time_tolerance_s: 1\n"
        "overlap_position_tolerance_m: 0.75\n"
        "max_walk_snap_m: 3\n",
        encoding="utf-8",
    )
    return site_root


def _reference_jpeg(video: Path) -> bytes:
    import cv2

    capture = cv2.VideoCapture(str(video))
    capture.set(cv2.CAP_PROP_POS_MSEC, 5000)
    ok, frame = capture.read()
    capture.release()
    assert ok, f"cannot read reference frame from {video}"
    # The target is the woman in a burgundy top and light-blue jeans. A target
    # crop prevents the gallery builder from choosing an unrelated large bbox.
    target = frame[720:1000, 260:410]
    assert target.shape[:2] == (280, 150)
    encoded_ok, encoded = cv2.imencode(".jpg", target)
    assert encoded_ok
    return encoded.tobytes()


def _clip_video_window(
    source: Path,
    destination: Path,
    *,
    start_s: float,
    duration_s: float,
) -> None:
    """Make the fixture behave like an archive returning the exact window."""
    import cv2

    capture = cv2.VideoCapture(str(source))
    assert capture.isOpened(), f"cannot open {source}"
    fps = capture.get(cv2.CAP_PROP_FPS)
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    assert fps > 0 and width > 0 and height > 0
    first_frame = max(int(round(start_s * fps)), 0)
    frame_count = max(int(round(duration_s * fps)), 1)
    capture.set(cv2.CAP_PROP_POS_FRAMES, first_frame)
    destination.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(
        str(destination),
        cv2.VideoWriter_fourcc(*"mp4v"),
        fps,
        (width, height),
    )
    assert writer.isOpened(), f"cannot create {destination}"
    try:
        for _ in range(frame_count):
            ok, frame = capture.read()
            if not ok:
                break
            writer.write(frame)
    finally:
        writer.release()
        capture.release()
    assert destination.is_file() and destination.stat().st_size > 0


def _verification_jpeg(
    videos: dict[str, Path],
    map_path: Path,
    tracklets,
    selected_tracklet_ids: set[str],
    observed_points: list[dict],
) -> bytes:
    """Build a human-checkable video/map contact sheet for this exact run."""
    import cv2
    import numpy as np

    colours = {"Cam001": (255, 180, 0), "Cam003": (0, 220, 255)}
    panels = []
    for camera_id in ("Cam001", "Cam003"):
        capture = cv2.VideoCapture(str(videos[camera_id]))
        capture.set(cv2.CAP_PROP_POS_MSEC, 7500)
        ok, frame = capture.read()
        capture.release()
        assert ok
        camera_points = [
            (int(round(point.x_px)), int(round(point.y_px)))
            for tracklet in tracklets
            if tracklet.camera == camera_id
            and tracklet.id in selected_tracklet_ids
            for point in (tracklet.trajectory or [])
        ]
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
    map_points = [
        (int(round(point["x"])), int(round(point["y"])))
        for point in observed_points
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
    cv2.putText(
        floor_map,
        "fused observed ground path",
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


def _publish_inspectable_output(
    output_root: Path,
    run_root: Path,
    *,
    route_svg: bytes,
    case_view: dict,
    probe_results: list[dict],
    report: dict,
    verification_jpeg: bytes,
) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    files = {
        "route.svg": route_svg,
        "case-view.json": json.dumps(case_view, indent=2).encode(),
        "probe-results.json": json.dumps(probe_results, indent=2).encode(),
        "report.json": json.dumps(report, indent=2).encode(),
        "verification.jpg": verification_jpeg,
    }
    for name, data in files.items():
        (run_root / name).write_bytes(data)
        # Stable aliases make the newest result easy to find after every run.
        (output_root / name).write_bytes(data)
    summary = html.escape(json.dumps(report, indent=2))
    page = (
        "<!doctype html><meta charset='utf-8'><title>JAVES real-CV E2E</title>"
        "<style>body{font:14px system-ui;margin:24px;background:#f8fafc}"
        "img{max-width:100%;border:1px solid #cbd5e1;background:white}"
        "pre{background:#fff;padding:16px;overflow:auto}</style>"
        "<h1>JAVES real-CV E2E route</h1><img src='route.svg' alt='route'>"
        "<h2>Video and map verification</h2>"
        "<img src='verification.jpg' alt='Cam001, Cam003 and fused map path'>"
        f"<h2>Run report</h2><pre>{summary}</pre>"
    )
    (run_root / "index.html").write_text(page, encoding="utf-8")
    (output_root / "index.html").write_text(page, encoding="utf-8")
    (output_root / "LATEST.txt").write_text(
        str(run_root.resolve()) + "\n", encoding="utf-8"
    )


def test_real_cv_http_pipeline_renders_nvidia_route() -> None:
    _require_real_cv()

    from fastapi import Body, FastAPI, Header, HTTPException
    from javes_contracts import (
        ArtifactRef,
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
    output_root = Path(
        os.environ.get(
            "JAVES_REAL_CV_E2E_OUT",
            repository_root / "artifacts" / "real-cv-e2e",
        )
    ).resolve()
    run_id = datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%S-%fZ")
    run_root = output_root / "runs" / run_id
    run_root.mkdir(parents=True)
    site_root = _write_site_registry(run_root, repository_root)
    artifact_store = FileArtifactStore(run_root / "artifact-store")
    base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=30)

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

    with ExitStack() as stack:
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

        clock = FixedClock(base + timedelta(seconds=16))
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
            "idempotency-key": "real-cv-e2e-create",
        }

        case_id = "real-cv-e2e"
        reference_key = f"cases/{case_id}/reference.jpg"
        reference_bytes = _reference_jpeg(videos["Cam001"])
        artifact_store.put(reference_key, reference_bytes, "image/jpeg")
        (run_root / "reference.jpg").write_bytes(reference_bytes)
        created = main_http.post(
            "/v1/cases",
            headers=operator_headers,
            json=CreateCaseBody(
                case_id=case_id,
                operator_id=root_user.user_id,
                target_id="nvidia-reference-person",
                reference_image_ref=ArtifactRef(object_key=reference_key),
                last_known=LastKnown(
                    camera_id="Cam001",
                    time=base + timedelta(seconds=5),
                    footpoint_px=(337.7, 985.9),
                ),
                window=CaseWindow(
                    start=base + timedelta(seconds=5),
                    end=base + timedelta(seconds=16),
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
                camera_id="Cam003",
                t_from=base + timedelta(seconds=5),
                t_to=base + timedelta(seconds=10),
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
            sample_fps=1.5,
            min_track_frames=3,
            embed_every=2,
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
            } == {"Cam001", "Cam003"}
            assert {
                job.payload.probe.reason for job in pending_jobs
            } == {"ANCHOR_EVIDENCE"}

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

        probe_records = repository.list_probe_results(case_id)
        assert probe_records
        real_results = [record.result for record in probe_records]
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
        assert view["best_hypothesis"] is not None
        route_cameras = [
            visit["camera_id"] for visit in view["best_hypothesis"]["visits"]
        ]
        assert {"Cam001", "Cam003"}.issubset(route_cameras)
        projected_visits = [
            visit
            for visit in view["best_hypothesis"]["visits"]
            if visit["projected_trajectory"]
        ]
        assert {visit["camera_id"] for visit in projected_visits} == {
            "Cam001",
            "Cam003",
        }
        overlap_transition = next(
            transition
            for transition in view["best_hypothesis"]["transitions"]
            if transition["kind"] == "OVERLAP"
        )
        assert overlap_transition["geometry"] is None

        observed_segments = view["best_hypothesis"]["observed_segments"]
        assert len(observed_segments) == 1
        observed_segment = observed_segments[0]
        assert observed_segment["camera_ids"] == ["Cam001", "Cam003"]
        observed_points = observed_segment["points"]
        assert len(observed_points) >= 3
        selected_tracklet_ids = set(observed_segment["tracklet_ids"])

        camera_points = {
            visit["camera_id"]: visit["projected_trajectory"]
            for visit in projected_visits
        }
        aligned_distances_m = []
        m_per_px = 0.3048 / ((9.781818 + 9.785714) / 2.0)
        for left in camera_points["Cam001"]:
            left_time = datetime.fromisoformat(left["timestamp"])
            candidates = [
                right
                for right in camera_points["Cam003"]
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
        assert min(xs) >= 645 and max(xs) <= 695
        assert min(ys) >= 485 and max(ys) <= 525
        assert xs[-1] - xs[0] >= 8
        observed_distance_m = sum(
            (
                (right["x"] - left["x"]) ** 2
                + (right["y"] - left["y"]) ** 2
            )
            ** 0.5
            * m_per_px
            for left, right in zip(observed_points, observed_points[1:])
        )
        assert 0.2 <= observed_distance_m <= 3.0

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
        assert b'data-role="inferred-path"' not in route_svg
        assert b'data-role="legend"' in route_svg
        assert b"Cam001" in route_svg and b"Cam003" in route_svg

        result_payloads = [
            result.model_dump(mode="json", by_alias=True) for result in real_results
        ]
        report = {
            "case_id": case_id,
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
            "allowed_cross_camera_alignment_error_m": 0.25,
            "observed_distance_m": observed_distance_m,
            "observed_bounds_px": {
                "x": [min(xs), max(xs)],
                "y": [min(ys), max(ys)],
            },
            "positioning_source": "NVIDIA homography + real YOLO foot-points",
            "target": "woman in burgundy top and light-blue jeans",
            "source_videos": {
                camera: str(path.resolve())
                for camera, path in videos.items()
                if camera in {"Cam001", "Cam003"}
            },
            "source_video_window_s": [5.0, 10.0],
            "run_directory": str(run_root.resolve()),
        }
        verification_jpeg = _verification_jpeg(
            videos,
            repository_root / "data/nvidia_mtmc/mtmc_4cam/map.png",
            real_tracklets,
            selected_tracklet_ids,
            observed_points,
        )
        _publish_inspectable_output(
            output_root,
            run_root,
            route_svg=route_svg,
            case_view=view,
            probe_results=result_payloads,
            report=report,
            verification_jpeg=verification_jpeg,
        )
        print(f"real-CV E2E output: {output_root}")
