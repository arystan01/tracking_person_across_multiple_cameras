"""Inspectable API-first E2E using real CV and the NVIDIA MTMC floor map.

This is intentionally opt-in: it loads the tracked YOLO/OSNet weights and
processes real video. Generated output is kept under ``artifacts/`` so a human
can open the exact SVG returned through Controls after the test finishes.
"""

from __future__ import annotations

import asyncio
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


def _write_site_registry(run_root: Path, repository_root: Path) -> Path:
    site_root = run_root / "site-config"
    site_dir = site_root / "sites" / "nvidia-mtmc-4cam" / "real-cv-e2e-v1"
    params_dir = site_root / "routing_params"
    site_dir.mkdir(parents=True)
    params_dir.mkdir(parents=True)
    shutil.copyfile(
        repository_root / "data/nvidia_mtmc/mtmc_4cam/map.png",
        site_dir / "map.png",
    )
    # Camera positions come from NVIDIA's projection matrices and T_ov2px.
    (site_dir / "site.yaml").write_text(
        """
site: nvidia-mtmc-4cam
version: real-cv-e2e-v1
timezone: UTC
map:
  floors:
    - {id: warehouse, image: map.png, size_px: [1920, 1080]}
cameras:
  Cam001: {zone: warehouse-west, floor: warehouse, map_xy: [583.1, 454.2]}
  Cam002: {zone: warehouse-west, floor: warehouse, map_xy: [668.2, 506.0]}
  Cam003: {zone: warehouse-south, floor: warehouse, map_xy: [520.9, 735.2]}
  Cam004: {zone: warehouse-south, floor: warehouse, map_xy: [729.1, 786.3]}
edges:
  - id: Cam001-Cam003
    from: Cam001
    to: Cam003
    t_min: 1
    t_typ: 8
    t_max: 30
    map_path: [[570, 540], [545, 640]]
max_gap_s: 2400
""".strip(),
        encoding="utf-8",
    )
    (params_dir / "real-cv-e2e-params-v1.yaml").write_text(
        "version: real-cv-e2e-params-v1\nK: 4\nB: 5\n",
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
    encoded_ok, encoded = cv2.imencode(".jpg", frame)
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
) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    files = {
        "route.svg": route_svg,
        "case-view.json": json.dumps(case_view, indent=2).encode(),
        "probe-results.json": json.dumps(probe_results, indent=2).encode(),
        "report.json": json.dumps(report, indent=2).encode(),
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
    from javes_perception.archive_api import HttpVideoArchive
    from javes_perception.runtime import PerceptionTaskRunner
    from javes_perception.state import PerceptionStateStore
    from javes_routing_api import create_routing_app
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
        return VideoSegmentResult(
            request_id=body.request_id,
            camera_id=body.camera_id,
            availability=VideoAvailability.AVAILABLE,
            requested_from=body.t_from,
            requested_to=body.t_to,
            covered_from=body.t_from,
            covered_to=body.t_to,
            coverage_ratio=1.0,
            source_ref=ArtifactRef(uri=videos[body.camera_id].resolve().as_uri()),
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

        base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=30)
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
                last_known=LastKnown(camera_id="Cam001", time=base),
                window=CaseWindow(start=base, end=base + timedelta(seconds=16)),
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
                t_from=base,
                t_to=base + timedelta(seconds=16),
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
            assert len(pending_jobs) == 1
            assert pending_jobs[0].payload.probe.camera == "Cam003"

            assert runner.run_once(limit=4, lease_seconds=600) == 1
            probe_jobs = repository.list_jobs(case_id)
            assert all(job.state == "DONE" for job in probe_jobs), [
                (job.job_id, job.state, job.attempt, job.last_error)
                for job in probe_jobs
            ]
            probe_dispatch = routing_dispatcher.drain()
            assert probe_dispatch.completed == 1
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
        assert route_cameras[:2] == ["Cam001", "Cam003"]
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
            "run_directory": str(run_root.resolve()),
        }
        _publish_inspectable_output(
            output_root,
            run_root,
            route_svg=route_svg,
            case_view=view,
            probe_results=result_payloads,
            report=report,
        )
        print(f"real-CV E2E output: {output_root}")
