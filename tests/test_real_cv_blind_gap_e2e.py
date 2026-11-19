"""Inspectable real-CV search across a genuine NVIDIA camera blind gap."""

from __future__ import annotations

import os
import shutil
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.support.real_cv_e2e import (
    ReportVideo,
    SyncASGIClient,
    nvidia_pixel_to_map_homographies,
    project_pixel,
    publish_inspectable_report,
    require_real_cv,
)


pytestmark = pytest.mark.real_cv


CAMERAS = ["Cam006", "Cam007", "Cam008", "Cam009", "Cam011"]
ROUTING_CAMERAS = ["Cam006", "Cam008", "Cam009", "Cam011"]
FLOOR_ID = "warehouse"
MAP_SIZE = (1130, 1080)
M_PER_PX = 0.3048 / 34.282
EFFECTIVE_MAX_GAP_S = 15.0


def _reference_jpeg(video: Path) -> bytes:
    import cv2

    capture = cv2.VideoCapture(str(video))
    capture.set(cv2.CAP_PROP_POS_MSEC, 2000)
    ok, frame = capture.read()
    capture.release()
    assert ok, f"cannot read reference frame from {video}"
    # The fixed target is the pink-overall worker at x=494..542, y=82..233.
    target = frame[82:233, 494:542]
    assert target.shape[:2] == (151, 48)
    encoded_ok, encoded = cv2.imencode(".jpg", target)
    assert encoded_ok
    return encoded.tobytes()


def _write_site_registry(run_root: Path, repository_root: Path) -> Path:
    import yaml

    fixture = repository_root / "data/nvidia_mtmc/mtmc_12cam"
    site_root = run_root / "site-config"
    site_dir = site_root / "sites" / "nvidia-mtmc-12cam" / "blind-gap-v1"
    params_dir = site_root / "routing_params"
    site_dir.mkdir(parents=True)
    params_dir.mkdir(parents=True)
    shutil.copyfile(fixture / "map.png", site_dir / "map.png")
    homographies = nvidia_pixel_to_map_homographies(fixture, ROUTING_CAMERAS)

    camera_positions = {
        "Cam009": [80, 940],
        "Cam011": [160, 1010],
        "Cam006": [370, 280],
        "Cam008": [370, 70],
    }
    coverage = {
        "Cam009": [[40, 800], [190, 800], [190, 1040], [40, 1040]],
        "Cam011": [[40, 900], [220, 900], [220, 1070], [40, 1070]],
        "Cam006": [[250, 190], [430, 190], [430, 330], [250, 330]],
        "Cam008": [[280, 20], [450, 20], [450, 150], [280, 150]],
    }
    cameras = {
        camera: {
            "zone": f"fixture-{camera.lower()}",
            "floor": FLOOR_ID,
            "map_xy": camera_positions[camera],
            "coverage_polygon": coverage[camera],
            "calibration": {
                "frame_size_px": [1920, 1080],
                "homography": homographies[camera],
                "floor_id": FLOOR_ID,
                "error_m": 0.5,
            },
        }
        for camera in ROUTING_CAMERAS
    }
    cameras["Cam009"]["overlap_camera_ids"] = ["Cam011"]
    cameras["Cam011"]["overlap_camera_ids"] = ["Cam009"]

    site = {
        "site": "nvidia-mtmc-12cam",
        "version": "blind-gap-v1",
        "timezone": "UTC",
        "map": {
            "floors": [
                {
                    "id": FLOOR_ID,
                    "image": "map.png",
                    "size_px": list(MAP_SIZE),
                    "m_per_px": M_PER_PX,
                }
            ]
        },
        "cameras": cameras,
        "blind_nodes": [
            {
                "id": "blind-09-06",
                "floor": FLOOR_ID,
                "map_xy": [100, 160],
                "borders": ["Cam009", "Cam006"],
                "dwell": {"median_s": 3, "sigma": 0.8},
            },
            {
                "id": "blind-after-06",
                "floor": FLOOR_ID,
                "map_xy": [337, 160],
                "borders": ["Cam006", "Cam008"],
                "dwell": {"median_s": 1, "sigma": 0.8},
            },
        ],
        "edges": [
            {
                "id": "cam09-blind",
                "from": "Cam009",
                "to": "blind-09-06",
                "t_min": 1.5,
                "t_typ": 4,
                "t_max": 7.5,
                "d_m": 3.4,
                "floor": FLOOR_ID,
                "map_path": [[100, 600], [100, 160]],
            },
            {
                "id": "blind-cam06",
                "from": "blind-09-06",
                "to": "Cam006",
                "t_min": 1.5,
                "t_typ": 4,
                "t_max": 7.5,
                "d_m": 3.0,
                "floor": FLOOR_ID,
                "map_path": [[337, 160]],
            },
            {
                "id": "cam06-blind",
                "from": "Cam006",
                "to": "blind-after-06",
                "t_min": 0.2,
                "t_typ": 0.8,
                "t_max": 1.5,
                "d_m": 0.8,
                "floor": FLOOR_ID,
            },
            {
                "id": "blind-cam08",
                "from": "blind-after-06",
                "to": "Cam008",
                "t_min": 0.2,
                "t_typ": 0.8,
                "t_max": 1.5,
                "d_m": 0.8,
                "floor": FLOOR_ID,
            },
        ],
        "walk_graph": {
            "nodes": {
                "cam09-ground": {"floor_id": FLOOR_ID, "map_xy": [100, 896]},
                "north-turn": {"floor_id": FLOOR_ID, "map_xy": [100, 160]},
                "cross": {"floor_id": FLOOR_ID, "map_xy": [337, 160]},
                "cam06-ground": {"floor_id": FLOOR_ID, "map_xy": [337, 237]},
                "north": {"floor_id": FLOOR_ID, "map_xy": [337, 60]},
                "south": {"floor_id": FLOOR_ID, "map_xy": [337, 430]},
                "west": {"floor_id": FLOOR_ID, "map_xy": [220, 160]},
                "east": {"floor_id": FLOOR_ID, "map_xy": [520, 160]},
            },
            "edges": [
                {"id": "walk-gap-vertical", "from": "cam09-ground", "to": "north-turn", "floor_id": FLOOR_ID, "bidirectional": True},
                {"id": "walk-gap-cross", "from": "north-turn", "to": "cross", "floor_id": FLOOR_ID, "bidirectional": True},
                {"id": "walk-cam06", "from": "cross", "to": "cam06-ground", "floor_id": FLOOR_ID, "bidirectional": True},
                {"id": "walk-north", "from": "cross", "to": "north", "floor_id": FLOOR_ID, "bidirectional": True},
                {"id": "walk-south", "from": "cam06-ground", "to": "south", "floor_id": FLOOR_ID, "bidirectional": True},
                {"id": "walk-west", "from": "cross", "to": "west", "floor_id": FLOOR_ID, "bidirectional": True},
                {"id": "walk-east", "from": "cross", "to": "east", "floor_id": FLOOR_ID, "bidirectional": True},
            ],
        },
        "max_gap_s": EFFECTIVE_MAX_GAP_S,
    }
    (site_dir / "site.yaml").write_text(
        yaml.safe_dump(site, sort_keys=False), encoding="utf-8"
    )
    # The real Cam009 -> Cam006 disappearance is about ten seconds. A global
    # three-second max gap would terminate before Cam006 could be requested,
    # so this fixture uses the smallest safe horizon and advances logical time
    # past it after the real 20-second evidence has been processed.
    (params_dir / "blind-gap-params-v1.yaml").write_text(
        "version: blind-gap-params-v1\n"
        "K: 4\nB: 5\n"
        "anchor_probe_window_s: 4\n"
        "historical_shell_s: 30\n"
        f"max_gap_s: {EFFECTIVE_MAX_GAP_S}\n"
        "max_retries: 0\n"
        "max_walk_snap_m: 3\n"
        "overlap_time_tolerance_s: 1\n"
        "overlap_position_tolerance_m: 1\n",
        encoding="utf-8",
    )
    return site_root


def _candidate_table(result) -> list[tuple]:
    tracklets = {tracklet.id: tracklet for tracklet in result.tracklets}
    return sorted(
        (
            tracklets[candidate.tracklet_id].t_in,
            tracklets[candidate.tracklet_id].t_out,
            candidate.score,
            candidate.id,
            candidate.tracklet_id,
        )
        for candidate in result.candidates
        if candidate.tracklet_id in tracklets
    )


def test_real_cv_blind_gap_reaches_max_gap_with_search_area() -> None:
    require_real_cv()

    from javes_contracts import (
        ArtifactRef,
        CaseWindow,
        CreateCaseBody,
        LastKnown,
        MapPosition,
        PerceptionProbeJob,
        ProbeRequest,
        ReviewDecisionBody,
    )
    from javes_archive import NvidiaArchiveRegistry, create_nvidia_archive_app
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
    fixture = repository_root / "data/nvidia_mtmc/mtmc_12cam"
    output_root = Path(
        os.environ.get(
            "JAVES_REAL_CV_BLIND_GAP_OUT",
            repository_root / "artifacts" / "real-cv-e2e" / "blind-gap",
        )
    ).resolve()
    run_id = datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%S-%fZ")
    run_root = output_root / "runs" / run_id
    run_root.mkdir(parents=True)
    site_root = _write_site_registry(run_root, repository_root)
    artifact_store = FileArtifactStore(run_root / "artifact-store")
    archive_registry = NvidiaArchiveRegistry(fixture)
    base = archive_registry.manifest.timeline_origin
    videos = {
        camera: fixture / archive_registry.manifest.cameras[camera].relative_path
        for camera in CAMERAS
    }
    assert all(path.is_file() for path in videos.values())
    homographies = nvidia_pixel_to_map_homographies(fixture, CAMERAS)
    last_known_xy = project_pixel(homographies["Cam009"], 518.0, 233.0)
    assert 80 <= last_known_xy[0] <= 120
    assert 870 <= last_known_xy[1] <= 920

    archive_api = create_nvidia_archive_app(
        archive_registry,
        service_token="archive-secret",
    )

    site_registry = FilesystemSiteConfigRegistry(site_root)
    site_http = SyncASGIClient(
        create_site_config_app(site_registry, service_token="site-secret"),
        base_url="http://site",
    )
    site_client = HttpSiteConfigClient(
        "http://site",
        "site-secret",
        default_params_version="blind-gap-params-v1",
        client=site_http,
    )
    routing_http = SyncASGIClient(
        create_routing_app(site_client, service_token="routing-secret"),
        base_url="http://routing",
    )
    routing = HttpRoutingAdapter(
        "http://routing", "routing-secret", client=routing_http
    )
    archive_http = SyncASGIClient(archive_api, base_url="http://archive")
    clock = FixedClock(base + timedelta(seconds=20))
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
        job_deadline_s=900,
    )
    auth = AuthService(repository=repository, clock=clock, ids=ids)
    root_user = auth.bootstrap_root("root", "blind-gap-password")
    routing_dispatcher = RoutingTriggerDispatcher(
        repository=repository,
        manager=manager,
        clock=clock,
        worker_id="blind-gap-routing",
    )
    main_http = SyncASGIClient(
        create_app(
            manager,
            auth=auth,
            worker_tokens={
                "perception": "perception-secret",
                "clip": "clip-secret",
                "retention": "retention-secret",
            },
            worker_lease_secret="blind-gap-lease-secret",
        ),
        base_url="http://main",
    )
    login = main_http.post(
        "/v1/auth/login",
        json={"username": "root", "password": "blind-gap-password"},
    )
    assert login.status_code == 200
    access_token = login.json()["access_token"]

    case_id = "real-cv-blind-gap"
    reference_key = f"cases/{case_id}/reference.jpg"
    reference_bytes = _reference_jpeg(videos["Cam009"])
    artifact_store.put(reference_key, reference_bytes, "image/jpeg")
    (run_root / "reference.jpg").write_bytes(reference_bytes)
    created = main_http.post(
        "/v1/cases",
        headers={
            "authorization": f"Bearer {access_token}",
            "idempotency-key": "blind-gap-create",
        },
        json=CreateCaseBody(
            case_id=case_id,
            operator_id=root_user.user_id,
            target_id="pink-overall-worker",
            reference_image_ref=ArtifactRef(object_key=reference_key),
            last_known=LastKnown(
                camera_id="Cam009",
                time=base + timedelta(seconds=2),
                map_position=MapPosition(
                    x=last_known_xy[0],
                    y=last_known_xy[1],
                    floor_id=FLOOR_ID,
                    error_m=0.5,
                ),
            ),
            window=CaseWindow(start=base, end=base + timedelta(seconds=35)),
            site_id="nvidia-mtmc-12cam",
            site_version="blind-gap-v1",
        ).model_dump(mode="json"),
    )
    assert created.status_code == 201, created.text

    archive = HttpVideoArchive(
        "http://archive",
        "archive-secret",
        camera_ids=set(videos),
        artifact_root=fixture,
        client=archive_http,
    )
    settings = PerceptionSettings(
        cameras={
            camera: CameraSource(
                path=str(path), start=base, archive_id=f"nvidia-{camera}"
            )
            for camera, path in videos.items()
        },
        detector_weights=str(repository_root / "models/yolo26n.pt"),
        reid_weights=str(repository_root / "models/osnet_ain_x1_0_msmt17.pt"),
        device="auto",
        sample_fps=1.5,
        min_track_frames=2,
        embed_every=1,
        score_floor=0.55,
    )
    perception_state = PerceptionStateStore(run_root / "perception-state.sqlite")
    perception_worker = PerceptionWorker(
        settings, artifact_store, archive=archive, state_store=perception_state
    )
    runner = PerceptionTaskRunner(
        perception_worker,
        HttpWorkerClient(
            "http://main",
            "perception",
            "perception-secret",
            "blind-gap-worker",
            client=main_http,
        ),
        perception_state,
    )
    decisions: dict[str, str] = {}
    try:
        assert runner.run_once(limit=1, lease_seconds=600) == 1
        first_dispatch = routing_dispatcher.drain()
        assert first_dispatch.failed == 0
        initial_jobs = repository.list_jobs(case_id, states={"PENDING"})
        assert {job.payload.probe.camera for job in initial_jobs} >= {
            "Cam006",
            "Cam009",
            "Cam011",
        }
        cam6_job = next(
            job for job in initial_jobs if job.payload.probe.camera == "Cam006"
        )
        assert "BLIND_EXIT" in cam6_job.payload.probe.reason
        assert "Cam007" not in {job.payload.probe.camera for job in initial_jobs}

        assert runner.run_once(limit=10, lease_seconds=600) == len(initial_jobs)
        evidence_dispatch = routing_dispatcher.drain()
        assert evidence_dispatch.failed == 0

        camera_by_probe = {
            job.payload.probe.probe_id: job.payload.probe.camera
            for job in repository.list_jobs(case_id)
        }
        records = repository.list_probe_results(case_id)
        cam6_result = next(
            record.result
            for record in records
            if camera_by_probe.get(record.result.probe_id) == "Cam006"
        )
        assert cam6_result.status == "DONE"
        assert cam6_result.coverage_ratio == 1.0
        assert cam6_result.candidates, _candidate_table(cam6_result)
        cam6_tracklets = {tracklet.id: tracklet for tracklet in cam6_result.tracklets}

        expected_rows = []
        for candidate in cam6_result.candidates:
            tracklet = cam6_tracklets[candidate.tracklet_id]
            relative_in = (tracklet.t_in - base).total_seconds()
            if not (12.0 <= relative_in <= 14.5) or not tracklet.trajectory:
                continue
            last = tracklet.trajectory[-1]
            ground = project_pixel(
                homographies["Cam006"], last.x_px, last.y_px
            )
            distance = ((ground[0] - 337) ** 2 + (ground[1] - 237) ** 2) ** 0.5
            expected_rows.append((distance, -candidate.score, candidate, tracklet, ground))
        assert expected_rows, _candidate_table(cam6_result)
        _, _, expected_candidate, expected_tracklet, expected_ground = min(
            expected_rows
        )
        assert (
            (expected_ground[0] - 337) ** 2 + (expected_ground[1] - 237) ** 2
        ) ** 0.5 <= 80

        earlier_rows = [
            (candidate.score, candidate, cam6_tracklets[candidate.tracklet_id])
            for candidate in cam6_result.candidates
            if cam6_tracklets[candidate.tracklet_id].t_in
            < expected_tracklet.t_in - timedelta(seconds=1)
        ]
        assert earlier_rows, _candidate_table(cam6_result)
        _, earlier_candidate, earlier_tracklet = max(
            earlier_rows, key=lambda item: (item[0], item[1].id)
        )

        for label, candidate, tracklet, decision in (
            ("earlier", earlier_candidate, earlier_tracklet, "REJECT"),
            ("expected", expected_candidate, expected_tracklet, "CONFIRM"),
        ):
            response = main_http.post(
                f"/v1/cases/{case_id}/decisions",
                headers={
                    "authorization": f"Bearer {access_token}",
                    "idempotency-key": f"blind-gap-{label}-{decision.lower()}",
                },
                json=ReviewDecisionBody(
                    decision_id=f"blind-gap-{label}-{decision.lower()}",
                    candidate_id=candidate.id,
                    tracklet_id=tracklet.id,
                    decision=decision,
                    operator_id=root_user.user_id,
                    occurred_at=base + timedelta(seconds=20),
                ).model_dump(mode="json"),
            )
            assert response.status_code == 200, response.text
            decisions[candidate.id] = decision

        gallery_before = repository.get(case_id).gallery_ref
        assert runner.run_once(limit=1, lease_seconds=600) == 1
        gallery_after = repository.get(case_id).gallery_ref
        assert gallery_after and gallery_after != gallery_before
        review_dispatch = routing_dispatcher.drain()
        assert review_dispatch.failed == 0

        pending_after_review = repository.list_jobs(case_id, states={"PENDING"})
        cam8_jobs = [
            job
            for job in repository.list_jobs(case_id)
            if job.payload.probe.camera == "Cam008"
        ]
        assert cam8_jobs
        if pending_after_review:
            assert runner.run_once(limit=10, lease_seconds=600) == len(
                pending_after_review
            )
        records = repository.list_probe_results(case_id)
        camera_by_probe = {
            job.payload.probe.probe_id: job.payload.probe.camera
            for job in repository.list_jobs(case_id)
        }
        cam8_results = [
            record.result
            for record in records
            if camera_by_probe.get(record.result.probe_id) == "Cam008"
        ]
        assert cam8_results
        assert all(result.status == "DONE" for result in cam8_results)
        assert all(not result.candidates for result in cam8_results)

        # Let the already-queued result/decision triggers observe the complete
        # real-video evidence after the post-sighting gap horizon.
        clock.set(base + timedelta(seconds=35))
        final_dispatch = routing_dispatcher.drain()
        assert final_dispatch.failed == 0

        for _ in range(20):
            # run_once also advances the runner's active OPEN tasks, whose Main
            # job state is LEASED rather than PENDING.
            if runner.run_once(limit=10, lease_seconds=600):
                report = routing_dispatcher.drain()
                assert report.failed == 0
            if repository.get(case_id).lifecycle == "CLOSED":
                break
            # Production scheduler heartbeats advance an empty historical
            # shell even when no new Perception result exists.
            manager.heartbeat(case_id)
            report = routing_dispatcher.drain()
            assert report.failed == 0, [
                (trigger.trigger_type, trigger.last_error)
                for trigger in repository.list_routing_triggers(case_id=case_id)
                if trigger.last_error
            ]

        case = repository.get(case_id)
        assert case.gallery_ref == gallery_after
        assert case.lifecycle == "CLOSED", {
            "done": case.routing_state.done,
            "stop_reason": case.routing_state.stop_reason,
            "frontier": case.routing_state.search_frontier.model_dump(
                mode="json"
            ),
            "pending_probes": [
                probe.model_dump(mode="json")
                for probe in case.routing_state.pending_probes
            ],
            "job_states": [
                (job.payload.probe.camera, job.state, job.payload.probe.reason)
                for job in repository.list_jobs(case_id)
            ],
        }

        validation_probe = ProbeRequest(
            probe_id="validation-only-cam007",
            camera="Cam007",
            t_from=base + timedelta(seconds=16),
            t_to=base + timedelta(seconds=20),
            priority=0.0,
            reason="VALIDATION_ONLY",
        )
        # Cam007 sees the worker only briefly at the end.  This isolated
        # validation run samples more densely and deliberately permits a
        # one-frame track; it is never delivered to Main or Routing.
        validation_worker = PerceptionWorker(
            settings.model_copy(
                update={"sample_fps": 3.0, "min_track_frames": 1}
            ),
            artifact_store,
            archive=archive,
        )
        validation_result = validation_worker.execute(
            PerceptionProbeJob(
                job_id="validation-only-cam007",
                case_id=case_id,
                target_gallery_ref=gallery_after,
                probe=validation_probe,
            ),
            base + timedelta(seconds=20),
        )
        assert validation_result.status == "DONE"
        assert validation_result.candidates
        validation_best = max(
            validation_result.candidates, key=lambda item: item.cosine
        )
        assert validation_best.cosine >= 0.8
    finally:
        perception_state.close()

    dashboard = main_http.get(
        f"/v1/cases/{case_id}/view",
        headers={"authorization": f"Bearer {access_token}"},
    )
    assert dashboard.status_code == 200, dashboard.text
    view = dashboard.json()
    assert view["lifecycle"] == "CLOSED"
    assert view["mode"] == "PAST_SUMMARY"
    assert view["stop_reason"] == "MAX_GAP"
    assert view["route_presentation"]["position_state"] == "LOST"
    assert view["estimate"]["reachable_segments"]
    assert len(view["estimate"]["reachable_segments"]) >= 2
    assert view["best_hypothesis"] is not None
    best = view["best_hypothesis"]
    assert any(
        transition["kind"] == "BLIND" and transition["geometry"]
        for transition in best["transitions"]
    )
    inferred = next(
        transition["geometry"]["points"]
        for transition in best["transitions"]
        if transition["kind"] == "BLIND" and transition["geometry"]
    )
    assert any(abs(point[0] - 100) <= 10 and abs(point[1] - 160) <= 10 for point in inferred)
    assert any(abs(point[0] - 337) <= 10 and abs(point[1] - 160) <= 10 for point in inferred)

    route_map = view["rendered_route_maps"][0]
    route_response = main_http.get(
        route_map["url"],
        headers={"authorization": f"Bearer {access_token}"},
    )
    assert route_response.status_code == 200
    route_svg = route_response.content
    assert b'data-role="observed-path"' in route_svg
    assert b'data-role="inferred-path"' in route_svg
    assert b'data-role="reachable-area"' in route_svg
    svg_root = ET.fromstring(route_svg)
    visible_text = " ".join(
        text.strip() for text in svg_root.itertext() if text and text.strip()
    )
    assert "%" not in visible_text
    assert "Cam007" not in visible_text
    assert "Possible area when search stopped" in visible_text

    result_payloads = [
        record.result.model_dump(mode="json", by_alias=True)
        for record in repository.list_probe_results(case_id)
    ]
    report = {
        "case_id": case_id,
        "target": "worker in pink overalls",
        "lifecycle": view["lifecycle"],
        "mode": view["mode"],
        "stop_reason": view["stop_reason"],
        "position_state": view["route_presentation"]["position_state"],
        "route": [visit["camera_id"] for visit in best["visits"]],
        "candidate_decisions": decisions,
        "validation_only": {
            "camera": "Cam007",
            "best_gallery_cosine": validation_best.cosine,
            "sent_to_main": False,
        },
        "last_known_ground_position": list(last_known_xy),
        "confirmed_cam006_ground_position": list(expected_ground),
        "effective_max_gap_s": EFFECTIVE_MAX_GAP_S,
        "max_gap_note": (
            "The real Cam009-to-Cam006 disappearance exceeds 3 s; 15 s is "
            "the smallest horizon that preserves its complete Cam006 track."
        ),
        "source_video_window_s": [0.0, 20.0],
        "run_directory": str(run_root.resolve()),
    }
    timeline = [
        {
            "time_s": round((expected_tracklet.t_in - base).total_seconds(), 3),
            "camera": "Cam006",
            "event": "candidate appears",
            "details": "confirmed through Controls review",
        },
        {
            "time_s": round((expected_tracklet.t_out - base).total_seconds(), 3),
            "camera": "Cam006",
            "event": "last seen",
            "details": "start of unobserved gap",
        },
        {
            "time_s": 20.0,
            "camera": "Cam008",
            "event": "checked empty",
            "details": "DONE with no target candidates",
        },
        {
            "time_s": 35.0,
            "camera": "—",
            "event": "search stopped",
            "details": "MAX_GAP; reachable area snapshot retained",
        },
    ]
    publish_inspectable_report(
        output_root,
        run_root,
        route_svg=route_svg,
        case_view=view,
        probe_results=result_payloads,
        report=report,
        videos=[
            ReportVideo("Cam009", videos["Cam009"], "evidence", note="reference at 2 s"),
            ReportVideo("Cam011", videos["Cam011"], "evidence", note="overlap anchor"),
            ReportVideo("Cam006", videos["Cam006"], "evidence", note="confirmed at 12.7–16.7 s"),
            ReportVideo("Cam008", videos["Cam008"], "checked-empty", note="target absent after last sighting"),
            ReportVideo("Cam007", videos["Cam007"], "validation-only", note="never sent to Main"),
        ],
        timeline=timeline,
    )
    print(f"blind-gap real-CV E2E output: {output_root}")
